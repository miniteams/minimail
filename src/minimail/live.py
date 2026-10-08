"""Which folders get a delta round, and when: `sync` runs one pass, `sync --live` runs forever."""

import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass

import httpx
import structlog

from .auth import AuthExpired, AuthUnavailable
from .folders import sync_folders
from .graph import SESSION_STATUSES, Graph
from .mail import Counters, Reconciled, reconcile_steps, sync_folder

log = structlog.get_logger()

_SUMMED = ("listed", "downloaded", "failed", "moved", "deleted")
# A folder's own failures (gone, forbidden, 5xx, refused or malformed page) skip that folder only.
# Read/write timeouts are tied to one item (a huge or stuck $value); connect errors mean the network.
FOLDER_ERRORS = (httpx.HTTPStatusError, httpx.ReadTimeout, httpx.WriteTimeout, ValueError, RuntimeError)
# SESSION_STATUSES back off the whole cycle; 503/504 stay per folder: one folder's backend can keep
# failing while the others answer.
# What a live cycle survives: sign-in and network outages, mailbox-wide throttling, Graph answering
# garbage to the tree walk, a busy or flaky archive disk (NAS I/O, "database is locked").
_RETRIED = (
    AuthExpired,
    AuthUnavailable,
    httpx.HTTPError,
    ValueError,
    RuntimeError,
    sqlite3.OperationalError,
)


@dataclass
class Cadence:
    inbox: float
    folders: float
    tree: float
    reconcile: float = 86400.0


def _round(
    graph: Graph,
    db: sqlite3.Connection,
    executor: Executor,
    folder: sqlite3.Row,
    pause: Callable[[], bool] | None = None,
) -> Counters:
    c = sync_folder(graph, db, executor, folder["id"], pause)
    if any(getattr(c, name) for name in _SUMMED):
        log.info("folder_synced", path=folder["path"], **vars(c))
    return c


def _attempt(
    graph: Graph,
    db: sqlite3.Connection,
    executor: Executor,
    folder: sqlite3.Row,
    pause: Callable[[], bool] | None = None,
) -> Counters | None:
    """`_round`, or None (logged) if the folder failed on its own; mailbox-wide failures propagate."""
    try:
        return _round(graph, db, executor, folder, pause)
    except FOLDER_ERRORS as exc:
        folder_failed(exc, folder, "folder_round_failed")
        return None


def folder_failed(exc: Exception, folder: sqlite3.Row, event: str) -> None:
    """Log a failure of the folder itself; re-raise a mailbox-wide one."""
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    if isinstance(exc, AuthExpired | AuthUnavailable) or status in SESSION_STATUSES:
        raise exc
    log.warning(event, path=folder["path"], error=type(exc).__name__, status=status, detail=str(exc)[:200])


def sync_pass(
    graph: Graph, db: sqlite3.Connection, executor: Executor, folders: Sequence[sqlite3.Row]
) -> Counters:
    """One full delta round per folder; returns the summed counters."""
    total = Counters()
    for folder in folders:
        c = _round(graph, db, executor, folder)
        for name in _SUMMED:
            setattr(total, name, getattr(total, name) + getattr(c, name))
    return total


def _sleep(seconds: float) -> bool:
    time.sleep(seconds)
    return False


