"""Message mirror: one Graph delta round per folder. The first round is the backfill, every later
one is incremental — same code, so a crash anywhere resumes the same way.

Durability: metadata of a page commits first, then each MIME as it lands, and the page cursor last.
A crash re-reads at most one page; messages whose MIME is already stored are never re-downloaded.
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from compression import zstd
from concurrent.futures import Executor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from .graph import Graph
from .mime import Extracted, extract

log = structlog.get_logger()

SELECT = (
    "id,parentFolderId,internetMessageId,subject,from,toRecipients,ccRecipients,receivedDateTime,"
    "sentDateTime,isRead,flag,categories,conversationId,hasAttachments,importance,isDraft,lastModifiedDateTime"
)
# 4xx about the item itself; 401/408/429 are about the session or the moment.
_PERMANENT = frozenset({400, 403, 404, 406, 410, 413, 415, 422})
# Graph allows 4 concurrent requests per mailbox per app; more only buys 429s.
DOWNLOAD_WORKERS = 4
# Delta cursors Graph no longer honours: start the round over (stored MIME is not refetched).
_RESYNC_CODES = frozenset(
    {"syncStateNotFound", "resyncRequired", "SyncStateNotFound", "ErrorInvalidSyncState"}
)


@dataclass
class Counters:
    listed: int = 0
    downloaded: int = 0
    failed: int = 0
    moved: int = 0
    deleted: int = 0


@dataclass
class Stored:
    id: str
    blob: bytes | None = None
    sha256: str | None = None
    size: int | None = None
    text: str | None = None
    error: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def path_id(msg_id: str) -> str:
    return quote(msg_id, safe="=-_")


def _address(entry: dict[str, Any] | None) -> str:
    addr = (entry or {}).get("emailAddress") or {}
    name, mail = addr.get("name") or "", addr.get("address") or ""
    return f"{name} <{mail}>" if name and mail and name != mail else name or mail


def fetch_mime(graph: Graph, msg_id: str) -> Stored:
    """Download the verbatim MIME and derive what the archive keeps. Runs in a worker thread.
    An HTTP error status is recorded on the message; transport errors propagate (the round stops
    and resumes later)."""
    try:
        raw = graph.request("GET", f"me/messages/{path_id(msg_id)}/$value").content
    except httpx.HTTPStatusError as exc:
        # Only a verdict on the item itself is final; throttling, auth and 5xx left after the
        # client's own retries stop the round so a later one fetches it.
        if exc.response.status_code not in _PERMANENT:
            raise
        return Stored(msg_id, error=f"http {exc.response.status_code}")
    try:
        text = extract(raw).text
    except Exception:  # noqa: BLE001 — unparseable MIME is still archived verbatim
        text = Extracted().text
    return Stored(msg_id, zstd.compress(raw), hashlib.sha256(raw).hexdigest(), len(raw), text)


def _upsert_meta(db: sqlite3.Connection, folder_id: str, item: dict[str, Any], now: str) -> None:
    recipients = ", ".join(
        _address(r) for r in (item.get("toRecipients") or []) + (item.get("ccRecipients") or [])
    )
    db.execute(
        """INSERT INTO messages (id, folder_id, internet_message_id, received_at, subject, sender,
             recipients, meta, first_seen_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET folder_id=excluded.folder_id,
             internet_message_id=excluded.internet_message_id, received_at=excluded.received_at,
             subject=excluded.subject, sender=excluded.sender, recipients=excluded.recipients,
             meta=excluded.meta, updated_at=excluded.updated_at, deleted_at=NULL""",
        (
            item["id"],
            item.get("parentFolderId") or folder_id,
            item.get("internetMessageId"),
            item.get("receivedDateTime"),
            item.get("subject") or "",
            _address(item.get("from")),
            recipients,
            json.dumps(item, ensure_ascii=False),
            now,
            now,
        ),
    )


def _handle_removed(graph: Graph, db: sqlite3.Connection, folder_id: str, msg_id: str, c: Counters) -> None:
    """Delta reports both moves and deletions as `@removed`; ask Graph which one it was."""
    try:
        current = graph.get(f"me/messages/{path_id(msg_id)}", **{"$select": "id,parentFolderId"})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        with db:
            # Only if it still sits in the folder that reported it: a move already applied wins.
            hit = db.execute(
                "UPDATE messages SET deleted_at=?, updated_at=? "
                "WHERE id=? AND folder_id=? AND deleted_at IS NULL",
                (_now(), _now(), msg_id, folder_id),
            ).rowcount
        c.deleted += hit
        return
    with db:
        hit = db.execute(
            "UPDATE messages SET folder_id=?, updated_at=?, deleted_at=NULL WHERE id=? AND folder_id!=?",
            (current["parentFolderId"], _now(), msg_id, current["parentFolderId"]),
        ).rowcount
    c.moved += hit


def _store_downloads(
    graph: Graph, db: sqlite3.Connection, executor: Executor, ids: Iterable[str], c: Counters
) -> None:
    futures = {executor.submit(fetch_mime, graph, msg_id) for msg_id in ids}
    # as_completed drops its reference to each future it yields; ours must go too, or every MIME of
    # the page stays in memory until the page ends.
    completed = as_completed(futures)
    del futures
    # A failed download fails itself, not the page: keep storing the others, then raise so the
    # round stops and its cursor stays put. Queued downloads die with the caller's executor.
    broken: list[httpx.HTTPError] = []
    for future in completed:
        try:
            s = future.result()
        except httpx.HTTPError as exc:
            broken.append(exc)
            continue
        with db:
            db.execute(
                """UPDATE messages SET mime=?, mime_sha256=?, mime_size=?, mime_error=?, search_text=?
                   WHERE id=?""",
                (s.blob, s.sha256, s.size, s.error, s.text, s.id),
            )
        if s.error:
            c.failed += 1
            log.warning("mime_unavailable", error=s.error)
        else:
            c.downloaded += 1
        del s, future
    if broken:
        raise broken[0]


def _apply_page(
    graph: Graph,
    db: sqlite3.Connection,
    executor: Executor,
    folder_id: str,
    items: list[dict[str, Any]],
    c: Counters,
) -> None:
    now = _now()
    live = [i for i in items if "@removed" not in i]
    with db:
        for item in live:
            _upsert_meta(db, folder_id, item, now)
    c.listed += len(live)
    for item in items:
        if "@removed" in item:
            _handle_removed(graph, db, folder_id, item["id"], c)
    if live:
        marks = ",".join("?" * len(live))
        need = [
            row[0]
            for row in db.execute(
                f"SELECT id FROM messages WHERE id IN ({marks}) AND mime IS NULL AND mime_error IS NULL",
                [i["id"] for i in live],
            )
        ]
        _store_downloads(graph, db, executor, need, c)


def _save_cursor(db: sqlite3.Connection, key: str, next_link: str | None, delta_link: str | None) -> None:
    with db:
        db.execute(
            """INSERT INTO sync_state (key, next_link, delta_link, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET next_link=excluded.next_link,
                 delta_link=COALESCE(excluded.delta_link, sync_state.delta_link),
                 updated_at=excluded.updated_at""",
            (key, next_link, delta_link, _now()),
        )


def _needs_resync(exc: httpx.HTTPStatusError) -> bool:
    if exc.response.status_code == 410:
        return True
    try:
        code = exc.response.json().get("error", {}).get("code", "")
    except ValueError:
        return False
    return exc.response.status_code == 400 and code in _RESYNC_CODES


def _catch_up(graph: Graph, db: sqlite3.Connection, executor: Executor, folder_id: str, c: Counters) -> None:
    """Fetch MIME still missing in this folder. An incremental round only lists changed messages,
    so one whose download failed transiently (or was reset by retry_failed) is otherwise never
    seen again."""
    missing = [
        row[0]
        for row in db.execute(
            "SELECT id FROM messages WHERE folder_id=? AND mime IS NULL AND mime_error IS NULL "
            "AND deleted_at IS NULL",
            (folder_id,),
        )
    ]
    if missing:
        log.info("mime_catch_up", count=len(missing))
        _store_downloads(graph, db, executor, missing, c)


def retry_failed(db: sqlite3.Connection) -> int:
    """Forget recorded download failures so the next round tries those messages again."""
    with db:
        return db.execute(
            "UPDATE messages SET mime_error=NULL WHERE mime IS NULL AND mime_error IS NOT NULL"
        ).rowcount


def sync_folder(graph: Graph, db: sqlite3.Connection, executor: Executor, folder_id: str) -> Counters:
    """One delta round for a folder: resumes a crashed round, else continues from the last round."""
    key = f"folder:{folder_id}"
    fresh = f"me/mailFolders/{path_id(folder_id)}/messages/delta?$select={SELECT}"
    row = db.execute("SELECT next_link, delta_link FROM sync_state WHERE key=?", (key,)).fetchone()
    url = (row and (row["next_link"] or row["delta_link"])) or fresh
    c = Counters()
    while True:
        try:
            page = graph.get(url)
        except httpx.HTTPStatusError as exc:
            if url == fresh or not _needs_resync(exc):
                raise
            log.warning("delta_resync", status=exc.response.status_code)
            with db:
                db.execute("DELETE FROM sync_state WHERE key=?", (key,))
            url = fresh
            continue
        _apply_page(graph, db, executor, folder_id, page.get("value", []), c)
        if next_link := page.get("@odata.nextLink"):
            _save_cursor(db, key, next_link, None)
            url = next_link
        elif delta_link := page.get("@odata.deltaLink"):
            _save_cursor(db, key, None, delta_link)
            _catch_up(graph, db, executor, folder_id, c)
            return c
        else:
            raise RuntimeError("delta page carries neither nextLink nor deltaLink")


def local_counts(db: sqlite3.Connection) -> dict[str, int]:
    return dict(
        db.execute(
            "SELECT folder_id, COUNT(*) FROM messages WHERE deleted_at IS NULL GROUP BY folder_id"
        ).fetchall()
    )
