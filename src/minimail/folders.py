"""Mail folder tree: re-walked whole on every sync (a few dozen calls) rather than via
`mailFolders/delta`, which returned an empty set on a real mailbox."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from .graph import SESSION_STATUSES, Graph
from .mail import path_id

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
                stack.append((f"me/mailFolders/{path_id(folder['id'])}/childFolders", full))


def message_count(graph: Graph, folder: dict[str, Any], path: str) -> int | None:
    """Messages only: totalItemCount also counts the non-mail items Deleted Items collects (events,
    contacts, tasks). None if this count fails (it only feeds the display): the walk goes on."""
    if not folder.get("totalItemCount"):
        return 0
    try:
        return int(graph.request("GET", f"me/mailFolders/{path_id(folder['id'])}/messages/$count").text)
    except (httpx.HTTPStatusError, ValueError) as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        # Session/throttling: fail the walk so a known count is not overwritten with NULL.
        if status in SESSION_STATUSES:
            raise
        log.warning("message_count_unavailable", path=path, error=type(exc).__name__, status=status)
        return None


def sync_folders(graph: Graph, db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Upsert the server tree; folders gone from it are tombstoned, never dropped (their messages
    keep pointing at them). Returns the live folders."""
    now = datetime.now(UTC).isoformat()
    # Counted before the transaction: no write lock held across network calls.
    seen = [(folder, full, message_count(graph, folder, full)) for folder, full in walk(graph)]
    with db:
        for folder, full, messages in seen:
            db.execute(
                """INSERT INTO folders (id, parent_id, display_name, path, total_count, message_count,
                     deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(id) DO UPDATE SET parent_id=excluded.parent_id,
                     display_name=excluded.display_name, path=excluded.path,
                     total_count=excluded.total_count, message_count=excluded.message_count,
                     deleted_at=NULL""",
                (
                    folder["id"],
                    folder.get("parentFolderId"),
                    folder["displayName"],
                    full,
                    folder.get("totalItemCount") or 0,
                    messages,
                ),
            )
        ids = [folder["id"] for folder, _, _ in seen]
        marks = ",".join("?" * len(ids))
        gone = db.execute(
            f"UPDATE folders SET deleted_at=? WHERE deleted_at IS NULL AND id NOT IN ({marks})", (now, *ids)
        ).rowcount
    log.info("folders_synced", live=len(seen), tombstoned=gone)
    return live_folders(db)


def live_folders(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM folders WHERE deleted_at IS NULL ORDER BY path").fetchall()
