"""Delta rounds against a fake Graph: backfill, resume, errors, resync, moves and deletions."""

import hashlib
import time
from collections import Counter
from collections.abc import Iterator
from compression import zstd
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest

from minimail.mail import local_counts, sync_folder
from minimail.store import open_db

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
        self.fail_value: dict[str, int] = {}  # id → HTTP status
        self.break_value: set[str] = set()  # ids whose download dies on the wire
        self.gone_links: set[str] = set()
        self.messages: dict[str, int | str] = {}  # GET /me/messages/{id}: status or parentFolderId
        self.slow: set[str] = set()  # ids whose download completes last

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        self.urls.append(url)
        path = req.url.path.removeprefix("/v1.0/")
        if path.endswith("/$value"):
            msg_id = path.split("/")[2]
            self.value_calls[msg_id] += 1
            if msg_id in self.slow:
                time.sleep(0.1)
            if msg_id in self.break_value:
                raise httpx.ConnectError("wire cut")
            if msg_id in self.fail_value:
                return httpx.Response(self.fail_value[msg_id])
            return httpx.Response(200, content=mime_for(msg_id))
        if path.startswith("me/messages/"):
            state = self.messages[path.split("/")[2]]
            if isinstance(state, int):
                return httpx.Response(state, json={"error": {"code": "ErrorItemNotFound"}})
            return httpx.Response(200, json={"id": "x", "parentFolderId": state})
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


def test_backfill_stores_verbatim_mime_and_cursor(tmp_path: Path, pool: ThreadPoolExecutor) -> None:
    """001-AC1 (messages) + 001-AC2: every message stored, MIME byte-identical, sha256 recorded."""
    fake = FakeGraph([[item("m1"), item("m2")], [item("m3")]])
    db = open_db(tmp_path)
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


def test_ids_are_path_encoded(tmp_path: Path, pool: ThreadPoolExecutor) -> None:
    fake = FakeGraph([[item("a+b=")]])
    sync_folder(make_graph(fake), open_db(tmp_path), pool, FOLDER)
    assert any("/me/messages/a%2Bb=/$value" in u for u in fake.urls)


def test_crash_mid_round_resumes_without_redownload(tmp_path: Path, pool: ThreadPoolExecutor) -> None:
    """001-AC3: a round dying mid-page resumes from its cursor; stored MIME is never fetched again."""
    fake = FakeGraph([[item("m1"), item("m2")], [item("m3"), item("m4")]])
    fake.break_value = {"m4"}
    db = open_db(tmp_path)
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


def test_rerun_after_complete_round_downloads_nothing(tmp_path: Path, pool: ThreadPoolExecutor) -> None:
    """001-AC3: idempotent re-run (here the fake replays the same items on the delta link)."""
    fake = FakeGraph([[item("m1")]])
    db = open_db(tmp_path)
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    assert sync_folder(graph, db, pool, FOLDER).downloaded == 0
    assert fake.value_calls["m1"] == 1


@pytest.mark.parametrize("status", [400, 404])
def test_unfetchable_mime_is_recorded_not_retried(
    tmp_path: Path, pool: ThreadPoolExecutor, status: int
) -> None:
    fake = FakeGraph([[item("m1"), item("m2")]])
    fake.fail_value = {"m2": status}
    db = open_db(tmp_path)
    graph = make_graph(fake)
    assert sync_folder(graph, db, pool, FOLDER).failed == 1
    row = db.execute("SELECT mime, mime_error, subject FROM messages WHERE id='m2'").fetchone()
    assert row["mime"] is None and row["mime_error"] == f"http {status}" and row["subject"] == "s-m2"
    sync_folder(graph, db, pool, FOLDER)
    assert fake.value_calls["m2"] == 1


def test_expired_cursor_restarts_round_without_redownload(tmp_path: Path, pool: ThreadPoolExecutor) -> None:
    fake = FakeGraph([[item("m1")]])
    db = open_db(tmp_path)
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    delta = db.execute("SELECT delta_link FROM sync_state").fetchone()[0]
    fake.gone_links = {delta}
    c = sync_folder(graph, db, pool, FOLDER)
    assert c.listed == 1 and c.downloaded == 0
    assert fake.value_calls["m1"] == 1
    assert db.execute("SELECT delta_link FROM sync_state").fetchone()[0] == delta  # fresh round, new link