def run_live(
    graph: Graph,
    db: sqlite3.Connection,
    executor: Executor,
    cadence: Cadence,
    *,
    wait: Callable[[float], bool] = _sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Folder tree every `cadence.tree`, Inbox every `cadence.inbox`, the other folders every
    `cadence.folders`, a reconcile pass of every folder every `cadence.reconcile` (after the other
    folders, yielding to the Inbox like them). Survives sign-in and Graph outages (retries one Inbox
    interval later); stops when `wait` returns True or on KeyboardInterrupt (SIGINT/SIGTERM)."""
    due_tree = due_inbox = due_folders = clock()
    # The start-up pass is already the heaviest; `minimail reconcile` covers an immediate need.
    due_reconcile = due_folders + cadence.reconcile
    to_reconcile: list[sqlite3.Row] = []
    job: tuple[Iterator[None], Reconciled] | None = None  # the folder reconcile in progress
    inbox: list[sqlite3.Row] = []
    others: list[sqlite3.Row] = []
    pending: list[sqlite3.Row] = []  # other folders still owed a round in the current pass
    requeued: set[str] = set()  # failing folders already sent to the back of this pass
    walked = False

    def inbox_due() -> bool:
        return clock() >= due_inbox

    def walk_tree(now: float) -> None:
        nonlocal inbox, others, pending, walked, due_tree
        try:
            folders = sync_folders(graph, db)
            inbox_id = graph.get("me/mailFolders/inbox", **{"$select": "id"})["id"]
        except _RETRIED as exc:
            if not walked:
                raise
            # The previous tree still serves: a failed refresh must not stop the Inbox.
            log.warning("tree_walk_failed", error=type(exc).__name__, detail=str(exc)[:200])
            due_tree = clock() + cadence.tree
            return
        inbox = [f for f in folders if f["id"] == inbox_id]
        others = [f for f in folders if f["id"] != inbox_id]
        pending = []  # rows of the old tree; the cursors in sync_state keep the progress
        walked = True
        due_tree = now + cadence.tree

    while True:
        now = clock()
        try:
            if not walked and now >= due_tree:
                walk_tree(now)  # the Inbox round needs a tree to find the Inbox
            if now >= due_inbox:
                for folder in inbox:
                    _attempt(graph, db, executor, folder)
                due_inbox = clock() + cadence.inbox
            # A refresh runs after the Inbox round: walk time never delays a due Inbox.
            if walked and now >= due_tree:
                walk_tree(now)
            if now >= due_folders:
                if not pending:
                    pending, requeued = list(others), set()
                # Long rounds (backfill, bulk move, MIME catch-up) yield as soon as the Inbox is due.
                while pending and not inbox_due():
                    folder = pending[0]
                    c = _attempt(graph, db, executor, folder, inbox_due)
                    if c is None:
                        # Once to the back of the pass, then skipped: it must not starve the others.
                        pending.pop(0)
                        if folder["id"] not in requeued:
                            requeued.add(folder["id"])
                            pending.append(folder)
                        continue
                    if c.paused:
                        break
                    pending.pop(0)
                due_folders = clock() + (0 if pending else cadence.folders)
            if walked and not pending and now >= due_reconcile:
                if not to_reconcile:
                    to_reconcile = [*inbox, *others]
                while to_reconcile and not inbox_due():
                    folder = to_reconcile[0]
                    if job is None:
                        c = Reconciled()
                        job = (reconcile_steps(graph, db, folder["id"], c), c)
                    try:
                        next(job[0])
                        continue
                    except StopIteration:
                        log.info("folder_reconciled", path=folder["path"], **job[1].summary())
                    except FOLDER_ERRORS as exc:
                        job = None  # a generator that raised is finished; a mailbox-wide error retries it
                        folder_failed(exc, folder, "folder_reconcile_failed")
                    job = None
                    to_reconcile.pop(0)
                if not to_reconcile:
                    due_reconcile = clock() + cadence.reconcile
        except _RETRIED as exc:
            log.warning("live_cycle_failed", error=type(exc).__name__, detail=str(exc)[:200])
            job = None  # restarts the folder being reconciled
            # Every task waits at least one Inbox interval: no hot loop against a dead endpoint.
            retry = clock() + cadence.inbox
            due_tree, due_inbox, due_folders, due_reconcile = (
                max(d, retry) for d in (due_tree, due_inbox, due_folders, due_reconcile)
            )
        if wait(max(0.0, min(due_tree, due_inbox, due_folders, due_reconcile) - clock())):
            return
