"""Which folders get a delta round, and when: `sync` runs one pass, `sync --live` runs forever."""

import sqlite3
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass

import httpx
import structlog

from .auth import AuthExpired, AuthUnavailable
from .folders import sync_folders
from .graph import Graph
from .mail import Counters, sync_folder

log = structlog.get_logger()

_SUMMED = ("listed", "downloaded", "failed", "moved", "deleted")
# A folder's own failures (gone, forbidden, 5xx, refused or malformed page) skip that folder only.
# Read/write timeouts are tied to one item (a huge or stuck $value); connect errors mean the network.
_FOLDER_ERRORS = (httpx.HTTPStatusError, httpx.ReadTimeout, httpx.WriteTimeout, ValueError, RuntimeError)
# Statuses about the session or the whole mailbox: the cycle backs off instead of trying each folder.
# 503/504 stay per folder: one folder's backend can keep failing while the others answer.
_MAILBOX_STATUSES = frozenset({401, 429})
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
    except _FOLDER_ERRORS as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        if isinstance(exc, AuthExpired | AuthUnavailable) or status in _MAILBOX_STATUSES:
            raise
        log.warning(
            "folder_round_failed",
            path=folder["path"],
            error=type(exc).__name__,
            status=status,
            detail=str(exc)[:200],
        )
        return None


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
    `cadence.folders`. Survives sign-in and Graph outages (retries one Inbox interval later); stops
    when `wait` returns True or on KeyboardInterrupt (SIGINT/SIGTERM)."""
    due_tree = due_inbox = due_folders = clock()
    inbox: list[sqlite3.Row] = []
    others: list[sqlite3.Row] = []
    pending: list[sqlite3.Row] = []  # other folders still owed a round in the current pass
    requeued: set[str] = set()  # failing folders already sent to the back of this pass
    walked = False

    def inbox_due() -> bool:
        return clock() >= due_inbox

    while True:
        now = clock()
        try:
            if now >= due_tree:
                try:
                    folders = sync_folders(graph, db)
                    inbox_id = graph.get("me/mailFolders/inbox", **{"$select": "id"})["id"]
                except _RETRIED as exc:
                    if not walked:
                        raise
                    # The previous tree still serves: a failed refresh must not stop the Inbox.
                    log.warning("tree_walk_failed", error=type(exc).__name__, detail=str(exc)[:200])
                    due_tree = clock() + cadence.tree
                else:
                    inbox = [f for f in folders if f["id"] == inbox_id]
                    others = [f for f in folders if f["id"] != inbox_id]
                    pending = []  # rows of the old tree; the cursors in sync_state keep the progress
                    walked = True
                    due_tree = now + cadence.tree
            if now >= due_inbox:
                for folder in inbox:
                    _attempt(graph, db, executor, folder)
                due_inbox = clock() + cadence.inbox
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
        except _RETRIED as exc:
            log.warning("live_cycle_failed", error=type(exc).__name__, detail=str(exc)[:200])
            # Every task waits at least one Inbox interval: no hot loop against a dead endpoint.
            retry = clock() + cadence.inbox
            due_tree, due_inbox, due_folders = (max(d, retry) for d in (due_tree, due_inbox, due_folders))
        if wait(max(0.0, min(due_tree, due_inbox, due_folders) - clock())):
            return