def test_fresh_round_error_is_not_swallowed(tmp_path: Path, pool: ThreadPoolExecutor) -> None:
    graph = make_graph(lambda req: httpx.Response(410, json={"error": {"code": "syncStateNotFound"}}))
    with pytest.raises(httpx.HTTPStatusError):
        sync_folder(graph, open_db(tmp_path), pool, FOLDER)


def test_removed_then_404_is_tombstoned_not_dropped(tmp_path: Path, pool: ThreadPoolExecutor) -> None:
    """001-AC5: a server deletion keeps the local copy, marked deleted."""
    fake = FakeGraph([[item("m1")]])
    db = open_db(tmp_path)
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    fake.pages = [[{"id": "m1", "@removed": {"reason": "deleted"}}]]
    fake.messages = {"m1": 404}
    assert sync_folder(graph, db, pool, FOLDER).deleted == 1
    row = db.execute("SELECT deleted_at, mime FROM messages WHERE id='m1'").fetchone()
    assert row["deleted_at"] is not None and row["mime"] is not None
    assert local_counts(db) == {}


def test_removed_but_alive_elsewhere_is_a_move(tmp_path: Path, pool: ThreadPoolExecutor) -> None:
    """001-AC5: a move updates the folder and never refetches the MIME."""
    fake = FakeGraph([[item("m1")]])
    db = open_db(tmp_path)
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    fake.pages = [[{"id": "m1", "@removed": {"reason": "deleted"}}]]
    fake.messages = {"m1": "F2"}
    assert sync_folder(graph, db, pool, FOLDER).moved == 1
    assert db.execute("SELECT folder_id, deleted_at FROM messages").fetchone()[:] == ("F2", None)
    assert fake.value_calls["m1"] == 1


def test_move_seen_first_in_destination_is_not_tombstoned(tmp_path: Path, pool: ThreadPoolExecutor) -> None:
    """001-AC5: destination folder's delta already applied the move; the source's @removed is a no-op."""
    db = open_db(tmp_path)
    sync_folder(make_graph(FakeGraph([[item("m1", "F2")]])), db, pool, "F2")
    fake = FakeGraph([[{"id": "m1", "@removed": {"reason": "deleted"}}]])
    fake.messages = {"m1": 404}  # even if the lookup misfires, F1 no longer owns it
    c = sync_folder(make_graph(fake), db, pool, FOLDER)
    assert (c.deleted, c.moved) == (0, 0)
    assert db.execute("SELECT folder_id, deleted_at FROM messages").fetchone()[:] == ("F2", None)


@pytest.mark.parametrize("status", [401, 429, 500, 503])
def test_transient_failure_is_retried_not_recorded(
    tmp_path: Path, pool: ThreadPoolExecutor, status: int
) -> None:
    """001-AC11/AC3: a status left after the client's retries stops the round without marking the
    message failed; the page's other downloads are kept; the next run fetches the missing one."""
    fake = FakeGraph([[item("m1"), item("m2")]])
    fake.fail_value = {"m2": status}
    fake.slow = {"m1"}  # the failure surfaces first: m1 must still be stored after it
    db = open_db(tmp_path)
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
    tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    from minimail.mail import retry_failed

    fake = FakeGraph([[item("m1")]])
    fake.fail_value = {"m1": 404}
    db = open_db(tmp_path)
    graph = make_graph(fake)
    sync_folder(graph, db, pool, FOLDER)
    fake.fail_value = {}
    fake.pages = [[]]  # the incremental round lists nothing: the message did not change
    assert sync_folder(graph, db, pool, FOLDER).downloaded == 0
    assert retry_failed(db) == 1
    assert sync_folder(graph, db, pool, FOLDER).downloaded == 1
    assert db.execute("SELECT mime IS NOT NULL, mime_error FROM messages").fetchone()[:] == (1, None)
