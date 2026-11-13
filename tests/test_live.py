"""`sync --live` loop on a simulated clock: cadence order, pausing, failure backoff."""

import sqlite3
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from minimail import live
from minimail.auth import AuthExpired, AuthUnavailable
from minimail.graph import Graph
from minimail.live import Cadence, run_live

from .conftest import BASE, StaticTokens, make_graph
from .test_mail import FakeGraph, item

CADENCE = Cadence(inbox=30.0, folders=300.0, tree=900.0)


FOLDER_FAULTS = [
    (500, "HTTPStatusError"),
    (502, "HTTPStatusError"),
    (503, "HTTPStatusError"),
    (504, "HTTPStatusError"),
    (403, "HTTPStatusError"),
    (404, "HTTPStatusError"),
    ("non_json", "JSONDecodeError"),
    ("malformed", "RuntimeError"),
    ("off_host", "ValueError"),
    (httpx.ReadTimeout("stuck delta"), "ReadTimeout"),
]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class LiveGraph(FakeGraph):
    """A mailbox with an Inbox and other folders, each serving its own delta pages. `events` records
    (time, call); a delta page of a folder in `cost` advances the clock."""

    def __init__(self, clock: Clock) -> None:
        super().__init__([])
        self.clock = clock
        self.folders: dict[str, list[list[dict[str, Any]]]] = {"INBOX": [[]], "F2": [[]]}
        self.cost: dict[str, float] = {}
        self.events: list[tuple[float, str]] = []
        self.broken: int | Exception | httpx.Response | None = None  # every call answers or raises this
        # folder id → how its delta calls fail: a status, "non_json", "malformed" or "off_host"
        self.failing: dict[str, int | str | Exception] = {}
        self.list_fail: dict[str, int | Exception] = {}  # folder id → its reconcile listing's failure

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if isinstance(self.broken, Exception):
            raise self.broken
        if isinstance(self.broken, httpx.Response):
            return self.broken
        if self.broken:
            return httpx.Response(self.broken)
        path = req.url.path.removeprefix("/v1.0/")
        if path == "me/mailFolders":
            self.events.append((self.clock.now, "tree"))
            tree = [
                {"id": f, "displayName": "Inbox" if f == "INBOX" else f"Folder {f}", "totalItemCount": 0}
                for f in self.folders
            ]
            return httpx.Response(200, json={"value": tree})
        if path == "me/mailFolders/inbox":
            return httpx.Response(200, json={"id": "INBOX"})
        if path.endswith("/messages/delta"):
            folder = path.split("/")[2]
            page = int(req.url.params.get("page", "0"))
            self.events.append((self.clock.now, f"{folder}:{page}"))
            self.clock.now += self.cost.get(folder, 0.0)
            fault = self.failing.get(folder)
            if isinstance(fault, Exception):
                raise fault
            if isinstance(fault, int):
                return httpx.Response(fault)
            if fault == "non_json":
                return httpx.Response(200, text="<html>maintenance</html>")
            pages = self.folders[folder]
            body: dict[str, Any] = {"value": pages[page]}
            link = f"{BASE}/me/mailFolders/{folder}/messages/delta"
            if fault == "malformed":
                pass
            elif fault == "off_host":
                body["@odata.nextLink"] = "https://evil.test/v1.0/steal"
            elif page + 1 < len(pages):
                body["@odata.nextLink"] = f"{link}?page={page + 1}"
            else:
                body["@odata.deltaLink"] = f"{link}?deltatoken=t"
            return httpx.Response(200, json=body)
        if path.startswith("me/mailFolders/") and path.endswith("/messages"):
            folder = path.split("/")[2]
            self.events.append((self.clock.now, f"list:{folder}"))
            self.clock.now += self.cost.get(f"list:{folder}", 0.0)
            fail = self.list_fail.get(folder)
            if isinstance(fail, Exception):
                raise fail
            if fail:
                return httpx.Response(fail)
        if path.endswith("/$value"):
            self.events.append((self.clock.now, f"$value:{path.split('/')[2]}"))
        return super().__call__(req)


class FlakyTokens(StaticTokens):
    def __init__(self) -> None:
        super().__init__()
        self.error: Exception | None = None

    def token(self, force_refresh: bool = False) -> str:
        super().token(force_refresh)
        if self.error:
            raise self.error
        return "tok"


