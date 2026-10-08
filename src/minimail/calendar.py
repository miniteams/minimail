"""Default calendar mirror: `calendarView/delta` rounds over [2000-01-01, now + 2 years].

The delta lists series masters, single events, exceptions and occurrences, but slim: masters,
singles and exceptions are fetched in full (`GET me/events/{id}`) and kept verbatim, an occurrence
keeps its delta item. A full event is refetched only when its changeKey moved, so re-listing is
cheap: the daily window slide is one more round over the wider window.
"""

import json
import re
import sqlite3
from collections.abc import Callable
from concurrent.futures import Executor
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import unquote

import httpx
import structlog

from .graph import Graph
from .mail import PERMANENT, Counters, _address, _needs_resync, _now, path_id
from .mime import MAX_TEXT, html_to_text
from .search import fts_query

log = structlog.get_logger()

KEY = "calendar"
# Before any mailbox's first event: "since the earliest event" without a probe round.
WINDOW_START = "2000-01-01T00:00:00Z"
HORIZON = timedelta(days=730)
_THREAD = re.compile(r"19:meeting_[^@/?#&\s]+@thread\.v2")


def window_end(now: datetime) -> str:
    # Day-granular: the window slides, and a round re-lists it, at most once a day.
    return f"{(now + HORIZON).date().isoformat()}T00:00:00Z"


def utc(when: dict[str, Any] | None) -> str | None:
    if not when or not when.get("dateTime"):
        return None
    if (when.get("timeZone") or "UTC") != "UTC":
        # Graph answers in UTC unless asked otherwise (Prefer: outlook.timezone), which minimail never does.
        raise ValueError(f"unexpected event time zone {when.get('timeZone')!r}")
    # Graph sends 7 fractional digits; fromisoformat takes 6.
    return datetime.fromisoformat(when["dateTime"][:26]).strftime("%Y-%m-%dT%H:%M:%SZ")


def meeting_thread_id(event: dict[str, Any]) -> str | None:
    """Teams meeting chat id, URL-encoded in the join URL; None for other providers."""
    url = (event.get("onlineMeeting") or {}).get("joinUrl") or ""
    found = _THREAD.search(unquote(url))
    return found.group(0) if found else None


def _search_text(event: dict[str, Any]) -> str:
    body = event.get("body") or {}
    content = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html":
        content = html_to_text(content)
    places = [(event.get("location") or {}).get("displayName")]
    places += [p.get("displayName") for p in event.get("locations") or []]
    people = [_address(event.get("organizer")), *(_address(a) for a in event.get("attendees") or [])]
    return "\n".join(t for t in dict.fromkeys([*places, *people, content]) if t)[:MAX_TEXT]


def _version(item: dict[str, Any]) -> str | None:
    value = item.get("changeKey") or item.get("lastModifiedDateTime")
    return str(value) if value else None


def _store(db: sqlite3.Connection, event_id: str, raw: str, version: str | None, now: str) -> None:
    event = json.loads(raw)
    db.execute(
        """INSERT INTO events (id, type, series_master_id, ical_uid, start_utc, end_utc, is_cancelled,
             meeting_thread_id, subject, raw, version, search_text, first_seen_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET type=excluded.type, series_master_id=excluded.series_master_id,
             ical_uid=excluded.ical_uid, start_utc=excluded.start_utc, end_utc=excluded.end_utc,
             is_cancelled=excluded.is_cancelled, meeting_thread_id=excluded.meeting_thread_id,
             subject=excluded.subject, raw=excluded.raw, version=excluded.version,
             search_text=excluded.search_text, updated_at=excluded.updated_at, deleted_at=NULL
           -- An unchanged re-listing (occurrences, every round) leaves the row and its index alone.
           WHERE events.raw IS NOT excluded.raw OR events.version IS NOT excluded.version
             OR events.deleted_at IS NOT NULL""",
        (
            event_id,
            event.get("type"),
            event.get("seriesMasterId"),
            event.get("iCalUId"),
            utc(event.get("start")),
            utc(event.get("end")),
            bool(event.get("isCancelled")),
            meeting_thread_id(event),
            event.get("subject") or "",
            raw,
            version,
            _search_text(event),
            now,
            now,
        ),
    )


