"""Delta rounds against a fake Graph: backfill, resume, errors, resync, moves and deletions."""

import hashlib
import json
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator
from compression import zstd
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from minimail import mail
from minimail.auth import AuthExpired, AuthUnavailable
from minimail.mail import local_counts, retry_failed, sync_folder

from .conftest import BASE, make_graph

FOLDER = "F1"


def mime_for(msg_id: str) -> bytes:
    return f"Subject: s-{msg_id}\r\nContent-Type: text/plain\r\n\r\nbody of {msg_id} déjà".encode()


def item(msg_id: str, folder: str = FOLDER) -> dict[str, Any]:
    return {
        "id": msg_id,
        "parentFolderId": folder,
        "subject": f"s-{msg_id}",
        "from": {"emailAddress": {"name": "Ann Example", "address": "ann@example.test"}},
        "toRecipients": [{"emailAddress": {"name": "", "address": "bob@example.test"}}],
        "receivedDateTime": "2026-01-01T00:00:00Z",
    }


class FakeGraph:
    """Serves a two-page delta round (`p1` → `p2` → deltaLink) and each message's $value."""

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.value_calls: Counter[str] = Counter()
        self.urls: list[str] = []
        self.requests: list[httpx.Request] = []
        self.fail_value: dict[str, int] = {}  # id → HTTP status
        self.break_value: set[str] = set()  # ids whose download dies on the wire
        self.raise_value: dict[str, Exception] = {}  # ids whose download raises this
        self.gone_links: set[str] = set()
        self.messages: dict[str, int | str] = {}  # GET /me/messages/{id}: status or parentFolderId (FOLDER)
        self.slow: set[str] = set()  # ids whose download completes last
        self.listing: dict[str, list[str]] = {}  # GET mailFolders/{id}/messages: ids, two per page

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        self.urls.append(url)
        self.requests.append(req)
        path = req.url.path.removeprefix("/v1.0/")
        if path == "me/calendarView/delta":
            return httpx.Response(200, json={"value": [], "@odata.deltaLink": f"{BASE}/{path}?deltatoken=c"})
        if path.endswith("/$value"):
            msg_id = path.split("/")[2]
            self.value_calls[msg_id] += 1
            if msg_id in self.slow:
                time.sleep(0.1)
            if msg_id in self.break_value:
                raise httpx.ConnectError("wire cut")
            if msg_id in self.raise_value:
                raise self.raise_value[msg_id]
            if msg_id in self.fail_value:
                return httpx.Response(self.fail_value[msg_id])
            return httpx.Response(200, content=mime_for(msg_id))
        if path.startswith("me/messages/"):
            state = self.messages.get(path.split("/")[2], FOLDER)
            if isinstance(state, int):
                return httpx.Response(state, json={"error": {"code": "ErrorItemNotFound"}})
            return httpx.Response(200, json={"id": "x", "parentFolderId": state})
        if path.startswith("me/mailFolders/") and path.endswith("/messages"):
            ids, skip = self.listing.get(path.split("/")[2], []), int(req.url.params.get("skip", "0"))
            listed: dict[str, Any] = {"value": [{"id": i} for i in ids[skip : skip + 2]]}
            if skip + 2 < len(ids):
                listed["@odata.nextLink"] = f"{BASE}/{path}?skip={skip + 2}"
            return httpx.Response(200, json=listed)
        page = int(req.url.params.get("page", "0"))
        if url in self.gone_links:
            return httpx.Response(410, json={"error": {"code": "syncStateNotFound"}})
        body: dict[str, Any] = {"value": self.pages[page]}
        if page + 1 < len(self.pages):
            body["@odata.nextLink"] = f"{BASE}/me/mailFolders/{FOLDER}/messages/delta?page={page + 1}"
        else:
            body["@odata.deltaLink"] = f"{BASE}/me/mailFolders/{FOLDER}/messages/delta?deltatoken=t"
        return httpx.Response(200, json=body)


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(2) as executor:
        yield executor