def waiter(clock: Clock, waits: list[float], stop: Callable[[], bool]) -> Callable[[float], bool]:
    """Advances the simulated clock by each requested wait; stops the loop once `stop()` holds."""

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        assert len(waits) < 5000, "hot loop: the live loop never lets time pass"
        if stop():
            return True
        clock.now += seconds
        return False

    return wait


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(2) as executor:
        yield executor


def _calls(fake: LiveGraph, prefix: str = "") -> list[tuple[float, str]]:
    return [(t, e) for t, e in fake.events if e.startswith(prefix)]


def _deltas(fake: LiveGraph) -> list[tuple[float, str]]:
    return [(t, e) for t, e in fake.events if not e.startswith("$value")]


def test_cadence_order(db: sqlite3.Connection, pool: ThreadPoolExecutor) -> None:
    clock = Clock()
    fake = LiveGraph(clock)
    waits: list[float] = []
    run_live(
        make_graph(fake), db, pool, CADENCE, wait=waiter(clock, waits, lambda: clock() >= 900), clock=clock
    )
    assert fake.events[:3] == [(0, "tree"), (0, "INBOX:0"), (0, "F2:0")]
    # The first walk precedes the Inbox (it finds it); a refresh follows the Inbox round.
    assert fake.events[-3:] == [(900, "INBOX:0"), (900, "tree"), (900, "F2:0")]
    assert [t for t, _ in _calls(fake, "INBOX")] == [30.0 * k for k in range(31)]
    assert [t for t, _ in _calls(fake, "F2")] == [0, 300, 600, 900]
    assert [t for t, _ in _calls(fake, "tree")] == [0, 900]
    assert set(waits) == {30.0}