def _fetch(graph: Graph, event_id: str) -> str | None:
    try:
        return graph.request("GET", f"me/events/{path_id(event_id)}").text
    except httpx.HTTPStatusError as exc:
        # Throttling, auth and 5xx left after retries stop the round; a verdict on the item does not.
        if exc.response.status_code not in PERMANENT:
            raise
        log.warning("event_fetch_failed", status=exc.response.status_code)
        return None


def _known(db: sqlite3.Connection, event_id: str) -> bool:
    return db.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone() is not None


def _stale(db: sqlite3.Connection, item: dict[str, Any]) -> bool:
    row = db.execute("SELECT version FROM events WHERE id=?", (item["id"],)).fetchone()
    return row is None or row[0] is None or row[0] != _version(item)


def _apply_page(
    graph: Graph, db: sqlite3.Connection, executor: Executor, items: list[dict[str, Any]], c: Counters
) -> None:
    live = [i for i in items if "@removed" not in i]
    wanted = [i["id"] for i in live if i.get("type") != "occurrence" and _stale(db, i)]
    fetched = dict(zip(wanted, executor.map(lambda event_id: _fetch(graph, event_id), wanted), strict=True))
    now = _now()
    with db:
        for item in live:
            if (raw := fetched.get(item["id"])) is not None:
                _store(db, item["id"], raw, _version(item), now)
                c.downloaded += 1
            elif item["id"] in fetched and _known(db, item["id"]):
                # Keep the archived full JSON, take the item's times and state (a refused GET may never
                # succeed); no version: fetched again when next listed.
                db.execute(
                    """UPDATE events SET version=NULL, start_utc=COALESCE(?, start_utc),
                         end_utc=COALESCE(?, end_utc), is_cancelled=COALESCE(?, is_cancelled),
                         deleted_at=NULL, updated_at=? WHERE id=?""",
                    (
                        utc(item.get("start")),
                        utc(item.get("end")),
                        bool(item["isCancelled"]) if "isCancelled" in item else None,
                        now,
                        item["id"],
                    ),
                )
            elif item["id"] in fetched or item.get("type") == "occurrence":
                # No version: a full event whose GET failed is fetched again when next listed.
                _store(db, item["id"], json.dumps(item, ensure_ascii=False), None, now)
            else:
                db.execute(
                    "UPDATE events SET deleted_at=NULL, updated_at=? WHERE id=? AND deleted_at IS NOT NULL",
                    (now, item["id"]),
                )
        for item in items:
            if "@removed" in item:
                c.deleted += db.execute(
                    "UPDATE events SET deleted_at=?, updated_at=? WHERE id=? AND deleted_at IS NULL",
                    (now, now, item["id"]),
                ).rowcount
    c.listed += len(live)


def _save_cursor(db: sqlite3.Connection, next_link: str | None, delta_link: str | None, end: str) -> None:
    with db:
        db.execute(
            """INSERT INTO sync_state (key, next_link, delta_link, window_end, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET next_link=excluded.next_link,
                 delta_link=COALESCE(excluded.delta_link, sync_state.delta_link),
                 window_end=excluded.window_end, updated_at=excluded.updated_at""",
            (KEY, next_link, delta_link, end, _now()),
        )


def sync_calendar(
    graph: Graph,
    db: sqlite3.Connection,
    executor: Executor,
    pause: Callable[[], bool] | None = None,
    now: datetime | None = None,
) -> Counters:
    """One delta round of the default calendar, resumed from the saved cursor. When the window end
    has moved (daily), the round finishes the old window's delta, then lists the wider window
    afresh. Between pages, a true `pause()` ends the call with `paused` set."""
    end = window_end(now or datetime.now(UTC))
    fresh = f"me/calendarView/delta?startDateTime={WINDOW_START}&endDateTime={end}"
    row = db.execute(
        "SELECT next_link, delta_link, window_end FROM sync_state WHERE key=?", (KEY,)
    ).fetchone()
    url = (row and (row["next_link"] or row["delta_link"])) or fresh
    current = row["window_end"] if row and url != fresh else end
    c = Counters()
    first = True
    while True:
        # The first page always runs: a round that may pause at once could never progress.
        if not first and pause is not None and pause():
            c.paused = True
            return c
        first = False
        try:
            page = graph.get(url)
        except httpx.HTTPStatusError as exc:
            if url == fresh or not _needs_resync(exc):
                raise
            log.warning("calendar_resync", status=exc.response.status_code)
            with db:
                db.execute("DELETE FROM sync_state WHERE key=?", (KEY,))
            url, current = fresh, end
            continue
        for link in (page.get("@odata.nextLink"), page.get("@odata.deltaLink")):
            if link:
                graph.url(link)  # Security: a link the client would refuse never becomes the cursor
        _apply_page(graph, db, executor, page.get("value", []), c)
        if next_link := page.get("@odata.nextLink"):
            _save_cursor(db, next_link, None, current)
            url = next_link
        elif delta_link := page.get("@odata.deltaLink"):
            _save_cursor(db, None, delta_link, current)
            if current == end:
                return c
            log.info("calendar_window_slide", end=end)
            url, current = fresh, end
        else:
            raise RuntimeError("delta page carries neither nextLink nor deltaLink")


