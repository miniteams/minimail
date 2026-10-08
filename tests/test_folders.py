import sqlite3

import httpx
import pytest
from structlog.testing import capture_logs

from minimail.folders import sync_folders

from .conftest import make_graph

TREE = {
    "me/mailFolders": [
        {"id": "A", "displayName": "Inbox", "childFolderCount": 1, "totalItemCount": 5},
        {"id": "B", "displayName": "Archive", "childFolderCount": 0, "totalItemCount": 2},
    ],
    "me/mailFolders/A/childFolders": [
        {
            "id": "C",
            "displayName": "Project",
            "parentFolderId": "A",
            "childFolderCount": 0,
            "totalItemCount": 1,
        }
    ],
}


# Deleted-items-like gap: folder A holds 5 items of which only 4 are messages.
COUNTS = {"A": "4", "B": "2", "C": "1"}


def _handler(tree: dict[str, list[dict[str, object]]], counted: list[str] | None = None):  # type: ignore[no-untyped-def]
    def handler(req: httpx.Request) -> httpx.Response:
        key = req.url.path.removeprefix("/v1.0/")
        if key.endswith("/messages/$count"):
            folder = key.split("/")[2]
            if counted is not None:
                counted.append(folder)
            return httpx.Response(200, text=COUNTS[folder], headers={"Content-Type": "text/plain"})
        return httpx.Response(200, json={"value": tree[key]})

    return handler


def test_tree_synced_with_paths(db: sqlite3.Connection) -> None:
    """001-AC1 (folder part): every folder and subfolder is stored with its full path and count."""
    rows = sync_folders(make_graph(_handler(TREE)), db)
    assert [(r["path"], r["total_count"]) for r in rows] == [
        ("Archive", 2),
        ("Inbox", 5),
        ("Inbox/Project", 1),
    ]


def test_message_count_is_recorded_beside_the_item_count(db: sqlite3.Connection) -> None:
    """001-AC1: each folder keeps Graph's item count and its messages/$count, which is what the local
    archive must match; an empty folder costs no $count call."""
    empty: dict[str, object] = {"id": "E", "displayName": "Empty"}
    tree = {**TREE, "me/mailFolders": [*TREE["me/mailFolders"], empty]}
    counted: list[str] = []
    rows = sync_folders(make_graph(_handler(tree, counted)), db)
    assert [(r["path"], r["total_count"], r["message_count"]) for r in rows] == [
        ("Archive", 2, 2),
        ("Empty", 0, 0),
        ("Inbox", 5, 4),
        ("Inbox/Project", 1, 1),
    ]
    assert sorted(counted) == ["A", "B", "C"]
    COUNTS["A"] = "5"  # the non-mail item became a message (or a new mail arrived)
    try:
        rows = sync_folders(make_graph(_handler(tree)), db)
    finally:
        COUNTS["A"] = "4"
    assert {r["path"]: r["message_count"] for r in rows}["Inbox"] == 5


@pytest.mark.parametrize("fault", [400, 403, 404, 500, "non_integer"])
def test_a_count_that_fails_keeps_null_and_the_walk_goes_on(db: sqlite3.Connection, fault: int | str) -> None:
    """001-AC1: a $count failing on its own (4xx, 5xx left after retries, a body that is not an
    integer) leaves message_count NULL (logged); the walk completes and the others are counted."""
    base = _handler(TREE)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/A/messages/$count"):
            if isinstance(fault, int):
                return httpx.Response(fault)
            return httpx.Response(200, text="<html>busy</html>")
        return base(req)  # type: ignore[no-any-return]

    with capture_logs() as logs:
        rows = sync_folders(make_graph(handler), db)
    assert {r["path"]: r["message_count"] for r in rows} == {"Archive": 2, "Inbox": None, "Inbox/Project": 1}
    status = fault if isinstance(fault, int) else None
    assert [(e["path"], e["status"]) for e in logs if e["event"] == "message_count_unavailable"] == [
        ("Inbox", status)
    ]


@pytest.mark.parametrize("status", [401, 429])
def test_session_or_throttling_on_count_fails_the_walk_and_keeps_the_count(
    db: sqlite3.Connection, status: int
) -> None:
    """001-AC1: a 401/429 left after retries is about the mailbox, not the count: the walk fails
    (live keeps its previous tree) and the stored count is not overwritten with NULL."""
    sync_folders(make_graph(_handler(TREE)), db)
    base = _handler(TREE)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/A/messages/$count"):
            return httpx.Response(status)
        return base(req)  # type: ignore[no-any-return]

    with pytest.raises(httpx.HTTPStatusError):
        sync_folders(make_graph(handler), db)
    assert db.execute("SELECT message_count FROM folders WHERE id='A'").fetchone()[0] == 4


def test_subfolder_deleted_mid_walk_is_skipped(db: sqlite3.Connection) -> None:
    base = _handler(TREE)

    def handler(req: httpx.Request) -> httpx.Response:
        if "childFolders" in req.url.path:
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        return base(req)  # type: ignore[no-any-return]

    rows = sync_folders(make_graph(handler), db)
    assert [r["path"] for r in rows] == ["Archive", "Inbox"]


def test_root_listing_404_still_fails(db: sqlite3.Connection) -> None:
    with pytest.raises(httpx.HTTPStatusError):
        sync_folders(make_graph(lambda req: httpx.Response(404)), db)


def test_vanished_folder_is_tombstoned_and_revived(db: sqlite3.Connection) -> None:
    sync_folders(make_graph(_handler(TREE)), db)
    smaller = {**TREE, "me/mailFolders": TREE["me/mailFolders"][:1]}
    rows = sync_folders(make_graph(_handler(smaller)), db)
    assert [r["path"] for r in rows] == ["Inbox", "Inbox/Project"]
    assert db.execute("SELECT deleted_at FROM folders WHERE id='B'").fetchone()[0] is not None
    sync_folders(make_graph(_handler(TREE)), db)
    assert db.execute("SELECT deleted_at FROM folders WHERE id='B'").fetchone()[0] is None
