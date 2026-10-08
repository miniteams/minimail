import sqlite3

import httpx

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


def _handler(tree: dict[str, list[dict[str, object]]]):  # type: ignore[no-untyped-def]
    def handler(req: httpx.Request) -> httpx.Response:
        key = req.url.path.removeprefix("/v1.0/")
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


def test_subfolder_deleted_mid_walk_is_skipped(db: sqlite3.Connection) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "childFolders" in req.url.path:
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        return httpx.Response(200, json={"value": TREE["me/mailFolders"]})

    rows = sync_folders(make_graph(handler), db)
    assert [r["path"] for r in rows] == ["Archive", "Inbox"]


def test_root_listing_404_still_fails(db: sqlite3.Connection) -> None:
    import pytest

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