def details(row: sqlite3.Row) -> dict[str, Any]:
    """An event's JSON over its series master's: an occurrence's slim item inherits the rest."""
    master = json.loads(row["master_raw"]) if row["master_raw"] else {}
    merged: dict[str, Any] = master | json.loads(row["raw"])
    return merged


_WITH_MASTER = "SELECT e.*, m.raw AS master_raw FROM events e LEFT JOIN events m ON m.id = e.series_master_id"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def agenda(db: sqlite3.Connection, first: date, last: date) -> list[tuple[sqlite3.Row, dict[str, Any]]]:
    """Live, non-cancelled events overlapping the local days [first, last], with their details; series
    expanded: occurrences and exceptions, never the master (it would repeat its first occurrence).
    An all-day event is floating (midnight to midnight UTC in Graph): it overlaps by its dates."""
    start, end = (datetime.combine(d, time()).astimezone() for d in (first, last + timedelta(1)))
    found = []
    for row in db.execute(
        f"""{_WITH_MASTER}
           WHERE e.deleted_at IS NULL AND NOT e.is_cancelled AND e.type IS NOT 'seriesMaster'
             AND (m.id IS NULL OR (m.deleted_at IS NULL AND NOT m.is_cancelled))
             AND e.start_utc < ? AND e.end_utc > ?
           ORDER BY e.start_utc, e.end_utc""",
        (_stamp(end), _stamp(start)),
    ):
        data = details(row)
        # An all-day day [D 00:00Z, D+1 00:00Z) overlaps local day D under any offset: the SQL test
        # keeps it, and the date test drops the neighbouring local day it also overlaps.
        if data.get("isAllDay"):
            hit = row["start_utc"][:10] <= last.isoformat() and row["end_utc"][:10] > first.isoformat()
        else:
            hit = row["start_utc"] < _stamp(end) and row["end_utc"] > _stamp(start)
        if hit:
            found.append((row, data))
    return found


def event(db: sqlite3.Connection, event_id: str) -> sqlite3.Row:
    row: sqlite3.Row = db.execute(f"{_WITH_MASTER} WHERE e.id=?", (event_id,)).fetchone()
    return row


def linked_mails(db: sqlite3.Connection, data: dict[str, Any]) -> list[sqlite3.Row]:
    """Messages whose text/calendar UID is the event's iCalUId or its series-wide uid."""
    uids = sorted({u for u in (data.get("iCalUId"), data.get("uid")) if u})
    return db.execute(
        "SELECT id, received_at, subject, deleted_at FROM messages "
        f"WHERE ical_uid IN ({','.join('?' * len(uids))}) ORDER BY received_at",
        uids,
    ).fetchall()


def search_events(
    db: sqlite3.Connection, text: str, limit: int = 20, deleted: bool = False
) -> list[sqlite3.Row]:
    """Matches latest start first; occurrences left out (each repeats its master's text)."""
    return db.execute(
        """SELECT e.id, e.start_utc, e.subject, e.deleted_at,
             snippet(events_fts, -1, '[', ']', '…', 12) AS snippet
           FROM events_fts JOIN events e ON e.rowid = events_fts.rowid
           WHERE events_fts MATCH ? AND (? OR e.deleted_at IS NULL) AND e.type IS NOT 'occurrence'
           ORDER BY e.start_utc DESC LIMIT ?""",
        (fts_query(text), deleted, limit),
    ).fetchall()