def test_new_inbox_mail_is_searchable_within_one_inbox_tick(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """001-AC4: a mail landing right after an Inbox check is stored, with its search text, by the
    next check one interval later."""
    clock = Clock()
    fake = LiveGraph(clock)
    waits: list[float] = []

    def arrive_then_stop() -> bool:
        if len(waits) == 1:
            fake.folders["INBOX"] = [[item("n1", "INBOX")]]
        return len(waits) == 2

    run_live(make_graph(fake), db, pool, CADENCE, wait=waiter(clock, waits, arrive_then_stop), clock=clock)
    assert _calls(fake, "$value") == [(30.0, "$value:n1")]
    row = db.execute("SELECT folder_id, search_text FROM messages WHERE id='n1'").fetchone()
    assert row["folder_id"] == "INBOX" and "déjà" in row["search_text"]


def test_long_round_yields_to_the_inbox_then_resumes(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """001-AC4: a backfilling folder pauses between pages once the Inbox is due, and resumes from
    its saved cursor right after the Inbox check, not a full folder interval later."""
    clock = Clock()
    fake = LiveGraph(clock)
    fake.folders["F2"] = [[item(f"a{p}", "F2")] for p in range(4)]
    fake.cost["F2"] = 20.0
    waits: list[float] = []
    run_live(
        make_graph(fake), db, pool, CADENCE, wait=waiter(clock, waits, lambda: len(waits) == 2), clock=clock
    )
    assert _deltas(fake) == [
        (0, "tree"),
        (0, "INBOX:0"),
        (0, "F2:0"),
        (20, "F2:1"),  # ends at 40 s, Inbox due since 30 s: pause here
        (40, "INBOX:0"),
        (40, "F2:2"),
        (60, "F2:3"),
    ]
    assert waits == [0.0, 0.0]  # no idle wait after the pause, nor after the Inbox fell due again
    assert db.execute("SELECT COUNT(*) FROM messages WHERE mime IS NOT NULL").fetchone()[0] == 4


def test_no_new_folder_round_starts_once_the_inbox_is_due(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """001-AC4: a folder finishing after the Inbox fell due hands over to the Inbox, not to the
    next folder."""
    clock = Clock()
    fake = LiveGraph(clock)
    fake.folders["F2"] = [[], []]
    fake.folders["F3"] = [[]]
    fake.cost["F2"] = 20.0
    waits: list[float] = []
    run_live(
        make_graph(fake), db, pool, CADENCE, wait=waiter(clock, waits, lambda: len(waits) == 2), clock=clock
    )
    assert _deltas(fake) == [
        (0, "tree"),
        (0, "INBOX:0"),
        (0, "F2:0"),
        (20, "F2:1"),
        (40, "INBOX:0"),
        (40, "F3:0"),
    ]


@pytest.mark.parametrize(
    ("fault", "error"),
    FOLDER_FAULTS,
    ids=["read_timeout" if isinstance(f, Exception) else str(f) for f, _ in FOLDER_FAULTS],
)
def test_a_failing_folder_does_not_starve_the_next(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, fault: int | str | Exception, error: str
) -> None:
    """A folder whose round keeps failing on its own (503/504 left after retries included) is
    retried once at the back of the pass, then skipped until the next pass; the folders after it
    still sync."""
    clock = Clock()
    fake = LiveGraph(clock)
    fake.folders["F3"] = [[item("m3", "F3")]]
    fake.failing = {"F2": fault}
    fake.cost["F2"] = 10.0  # a requeue loop would run into the Inbox deadline, not forever
    waits: list[float] = []
    with capture_logs() as logs:
        run_live(
            make_graph(fake),
            db,
            pool,
            CADENCE,
            wait=waiter(clock, waits, lambda: fake.value_calls["m3"] > 0 or clock() >= 600),
            clock=clock,
        )
    failed = [(e["path"], e["error"]) for e in logs if e["event"] == "folder_round_failed"]
    assert failed == [("Folder F2", error)] * 2  # one pass: first try, one requeue, no more
    assert not [e for e in logs if e["event"] == "live_cycle_failed"]
    assert db.execute("SELECT folder_id FROM messages WHERE mime IS NOT NULL").fetchall()[0][0] == "F3"


@pytest.mark.parametrize(
    "fault", [500, httpx.Response(200, text="<html>maintenance</html>")], ids=["500", "non_json"]
)
def test_a_failing_tree_refresh_keeps_the_previous_tree(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, fault: int | httpx.Response
) -> None:
    """Once a tree exists, a failing refresh is logged and retried a tree interval later while the
    Inbox and the other folders keep syncing on the previous tree."""
    clock = Clock()
    fake = LiveGraph(clock)
    real = fake.__call__
    waits: list[float] = []

    def tree_breaks_after_first_walk(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/mailFolders") and clock() > 0:
            fake.events.append((clock(), "tree"))
            return fault if isinstance(fault, httpx.Response) else httpx.Response(fault)
        return real(req)

    with capture_logs() as logs:
        run_live(
            make_graph(tree_breaks_after_first_walk),
            db,
            pool,
            CADENCE,
            wait=waiter(clock, waits, lambda: len(waits) == 61),  # a hot loop ends too, at once
            clock=clock,
        )
    assert [t for t, e in fake.events if e == "tree"] == [0, 900, 1800]
    assert [t for t, e in fake.events if e == "INBOX:0"] == [30.0 * k for k in range(61)]
    assert [t for t, e in fake.events if e == "F2:0"] == [0, 300, 600, 900, 1200, 1500, 1800]
    assert len([e for e in logs if e["event"] == "tree_walk_failed"]) == 2
    assert not [e for e in logs if e["event"] == "live_cycle_failed"]


def test_refused_next_link_never_becomes_the_cursor(db: sqlite3.Connection, pool: ThreadPoolExecutor) -> None:
    """An off-host nextLink is refused before it is stored: the folder keeps its previous cursor and
    the folders after it still sync."""
    clock = Clock()
    fake = LiveGraph(clock)
    fake.folders["F3"] = [[]]
    fake.cost["F2"] = 10.0  # a requeue loop would run into the Inbox deadline, not forever
    waits: list[float] = []

    def poison_after_first_pass() -> bool:
        if len(waits) == 1:
            fake.failing = {"F2": "off_host"}
        return clock() >= 330

    run_live(
        make_graph(fake), db, pool, CADENCE, wait=waiter(clock, waits, poison_after_first_pass), clock=clock
    )
    assert [t for t, e in _deltas(fake) if e == "F3:0"] == [10, 320]
    assert [t for t, e in _deltas(fake) if e == "F2:0"] == [0, 310, 320]
    cursor = db.execute("SELECT next_link, delta_link FROM sync_state WHERE key='folder:F2'").fetchone()
    assert cursor["next_link"] is None and "deltatoken" in cursor["delta_link"]
    assert "evil" not in str(db.execute("SELECT * FROM sync_state").fetchall())


@pytest.mark.parametrize("fault", ["delta", "download", "read_timeout"])
def test_a_failing_inbox_does_not_stop_the_other_folders(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, fault: str
) -> None:
    """The Inbox gets the same per-folder isolation: its delta failing with a 500 every round leaves
    the other folders syncing; a MIME download in it failing (500, read timeout) does not even fail
    the Inbox round (001-AC4)."""
    clock = Clock()
    fake = LiveGraph(clock)
    if fault == "delta":
        fake.failing = {"INBOX": 500}
    else:
        fake.folders["INBOX"] = [[item("n1", "INBOX")]]
        if fault == "download":
            fake.fail_value = {"n1": 500}
        else:
            fake.raise_value = {"n1": httpx.ReadTimeout("stuck body")}
    waits: list[float] = []
    with capture_logs() as logs:
        run_live(make_graph(fake), db, pool, CADENCE, wait=waiter(clock, waits, lambda: True), clock=clock)
    assert [e for _, e in _deltas(fake)] == ["tree", "INBOX:0", "F2:0"]
    failed = [e["path"] for e in logs if e["event"] == "folder_round_failed"]
    assert failed == (["Inbox"] if fault == "delta" else [])
    unavailable = [e["path"] for e in logs if e["event"] == "mime_unavailable"]
    assert unavailable == ([] if fault == "delta" else ["Inbox"])  # the catch-up waits for its back-off
    assert not [e for e in logs if e["event"] == "live_cycle_failed"]


@pytest.mark.parametrize(
    ("fault", "error"),
    [
        (401, "HTTPStatusError"),
        (429, "HTTPStatusError"),
        (AuthExpired("signed out"), "AuthExpired"),
        (AuthUnavailable("aad busy"), "AuthUnavailable"),
    ],
    ids=["401", "429", "auth_expired", "auth_unavailable"],
)
def test_mailbox_wide_failure_in_a_round_backs_off_the_cycle(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, fault: int | Exception, error: str
) -> None:
    """A throttling, session or sign-in failure inside a folder round is about the mailbox, not the
    folder: the cycle backs off instead of spending every folder's retry budget."""
    clock = Clock()
    fake = LiveGraph(clock)
    fake.failing = {"INBOX": fault}
    waits: list[float] = []
    with capture_logs() as logs:
        run_live(make_graph(fake), db, pool, CADENCE, wait=waiter(clock, waits, lambda: True), clock=clock)
    assert not [e for _, e in _deltas(fake) if e.startswith("F2")]
    assert [e["error"] for e in logs if e["event"] == "live_cycle_failed"] == [error]
    assert not [e for e in logs if e["event"] == "folder_round_failed"]
    assert waits == [30.0]


def _no_token(fake: LiveGraph, tokens: FlakyTokens) -> None:
    tokens.error = AuthExpired("sign in with `miniteams login`")


def _aad_down(fake: LiveGraph, tokens: FlakyTokens) -> None:
    tokens.error = AuthUnavailable("token refresh failed")


def _network_down(fake: LiveGraph, tokens: FlakyTokens) -> None:
    fake.broken = httpx.ConnectError("network down")


def _graph_500(fake: LiveGraph, tokens: FlakyTokens) -> None:
    fake.broken = 500


def _non_json(fake: LiveGraph, tokens: FlakyTokens) -> None:
    fake.broken = httpx.Response(200, text="<html>maintenance</html>")


@pytest.mark.parametrize(
    ("fault", "error"),
    [
        (_no_token, "AuthExpired"),
        (_aad_down, "AuthUnavailable"),
        (_network_down, "ConnectError"),
        (_graph_500, "HTTPStatusError"),
        (_non_json, "JSONDecodeError"),
        ("disk", "OperationalError"),
    ],
    ids=["auth_expired", "auth_unavailable", "connect_error", "http_500", "non_json", "sqlite"],
)
def test_failing_cycle_is_logged_and_retried_one_inbox_interval_later(
    db: sqlite3.Connection,
    pool: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    fault: Callable[[LiveGraph, FlakyTokens], None] | str,
    error: str,
) -> None:
    """001-AC10 (live): no token, AAD, Graph or the archive disk failing → logged, retried after one
    Inbox interval, doubling per consecutive failure (never a hot loop), and the loop recovers once
    healed."""
    clock = Clock()
    fake = LiveGraph(clock)
    tokens = FlakyTokens()
    disk_broken = [fault == "disk"]
    real_sync_folders = live.sync_folders

    def sync_folders(graph: Graph, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        if disk_broken[0]:
            raise sqlite3.OperationalError("database is locked")
        return real_sync_folders(graph, conn)

    monkeypatch.setattr(live, "sync_folders", sync_folders)
    if callable(fault):
        fault(fake, tokens)
    waits: list[float] = []

    def heal_after_three() -> bool:
        if len(waits) == 3:
            tokens.error = fake.broken = None
            disk_broken[0] = False
        return len(waits) == 4

    with capture_logs() as logs:
        run_live(
            make_graph(fake, tokens=tokens),
            db,
            pool,
            CADENCE,
            wait=waiter(clock, waits, heal_after_three),
            clock=clock,
        )
    assert [e["error"] for e in logs if e["event"] == "live_cycle_failed"] == [error] * 3
    assert waits[:3] == [30.0, 60.0, 120.0]
    assert _deltas(fake)[-2:] == [(210.0, "INBOX:0"), (210.0, "F2:0")]


def test_a_graph_outage_backs_off_the_cycle_until_it_heals(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """Graph answering 502 to every call is a mailbox outage, not one failure per folder: the first
    failing round's probe aborts the cycle, retried after a doubling delay capped at 10 min; once
    Graph heals, the loop is back to its cadence."""
    clock = Clock()
    fake = LiveGraph(clock)
    fake.folders["F3"] = [[]]
    waits: list[float] = []

    def outage() -> bool:
        if len(waits) in (1, 9):
            fake.broken = 502  # after the start-up cycle, then again after one healthy cycle
        if len(waits) == 8:
            fake.broken = None
        return len(waits) == 10

    with capture_logs() as logs:
        run_live(make_graph(fake), db, pool, CADENCE, wait=waiter(clock, waits, outage), clock=clock)
    cycles = [e for e in logs if e["event"] == "live_cycle_failed"]
    # Healthy again: the next outage starts over from one Inbox interval.
    assert [e["retry_in"] for e in cycles] == [30.0, 60.0, 120.0, 240.0, 480.0, 600.0, 600.0, 30.0]
    assert waits[1:8] == [30.0, 60.0, 120.0, 240.0, 480.0, 600.0, 600.0]
    assert not [e for e in logs if e["event"].endswith("_round_failed")]
    healed = sum(waits[:8])
    assert [e for t, e in _deltas(fake) if t == healed] == ["INBOX:0", "tree", "F2:0", "F3:0"]


def test_cycle_failure_keeps_the_folder_owed(db: sqlite3.Connection, pool: ThreadPoolExecutor) -> None:
    """A transport failure during a folder's round aborts the cycle but not the pass: once the
    network is back, the pass resumes at that folder, not at the first one."""
    clock = Clock()
    fake = LiveGraph(clock)
    fake.folders["F3"] = [[]]
    waits: list[float] = []
    real = fake.__call__

    def flaky(req: httpx.Request) -> httpx.Response:
        if "F3" in req.url.path and len(waits) == 0:
            raise httpx.ConnectError("network down")
        return real(req)

    def stop() -> bool:
        return len(waits) == 2

    run_live(make_graph(flaky), db, pool, CADENCE, wait=waiter(clock, waits, stop), clock=clock)
    assert _deltas(fake) == [(0, "tree"), (0, "INBOX:0"), (0, "F2:0"), (30, "INBOX:0"), (30, "F3:0")]


def test_reconcile_runs_after_the_others_and_yields_to_the_inbox(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """001-AC5, 001-AC4: every `reconcile` seconds each folder is reconciled, after the other folders'
    pass, pausing between listing pages once the Inbox is due and resuming the same listing."""
    clock = Clock()
    fake = LiveGraph(clock)
    fake.listing = {"F2": ["a", "b", "c", "d", "e"]}
    fake.cost["list:F2"] = 20.0
    waits: list[float] = []
    cadence = Cadence(inbox=30.0, folders=300.0, tree=900.0, reconcile=100.0)
    with capture_logs() as logs:
        run_live(
            make_graph(fake),
            db,
            pool,
            cadence,
            wait=waiter(clock, waits, lambda: clock.now >= 250),
            clock=clock,
        )
    done = [set(e) - {"event", "log_level", "path"} for e in logs if e["event"] == "folder_reconciled"]
    assert done == [{"listed", "moved", "deleted", "revived", "unsynced"}] * 2
    # Due at 100; the F2 page ending at 120 hits the Inbox's due time: the Inbox runs, then page 2.
    assert [e for t, e in fake.events if 100 <= t <= 140] == [
        "list:INBOX",
        "list:F2",
        "INBOX:0",
        "list:F2",
        "list:F2",
    ]
    assert [t for t, e in fake.events if e == "list:INBOX"] == [100]  # next pass due at 260


@pytest.mark.parametrize(
    ("status", "event"),
    [
        (500, "folder_reconcile_failed"),
        (429, "live_cycle_failed"),
        (httpx.ConnectError("network down"), "live_cycle_failed"),
    ],
)
def test_a_failing_reconcile_skips_its_folder_or_retries_the_cycle(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, status: int | Exception, event: str
) -> None:
    """001-AC5: a folder failing on its own is skipped, the next one still reconciled; a mailbox-wide
    failure backs off the cycle and restarts the folder's listing."""
    clock = Clock()
    fake = LiveGraph(clock)
    fake.folders["F3"] = [[]]
    fake.list_fail = {"F2": status}
    waits: list[float] = []

    def stop() -> bool:
        if clock.now >= 160:
            fake.list_fail.clear()
        return clock.now >= 250

    cadence = Cadence(inbox=30.0, folders=300.0, tree=900.0, reconcile=150.0)
    with capture_logs() as logs:
        run_live(make_graph(fake), db, pool, cadence, wait=waiter(clock, waits, stop), clock=clock)
    assert event in [e["event"] for e in logs]
    lists = [e for _, e in _calls(fake, "list")]
    assert lists[-2:] == ["list:F2", "list:F3"] and lists.count("list:F3") == 1
    assert lists.count("list:INBOX") == 1  # a retried cycle resumes at the failed folder
    if status == 500:
        assert lists == ["list:INBOX", "list:F2", "list:F3"]
    else:  # F2 listed again once the failure clears (at 160), not counted as reconciled
        assert [t for t, e in _calls(fake, "list:F2")][-1] >= 160


class CalLive(LiveGraph):
    """LiveGraph plus a calendar: `cal_pages` pages per round, each costing `cal_cost` seconds."""

    def __init__(self, clock: Clock) -> None:
        super().__init__(clock)
        self.cal_pages = 1
        self.cal_cost = 0.0
        self.cal_fail: int | str | None = None
        self.cal_items: list[dict[str, Any]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/v1.0/")
        if path != "me/calendarView/delta" or self.broken:
            return super().__call__(req)
        page = int(req.url.params.get("page", "0"))
        self.events.append((self.clock.now, f"cal:{page}"))
        self.clock.now += self.cal_cost
        if self.cal_fail == "non_json":
            return httpx.Response(200, text="<html>maintenance</html>")
        if isinstance(self.cal_fail, int):
            return httpx.Response(self.cal_fail)
        body: dict[str, Any] = {"value": self.cal_items}
        if page + 1 < self.cal_pages:
            body["@odata.nextLink"] = f"{BASE}/{path}?page={page + 1}"
        else:
            body["@odata.deltaLink"] = f"{BASE}/{path}?deltatoken=t"
        return httpx.Response(200, json=body)


def test_calendar_follows_every_inbox_round(db: sqlite3.Connection, pool: ThreadPoolExecutor) -> None:
    """001-AC16 (cadence): the calendar is checked right after every Inbox round, 30 s apart."""
    clock = Clock()
    fake = CalLive(clock)
    fake.cal_items = [{"id": "o1", "type": "occurrence", "seriesMasterId": "m"}]
    with capture_logs() as logs:
        run_live(
            make_graph(fake), db, pool, CADENCE, wait=waiter(clock, [], lambda: clock() >= 60), clock=clock
        )
    rounds = [e for e in fake.events if e[1].startswith(("INBOX", "cal"))]
    assert rounds == [(t, e) for t in (0.0, 30.0, 60.0) for e in ("INBOX:0", "cal:0")]
    assert [e["listed"] for e in logs if e["event"] == "calendar_synced"] == [1, 1, 1]


def test_a_long_calendar_round_yields_to_the_inbox(db: sqlite3.Connection, pool: ThreadPoolExecutor) -> None:
    """001-AC4: a calendar backfill stops between pages once the Inbox is due, then resumes."""
    clock = Clock()
    fake = CalLive(clock)
    fake.cal_pages, fake.cal_cost = 3, 20.0
    run_live(make_graph(fake), db, pool, CADENCE, wait=waiter(clock, [], lambda: clock() >= 60), clock=clock)
    rounds = [e for e in fake.events if e[1].startswith(("INBOX", "cal"))]
    assert rounds[:5] == [(0, "INBOX:0"), (0, "cal:0"), (20, "cal:1"), (40, "INBOX:0"), (40, "cal:2")]


@pytest.mark.parametrize("status", [500, 403, 404, "non_json"])
def test_a_failing_calendar_is_logged_and_isolated(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, status: int | str
) -> None:
    """The calendar fails on its own like a folder: logged, the Inbox and other folders go on, retried
    next Inbox round."""
    clock = Clock()
    fake = CalLive(clock)
    fake.cal_fail = status
    with capture_logs() as logs:
        run_live(
            make_graph(fake), db, pool, CADENCE, wait=waiter(clock, [], lambda: clock() >= 30), clock=clock
        )
    failed = [e for e in logs if e["event"] == "calendar_round_failed"]
    assert len(failed) == 2 and failed[0]["status"] == (None if status == "non_json" else status)
    assert [e for _, e in fake.events if e.startswith(("INBOX", "F2", "cal"))][:4] == [
        "INBOX:0",
        "cal:0",
        "F2:0",
        "INBOX:0",
    ]
    assert not [e for e in logs if e["event"] == "live_cycle_failed"]


@pytest.mark.parametrize("status", [401, 429])
def test_a_mailbox_wide_calendar_failure_backs_off_the_cycle(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, status: int
) -> None:
    clock = Clock()
    fake = CalLive(clock)
    fake.cal_fail = status
    with capture_logs() as logs:
        run_live(
            make_graph(fake), db, pool, CADENCE, wait=waiter(clock, [], lambda: clock() >= 30), clock=clock
        )
    assert [e["event"] for e in logs if e["event"] == "live_cycle_failed"]


def test_one_shot_sync_isolates_a_failing_folder(db: sqlite3.Connection, pool: ThreadPoolExecutor) -> None:
    """`sync` gets the per-folder isolation too: the failing folder is counted, the others still sync;
    a mailbox-wide failure still stops the pass."""
    from minimail.folders import sync_folders

    fake = LiveGraph(Clock())
    fake.folders = {"F2": [[]], "INBOX": [[item("n1", "INBOX")]], "F3": [[]]}
    fake.failing = {"F2": 500}
    graph = make_graph(fake)
    folders = sync_folders(graph, db)
    total, failed = live.sync_pass(graph, db, pool, folders)
    assert (failed, total.downloaded) == (1, 1)
    assert [e for _, e in _deltas(fake)] == ["tree", "F2:0", "F3:0", "INBOX:0"]
    fake.failing = {"F2": 429}
    with pytest.raises(httpx.HTTPStatusError):
        live.sync_pass(graph, db, pool, folders)
    fake.failing = {"F2": 502}
    real = fake.__call__

    def outage(req: httpx.Request) -> httpx.Response:
        # Graph down: the probe of the mailbox fails like the folder.
        return httpx.Response(502) if req.url.path.endswith("/mailFolders/inbox") else real(req)

    fake.events.clear()
    with pytest.raises(httpx.HTTPStatusError, match="mailFolders/inbox"):
        live.sync_pass(make_graph(outage), db, pool, folders)
    assert [e for _, e in _deltas(fake)] == ["F2:0"] * 7  # aborted at the first folder
