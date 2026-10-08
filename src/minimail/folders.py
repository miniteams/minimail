"""Mail folder tree: re-walked whole on every sync (a few dozen calls) rather than via
`mailFolders/delta`, which returned an empty set on a real mailbox."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from .graph import Graph

log = structlog.get_logger()

_SELECT = "id,displayName,parentFolderId,childFolderCount,totalItemCount"


def walk(graph: Graph) -> Iterator[tuple[dict[str, Any], str]]:
    """Every folder with its `/`-joined path, parents before children."""
    stack: list[tuple[str, str]] = [("me/mailFolders", "")]
    while stack:
        path, prefix = stack.pop()
        try:
            children = list(graph.items(path, **{"$select": _SELECT, "$top": 100}))
        except httpx.HTTPStatusError as exc:
            # Deleted between its parent's listing and this one: the next walk tombstones it.
            if exc.response.status_code != 404 or not prefix:
                raise
            log.info("folder_vanished", path=prefix)
            continue
        for folder in children:
            full = f"{prefix}/{folder['displayName']}" if prefix else folder["displayName"]
            yield folder, full
            if folder.get("childFolderCount"):
                stack.append((f"me/mailFolders/{folder['id']}/childFolders", full))


def sync_folders(graph: Graph, db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Upsert the server tree; folders gone from it are tombstoned, never dropped (their messages
    keep pointing at them). Returns the live folders."""
    now = datetime.now(UTC).isoformat()
    seen = list(walk(graph))
    with db:
        for folder, full in seen:
            db.execute(
                """INSERT INTO folders (id, parent_id, display_name, path, total_count, deleted_at)
                   VALUES (?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(id) DO UPDATE SET parent_id=excluded.parent_id,
                     display_name=excluded.display_name, path=excluded.path,
                     total_count=excluded.total_count, deleted_at=NULL""",
                (
                    folder["id"],
                    folder.get("parentFolderId"),
                    folder["displayName"],
                    full,
                    folder.get("totalItemCount") or 0,
                ),
            )
        ids = [folder["id"] for folder, _ in seen]
        marks = ",".join("?" * len(ids))
        gone = db.execute(
            f"UPDATE folders SET deleted_at=? WHERE deleted_at IS NULL AND id NOT IN ({marks})", (now, *ids)
        ).rowcount
    log.info("folders_synced", live=len(seen), tombstoned=gone)
    return live_folders(db)


def live_folders(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM folders WHERE deleted_at IS NULL ORDER BY path").fetchall()