def test_backfill_stores_verbatim_mime_and_cursor(db: sqlite3.Connection, pool: ThreadPoolExecutor) -> None:
    """001-AC1 (messages) + 001-AC2: every message stored, MIME byte-identical, sha256 recorded."""
    fake = FakeGraph([[item("m1"), item("m2")], [item("m3")]])
    c = sync_folder(make_graph(fake), db, pool, FOLDER)
    assert (c.listed, c.downloaded, c.failed) == (3, 3, 0)
    for row in db.execute("SELECT id, mime, mime_sha256, mime_size, search_text, sender FROM messages"):
        raw = zstd.decompress(row["mime"])
        assert raw == mime_for(row["id"])
        assert row["mime_sha256"] == hashlib.sha256(raw).hexdigest()
        assert row["mime_size"] == len(raw)
        assert "déjà" in row["search_text"]
        assert row["sender"] == "Ann Example <ann@example.test>"
    assert local_counts(db) == {FOLDER: 3}
    state = db.execute("SELECT next_link, delta_link FROM sync_state").fetchone()
    assert state["next_link"] is None and "deltatoken" in state["delta_link"]


def test_ids_are_path_encoded(db: sqlite3.Connection, pool: ThreadPoolExecutor) -> None:
    fake = FakeGraph([[item("a+b=")]])
    sync_folder(make_graph(fake), db, pool, FOLDER)
    assert any("/me/messages/a%2Bb=/$value" in u for u in fake.urls)


def test_crash_mid_round_resumes_without_redownload(db: sqlite3.Connection, pool: ThreadPoolExecutor) -> None:
    """001-AC3: a round dying mid-page resumes from its cursor; stored MIME is never fetched again."""
    fake = FakeGraph([[item("m1"), item("m2")], [item("m3"), item("m4")]])
    fake.break_value = {"m4"}
    graph = make_graph(fake)
    with pytest.raises(httpx.ConnectError):
        sync_folder(graph, db, pool, FOLDER)
    assert "page=1" in db.execute("SELECT next_link FROM sync_state").fetchone()[0]
    fake.break_value = set()
    fake.urls.clear()
    sync_folder(graph, db, pool, FOLDER)
    assert all("page=1" in u or "$value" in u for u in fake.urls), "resumed from page 2, not page 1"
    assert set(fake.value_calls) == {"m1", "m2", "m3", "m4"}
    assert fake.value_calls["m4"] == 2 and all(fake.value_calls[m] == 1 for m in ("m1", "m2", "m3"))
    assert db.execute("SELECT COUNT(*) FROM messages WHERE mime IS NOT NULL").fetchone()[0] == 4


def test_rerun_after_complete_round_downloads_nothing(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """001-AC3: idempotent re-run (here the fake replays the same items on the delta link)."""
    fake = FakeGraph([[item("m1")]])
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    assert sync_folder(graph, db, pool, FOLDER).downloaded == 0
    assert fake.value_calls["m1"] == 1


@pytest.mark.parametrize("status", [400, 404])
def test_unfetchable_mime_is_recorded_not_retried(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, status: int
) -> None:
    fake = FakeGraph([[item("m1"), item("m2")]])
    fake.fail_value = {"m2": status}
    graph = make_graph(fake)
    assert sync_folder(graph, db, pool, FOLDER).failed == 1
    row = db.execute("SELECT mime, mime_error, subject FROM messages WHERE id='m2'").fetchone()
    assert row["mime"] is None and row["mime_error"] == f"http {status}" and row["subject"] == "s-m2"
    sync_folder(graph, db, pool, FOLDER)
    assert fake.value_calls["m2"] == 1


def test_expired_cursor_restarts_round_without_redownload(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    fake = FakeGraph([[item("m1")]])
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    delta = db.execute("SELECT delta_link FROM sync_state").fetchone()[0]
    fake.gone_links = {delta}
    c = sync_folder(graph, db, pool, FOLDER)
    assert c.listed == 1 and c.downloaded == 0
    assert fake.value_calls["m1"] == 1
    assert db.execute("SELECT delta_link FROM sync_state").fetchone()[0] == delta  # fresh round, new link


def test_fresh_round_error_is_not_swallowed(db: sqlite3.Connection, pool: ThreadPoolExecutor) -> None:
    graph = make_graph(lambda req: httpx.Response(410, json={"error": {"code": "syncStateNotFound"}}))
    with pytest.raises(httpx.HTTPStatusError):
        sync_folder(graph, db, pool, FOLDER)


def test_removed_then_404_is_tombstoned_not_dropped(db: sqlite3.Connection, pool: ThreadPoolExecutor) -> None:
    """001-AC5: a server deletion keeps the local copy, marked deleted."""
    fake = FakeGraph([[item("m1")]])
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    fake.pages = [[{"id": "m1", "@removed": {"reason": "deleted"}}]]
    fake.messages = {"m1": 404}
    assert sync_folder(graph, db, pool, FOLDER).deleted == 1
    row = db.execute("SELECT deleted_at, mime FROM messages WHERE id='m1'").fetchone()
    assert row["deleted_at"] is not None and row["mime"] is not None
    assert local_counts(db) == {}


def _visible_folders(db: sqlite3.Connection, *ids: str, gone: str | None = None) -> None:
    with db:
        for fid in ids:
            db.execute(
                "INSERT INTO folders (id, display_name, path, deleted_at) VALUES (?, ?, ?, ?)",
                (fid, fid, fid, "2026-01-01T00:00:00+00:00" if fid == gone else None),
            )


def test_removed_but_alive_elsewhere_is_a_move(db: sqlite3.Connection, pool: ThreadPoolExecutor) -> None:
    """001-AC5: a move to a folder of the visible tree updates the folder and never refetches the MIME."""
    _visible_folders(db, FOLDER, "F2")
    fake = FakeGraph([[item("m1")]])
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    fake.pages = [[{"id": "m1", "@removed": {"reason": "deleted"}}]]
    fake.messages = {"m1": "F2"}
    assert sync_folder(graph, db, pool, FOLDER).moved == 1
    assert db.execute("SELECT folder_id, deleted_at FROM messages").fetchone()[:] == ("F2", None)
    assert fake.value_calls["m1"] == 1


@pytest.mark.parametrize("parent", ["RECOVERABLE", "GONE"])
def test_removed_and_parked_outside_the_visible_tree_is_a_deletion(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, parent: str
) -> None:
    """001-AC5: a soft delete leaves the message readable (GET 200) under a folder outside the visible
    tree (hidden Recoverable Items, or a tombstoned folder): tombstoned like a 404, last visible
    folder kept, MIME kept and not refetched."""
    _visible_folders(db, FOLDER, "GONE", gone="GONE")
    fake = FakeGraph([[item("m1")]])
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    fake.pages = [[{"id": "m1", "@removed": {"reason": "deleted"}}]]
    fake.messages = {"m1": parent}
    c = sync_folder(graph, db, pool, FOLDER)
    assert (c.deleted, c.moved) == (1, 0)
    row = db.execute("SELECT folder_id, deleted_at, mime FROM messages WHERE id='m1'").fetchone()
    assert row["folder_id"] == FOLDER and row["deleted_at"] is not None and row["mime"] is not None
    assert fake.value_calls["m1"] == 1


def test_move_seen_first_in_destination_is_not_tombstoned(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """001-AC5: destination folder's delta already applied the move; the source's @removed is a no-op."""
    sync_folder(make_graph(FakeGraph([[item("m1", "F2")]])), db, pool, "F2")
    fake = FakeGraph([[{"id": "m1", "@removed": {"reason": "deleted"}}]])
    fake.messages = {"m1": 404}  # even if the lookup misfires, F1 no longer owns it
    c = sync_folder(make_graph(fake), db, pool, FOLDER)
    assert (c.deleted, c.moved) == (0, 0)
    assert db.execute("SELECT folder_id, deleted_at FROM messages").fetchone()[:] == ("F2", None)


@pytest.mark.parametrize("status", [401, 429, 500, 503])
def test_transient_failure_is_retried_not_recorded(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, status: int
) -> None:
    """001-AC11/AC3: a status left after the client's retries stops the round without marking the
    message failed; the page's other downloads are kept; the next run fetches the missing one."""
    fake = FakeGraph([[item("m1"), item("m2")]])
    fake.fail_value = {"m2": status}
    fake.slow = {"m1"}  # the failure surfaces first: m1 must still be stored after it
    graph = make_graph(fake)
    with pytest.raises(httpx.HTTPStatusError):
        sync_folder(graph, db, pool, FOLDER)
    rows = dict(db.execute("SELECT id, mime IS NOT NULL FROM messages").fetchall())
    assert rows == {"m1": 1, "m2": 0}
    assert db.execute("SELECT mime_error FROM messages WHERE id='m2'").fetchone()[0] is None
    fake.fail_value = {}
    sync_folder(graph, db, pool, FOLDER)
    assert db.execute("SELECT COUNT(*) FROM messages WHERE mime IS NOT NULL").fetchone()[0] == 2
    assert fake.value_calls["m1"] == 1


def test_retry_failed_is_caught_up_by_an_empty_incremental_round(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    fake = FakeGraph([[item("m1")]])
    fake.fail_value = {"m1": 404}
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    fake.fail_value = {}
    fake.pages = [[]]  # the incremental round lists nothing: the message did not change
    assert sync_folder(graph, db, pool, FOLDER).downloaded == 0
    assert retry_failed(db) == 1
    assert sync_folder(graph, db, pool, FOLDER).downloaded == 1
    assert db.execute("SELECT mime IS NOT NULL, mime_error FROM messages").fetchone()[:] == (1, None)


def test_metadata_only_change_updates_meta_without_refetch(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """001-AC6: read, flag and category changes land in `meta`; the MIME is not downloaded again."""
    fake = FakeGraph([[item("m1")]])
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    stored = db.execute("SELECT mime FROM messages").fetchone()[0]
    changed = {**item("m1"), "isRead": True, "flag": {"flagStatus": "flagged"}, "categories": ["Blue"]}
    fake.pages = [[changed]]
    fake.urls.clear()
    c = sync_folder(graph, db, pool, FOLDER)
    assert (c.listed, c.downloaded) == (1, 0)
    assert not [u for u in fake.urls if "$value" in u]
    row = db.execute("SELECT meta, mime FROM messages WHERE id='m1'").fetchone()
    meta = json.loads(row["meta"])
    assert (meta["isRead"], meta["flag"], meta["categories"]) == (True, {"flagStatus": "flagged"}, ["Blue"])
    assert row["mime"] == stored


def test_paused_round_still_progresses_and_resumes(db: sqlite3.Connection, pool: ThreadPoolExecutor) -> None:
    """A round asked to pause at once still applies its first page, saves its cursor, and the next
    call resumes from it."""
    fake = FakeGraph([[item("m1")], [item("m2")]])
    graph = make_graph(fake)
    c = sync_folder(graph, db, pool, FOLDER, pause=lambda: True)
    assert (c.paused, c.listed) == (True, 1)
    assert "page=1" in db.execute("SELECT next_link FROM sync_state").fetchone()[0]
    c = sync_folder(graph, db, pool, FOLDER, pause=lambda: True)
    assert (c.paused, c.listed) == (False, 1)
    assert set(fake.value_calls) == {"m1", "m2"}


@pytest.mark.parametrize(
    "error", [AuthUnavailable("aad busy"), AuthExpired("signed out"), httpx.ReadTimeout("stuck")]
)
def test_one_failing_download_keeps_the_others(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, error: Exception
) -> None:
    """A worker failing to get a token, or stuck reading one body, fails its own download only: the
    page's other MIME are stored before the error stops the round."""
    ids = [f"m{i}" for i in range(8)]
    fake = FakeGraph([[item(i) for i in ids]])
    fake.raise_value = {"m0": error}
    fake.slow = set(ids[1:])  # the failure surfaces first
    with pytest.raises(type(error)):
        sync_folder(make_graph(fake), db, pool, FOLDER)
    assert db.execute("SELECT COUNT(*) FROM messages WHERE mime IS NOT NULL").fetchone()[0] == 7


def test_mime_catch_up_yields_in_page_sized_chunks(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-of-round catch-up stops between chunks when asked to; the next round fetches the rest."""
    monkeypatch.setattr(mail, "PAGE_SIZE", 2)
    fake = FakeGraph([[item(f"m{i}") for i in range(5)]])
    fake.fail_value = {f"m{i}": 404 for i in range(5)}
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    fake.fail_value, fake.pages = {}, [[]]
    retry_failed(db)
    answers = iter([False, True])
    c = sync_folder(graph, db, pool, FOLDER, pause=lambda: next(answers))
    assert (c.paused, c.downloaded) == (True, 2)
    c = sync_folder(graph, db, pool, FOLDER, pause=lambda: False)
    assert (c.paused, c.downloaded) == (False, 3)


def test_move_into_a_folder_newer_than_the_tree_heals_on_its_first_round(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """001-AC5: a move into a folder the last tree walk has not seen yet is first taken for a deletion;
    the new folder's first round lists the message, which revives it there without a refetch."""
    _visible_folders(db, FOLDER)
    fake = FakeGraph([[item("m1")]])
    sync_folder(make_graph(fake), db, pool, FOLDER)
    fake.pages = [[{"id": "m1", "@removed": {"reason": "deleted"}}]]
    fake.messages = {"m1": "NEW"}
    assert sync_folder(make_graph(fake), db, pool, FOLDER).deleted == 1
    _visible_folders(db, "NEW")  # the next tree walk
    sync_folder(make_graph(FakeGraph([[item("m1", "NEW")]])), db, pool, "NEW")
    assert db.execute("SELECT folder_id, deleted_at FROM messages").fetchone()[:] == ("NEW", None)
    assert fake.value_calls["m1"] == 1


def test_item_whose_value_and_lookup_both_404_is_a_tombstone(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """001-AC1: Deleted Items' delta lists non-mail items that /messages does not ($value and GET both
    404); they never count as live messages, even when a fresh round lists them again."""
    fake = FakeGraph([[item("m1"), item("p1")]])
    fake.fail_value = {"p1": 404}
    fake.messages = {"p1": 404}
    graph = make_graph(fake)
    with capture_logs() as logs:
        assert sync_folder(graph, db, pool, FOLDER).deleted == 1
    assert {"event": "item_gone", "folder_id": FOLDER, "log_level": "warning"} in logs
    assert local_counts(db) == {FOLDER: 1}
    row = db.execute("SELECT deleted_at, mime, mime_error FROM messages WHERE id='p1'").fetchone()
    assert row["deleted_at"] is not None and row["mime"] is None and row["mime_error"] is None
    with db:
        db.execute("DELETE FROM sync_state")
    sync_folder(graph, db, pool, FOLDER)
    assert local_counts(db) == {FOLDER: 1}
    assert fake.value_calls == Counter({"m1": 1, "p1": 2})


def test_reconcile_catches_deletions_and_moves_delta_never_reported(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """001-AC5: delta never reports a soft delete out of Deleted Items; a local message the folder no
    longer lists is tombstoned (404 or parked outside the visible tree) or moved, MIME kept, never
    refetched; a server message not stored yet is only counted (the delta owns additions)."""
    _visible_folders(db, FOLDER, "F2")
    fake = FakeGraph([[item(m) for m in ("m1", "m2", "m3", "m4")]])
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    fake.listing = {FOLDER: ["m1", "new"]}
    fake.messages = {"m2": 404, "m3": "RECOVERABLE", "m4": "F2"}
    c = mail.reconcile_folder(graph, db, FOLDER)
    assert c.summary() == {"listed": 2, "moved": 1, "deleted": 2, "revived": 0, "unsynced": 1}
    rows = {
        r["id"]: (r["folder_id"], r["deleted_at"] is not None) for r in db.execute("SELECT * FROM messages")
    }
    assert rows == {"m1": (FOLDER, False), "m2": (FOLDER, True), "m3": (FOLDER, True), "m4": ("F2", False)}
    assert db.execute("SELECT COUNT(*) FROM messages WHERE mime IS NULL").fetchone()[0] == 0
    assert set(fake.value_calls.values()) == {1} and "new" not in fake.value_calls
    assert not any("me/messages/m1?" in u for u in fake.urls)


def test_reconcile_yields_after_each_page_and_each_chunk_of_lookups(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """001-AC4: each unit between two yields is one listing page or at most PAGE_SIZE lookups."""
    monkeypatch.setattr(mail, "PAGE_SIZE", 2)
    fake = FakeGraph([[item(m) for m in ("m1", "m2", "m3")]])
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    fake.listing = {FOLDER: ["x1", "x2", "x3", "x4", "x5"]}
    fake.messages = {"m1": 404, "m2": 404, "m3": 404}
    base = len(fake.urls)
    marks = [len(fake.urls) - base for _ in mail.reconcile_steps(graph, db, FOLDER, mail.Reconciled())]
    assert marks == [1, 2, 3, 5]
    assert len(fake.urls) - base == 6 and local_counts(db) == {}


def test_reconcile_lists_ids_in_pages_of_1000_with_immutable_ids(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """Listing is latency-bound: every listing page asks for 1000 ids, still as immutable ids."""
    fake = FakeGraph([[]])
    fake.listing = {FOLDER: ["m1", "m2", "m3"]}
    mail.reconcile_folder(make_graph(fake), db, FOLDER)
    listing = [r for r in fake.requests if r.url.path.endswith(f"/mailFolders/{FOLDER}/messages")]
    assert len(listing) == 2
    assert listing[0].url.params["$top"] == "1000"
    for req in listing:
        assert req.headers["Prefer"] == 'IdType="ImmutableId", odata.maxpagesize=1000'


def test_reconcile_revives_a_tombstone_the_server_still_lists(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """001-AC5, 001-AC1: a false tombstone (a transient double 404) the server still lists in this
    folder is live again and its MIME is fetched by the next round; one listed elsewhere, or stored
    in another folder, stays as it is and is not counted as unsynced."""
    fake = FakeGraph([[item("m1"), item("m2"), item("m3")]])
    fake.fail_value = {"m2": 404, "m3": 404}
    fake.messages = {"m2": 404, "m3": 404}
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    sync_folder(make_graph(FakeGraph([[item("e1", "F2")]])), db, pool, "F2")
    assert local_counts(db) == {FOLDER: 1, "F2": 1}
    fake.fail_value, fake.pages = {}, [[]]
    fake.listing = {FOLDER: ["m1", "m2", "m3", "e1"]}
    fake.messages = {"m3": "F2"}
    c = mail.reconcile_folder(graph, db, FOLDER)
    assert (c.revived, c.unsynced, c.deleted) == (1, 0, 0)
    gone = {r[0] for r in db.execute("SELECT id FROM messages WHERE deleted_at IS NOT NULL")}
    assert gone == {"m3"}
    sync_folder(graph, db, pool, FOLDER)
    assert db.execute("SELECT mime IS NOT NULL FROM messages WHERE id='m2'").fetchone()[0] == 1
    assert fake.value_calls["m2"] == 2 and fake.value_calls["m3"] == 1
