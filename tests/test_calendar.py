"""Calendar mirror against a fake Graph: backfill, refetch rules, tombstones, window slide, links, CLI."""

import argparse
import json
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from minimail import calendar, cli
from minimail.calendar import meeting_thread_id, sync_calendar, window_end
from minimail.mime import calendar_uid, parse
from minimail.search import short_id
from minimail.store import open_db

from .conftest import BASE, make_graph

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
JOIN = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_Zm9vYmFy%40thread.v2/0?context=%7b%7d"
THREAD = "19:meeting_Zm9vYmFy@thread.v2"


def ev(
    event_id: str, kind: str = "singleInstance", day: int = 12, hour: int = 9, **extra: Any
) -> dict[str, Any]:
    """A slim delta item, times in UTC as Graph sends them."""
    return {
        "id": event_id,
        "type": kind,
        "changeKey": "k1",
        "start": {"dateTime": f"2026-10-{day:02d}T{hour:02d}:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": f"2026-10-{day:02d}T{hour + 1:02d}:00:00.0000000", "timeZone": "UTC"},
        **extra,
    }


def full(item: dict[str, Any], **extra: Any) -> str:
    # Indented: a re-serialised copy would not match byte for byte.
    return json.dumps({**item, "subject": f"subj-{item['id']}", **extra}, indent=1)


class CalGraph:
    """A fresh calendarView/delta round serves `pages` (nextLink `page=N`), a deltaLink round serves
    `changes` once; `me/events/{id}` answers `full[id]` verbatim, 404 if absent."""

    def __init__(self, pages: list[list[dict[str, Any]]], full_events: dict[str, str]) -> None:
        self.pages = pages
        self.full = full_events
        self.changes: list[dict[str, Any]] = []
        self.urls: list[str] = []
        self.gets: Counter[str] = Counter()
        self.gone = False  # the delta link answers 410
        self.status: dict[str, int] = {}  # event id → status its GET answers

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.urls.append(str(req.url))
        path = req.url.path.removeprefix("/v1.0/")
        if path.startswith("me/events/"):
            event_id = path.split("/")[2]
            self.gets[event_id] += 1
            if event_id in self.status:
                return httpx.Response(self.status[event_id])
            return (
                httpx.Response(200, text=self.full[event_id])
                if event_id in self.full
                else httpx.Response(404)
            )
        assert path == "me/calendarView/delta", path
        link = f"{BASE}/{path}"
        if "deltatoken" in req.url.params:
            if self.gone:
                return httpx.Response(410, json={"error": {"code": "syncStateNotFound"}})
            changes, self.changes = self.changes, []
            return httpx.Response(200, json={"value": changes, "@odata.deltaLink": f"{link}?deltatoken=t"})
        page = int(req.url.params.get("page", "0"))
        body: dict[str, Any] = {"value": self.pages[page]}
        if page + 1 < len(self.pages):
            body["@odata.nextLink"] = f"{link}?page={page + 1}"
        else:
            body["@odata.deltaLink"] = f"{link}?deltatoken=t"
        return httpx.Response(200, json=body)


MASTER = ev("M", "seriesMaster", hour=8, iCalUId="UID-M", uid="SERIES-M")
OCCURRENCE = ev("O1", "occurrence", hour=8, seriesMasterId="M")
EXCEPTION = ev("X", "exception", day=13, hour=10, seriesMasterId="M")
SINGLE = ev("S", iCalUId="UID-S")


def corpus() -> CalGraph:
    return CalGraph(
        [[MASTER, SINGLE], [EXCEPTION, OCCURRENCE]],
        {
            "M": full(
                MASTER,
                subject="Weekly sync",
                organizer={"emailAddress": {"name": "Ann Example", "address": "ann@example.test"}},
                onlineMeeting={"joinUrl": JOIN},
            ),
            "S": full(
                SINGLE,
                subject="Budget review",
                location={"displayName": "Salle Été"},
                attendees=[{"emailAddress": {"name": "Zoë Example", "address": "zoe@example.test"}}],
                body={
                    "contentType": "html",
                    "content": '<p class="classword" style="color:stylish">visibleword</p>',
                },
            ),
            "X": full(EXCEPTION, subject="Weekly sync (moved)"),
        },
    )


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(2) as executor:
        yield executor


def rows(db: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["id"]: r for r in db.execute("SELECT * FROM events")}


def test_backfill_stores_every_kind_full_json_verbatim(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """001-AC14: one row per master, single, exception and occurrence over [2000-01-01, now + 2 years];
    masters, singles and exceptions hold the full GET JSON verbatim, an occurrence its delta item."""
    fake = corpus()
    c = sync_calendar(make_graph(fake), db, pool, now=NOW)
    assert (c.listed, c.downloaded, c.deleted) == (4, 3, 0)
    assert "startDateTime=2000-01-01T00:00:00Z&endDateTime=2028-10-07T00:00:00Z" in fake.urls[0]
    got = rows(db)
    assert {k: r["type"] for k, r in got.items()} == {
        "M": "seriesMaster",
        "S": "singleInstance",
        "X": "exception",
        "O1": "occurrence",
    }
    for event_id in ("M", "S", "X"):
        assert got[event_id]["raw"] == fake.full[event_id]
    assert json.loads(got["O1"]["raw"]) == OCCURRENCE and got["O1"]["series_master_id"] == "M"
    assert fake.gets == Counter({"M": 1, "S": 1, "X": 1})
    assert (got["S"]["start_utc"], got["S"]["end_utc"]) == ("2026-10-12T09:00:00Z", "2026-10-12T10:00:00Z")
    assert got["M"]["meeting_thread_id"] == THREAD and got["S"]["meeting_thread_id"] is None
    state = db.execute(
        "SELECT next_link, delta_link, window_end FROM sync_state WHERE key='calendar'"
    ).fetchone()
    assert state["next_link"] is None and "deltatoken" in state["delta_link"]
    assert state["window_end"] == window_end(NOW) == "2028-10-07T00:00:00Z"


def test_full_event_refetched_only_when_its_change_key_moves(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """001-AC14, 001-AC17: a re-listed event with the same changeKey costs no GET; a new one does."""
    fake = corpus()
    graph = make_graph(fake)
    sync_calendar(graph, db, pool, now=NOW)
    fake.changes = [SINGLE, {**EXCEPTION, "changeKey": "k2"}, MASTER, OCCURRENCE]
    c = sync_calendar(graph, db, pool, now=NOW)
    assert fake.gets == Counter({"M": 1, "S": 1, "X": 2}) and c.downloaded == 1


def test_moved_cancelled_and_deleted_events(db: sqlite3.Connection, pool: ThreadPoolExecutor) -> None:
    """001-AC16 (logic): a moved event gets its new times, a cancelled one stays flagged, a removed one
    becomes a tombstone (row and JSON kept); a removal of an unknown id stores nothing."""
    fake = corpus()
    graph = make_graph(fake)
    sync_calendar(graph, db, pool, now=NOW)
    moved = ev("S", hour=15, changeKey="k2", iCalUId="UID-S")
    fake.full["S"] = full(moved)
    fake.full["X"] = full({**EXCEPTION, "changeKey": "k2"}, isCancelled=True)
    fake.changes = [moved, {**EXCEPTION, "changeKey": "k2"}, {"id": "O1", "@removed": {"reason": "deleted"}}]
    fake.changes.append({"id": "never-seen", "@removed": {"reason": "deleted"}})
    c = sync_calendar(graph, db, pool, now=NOW)
    got = rows(db)
    assert got["S"]["start_utc"] == "2026-10-12T15:00:00Z"
    assert got["X"]["is_cancelled"] == 1 and got["X"]["deleted_at"] is None
    assert got["O1"]["deleted_at"] is not None and json.loads(got["O1"]["raw"]) == OCCURRENCE
    assert c.deleted == 1 and "never-seen" not in got
    fake.changes = [OCCURRENCE]  # listed again: live again
    sync_calendar(graph, db, pool, now=NOW)
    assert rows(db)["O1"]["deleted_at"] is None


def test_window_slides_daily_without_refetching_unchanged_events(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """001-AC17: a day later the old window's delta runs, then the wider window is listed afresh;
    only the event entering the future edge is fetched; the next round is incremental again."""
    fake = corpus()
    graph = make_graph(fake)
    sync_calendar(graph, db, pool, now=NOW)
    edge = ev("F", day=20)
    fake.pages[-1] = [*fake.pages[-1], edge]
    fake.full["F"] = full(edge)
    later = NOW + timedelta(days=1)
    start = len(fake.urls)
    c = sync_calendar(graph, db, pool, now=later)
    calls = [u for u in fake.urls[start:] if "calendarView" in u]
    assert "deltatoken" in calls[0] and f"endDateTime={window_end(later)}" in calls[1]
    assert fake.gets == Counter({"M": 1, "S": 1, "X": 1, "F": 1}) and c.downloaded == 1
    assert db.execute("SELECT window_end FROM sync_state").fetchone()[0] == "2028-10-08T00:00:00Z"
    start = len(fake.urls)
    sync_calendar(graph, db, pool, now=later + timedelta(hours=1))
    assert fake.urls[start:] == [f"{BASE}/me/calendarView/delta?deltatoken=t"]


def test_paused_round_resumes_and_a_slide_survives_a_pause(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """001-AC4 bound: a calendar round yields between pages (the first always runs) and resumes from
    its cursor; a pause between the old window's delta and the fresh listing redoes both."""
    fake = corpus()
    graph = make_graph(fake)
    c = sync_calendar(graph, db, pool, lambda: True, now=NOW)
    assert c.paused and set(rows(db)) == {"M", "S"}
    sync_calendar(graph, db, pool, lambda: True, now=NOW)
    assert set(rows(db)) == {"M", "S", "X", "O1"}
    later = NOW + timedelta(days=1)
    assert sync_calendar(graph, db, pool, lambda: True, now=later).paused
    assert db.execute("SELECT window_end FROM sync_state").fetchone()[0] == window_end(NOW)
    sync_calendar(graph, db, pool, now=later)
    assert db.execute("SELECT window_end FROM sync_state").fetchone()[0] == window_end(later)


def test_expired_delta_link_restarts_without_refetch(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    fake = corpus()
    graph = make_graph(fake)
    sync_calendar(graph, db, pool, now=NOW)
    fake.gone = True
    c = sync_calendar(graph, db, pool, now=NOW)
    assert c.listed == 4 and c.downloaded == 0 and fake.gets == Counter({"M": 1, "S": 1, "X": 1})


def test_a_full_event_gone_since_listed_keeps_its_item_and_is_fetched_next_time(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """A new event whose GET 404s keeps its delta item; an archived one keeps its full JSON (never
    overwritten by the slim item); both are fetched again when next listed."""
    fake = corpus()
    del fake.full["S"]
    graph = make_graph(fake)
    sync_calendar(graph, db, pool, now=NOW)
    assert json.loads(rows(db)["S"]["raw"]) == SINGLE and rows(db)["S"]["version"] is None
    fake.full["S"] = full(SINGLE)
    fake.changes = [SINGLE]
    sync_calendar(graph, db, pool, now=NOW)
    assert rows(db)["S"]["raw"] == fake.full["S"]
    archived = rows(db)["M"]
    del fake.full["M"]
    fake.changes = [{**MASTER, "changeKey": "k2"}]
    sync_calendar(graph, db, pool, now=NOW)
    kept = rows(db)["M"]
    assert kept["version"] is None and kept["deleted_at"] is None
    assert [kept[k] for k in ("raw", "subject", "search_text")] == [
        archived[k] for k in ("raw", "subject", "search_text")
    ]
    fake.full["M"] = full({**MASTER, "changeKey": "k2"}, subject="Weekly sync v2")
    fake.changes = [{**MASTER, "changeKey": "k2"}]
    sync_calendar(graph, db, pool, now=NOW)
    assert rows(db)["M"]["subject"] == "Weekly sync v2" and rows(db)["M"]["version"] == "k2"


@pytest.mark.parametrize("status", [404, 403])
def test_a_refused_get_still_takes_the_items_times_and_liveness(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, status: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """001-AC16: an archived event whose GET is refused keeps its full JSON but follows the delta
    item: a move shows in the agenda, a tombstone listed live again is live, a cancel is flagged."""
    fake = corpus()
    graph = make_graph(fake)
    sync_calendar(graph, db, pool, now=NOW)
    fake.changes = [{"id": "S", "@removed": {"reason": "deleted"}}]
    sync_calendar(graph, db, pool, now=NOW)
    archived = rows(db)["S"]
    fake.status = {"S": status, "X": status}
    fake.changes = [ev("S", hour=15, changeKey="k2"), {**EXCEPTION, "changeKey": "k2", "isCancelled": True}]
    sync_calendar(graph, db, pool, now=NOW)
    got = rows(db)
    assert got["S"]["deleted_at"] is None and got["S"]["raw"] == archived["raw"]
    assert (got["S"]["start_utc"], got["S"]["end_utc"]) == ("2026-10-12T15:00:00Z", "2026-10-12T16:00:00Z")
    assert got["X"]["is_cancelled"] == 1 and got["X"]["start_utc"] == "2026-10-13T10:00:00Z"
    listed = calendar.agenda(db, datetime(2026, 10, 12).date(), datetime(2026, 10, 12).date())
    assert [r["start_utc"] for r, _ in listed if r["id"] == "S"] == ["2026-10-12T15:00:00Z"]
    args = argparse.Namespace(id="X", json=False)
    assert cli.cmd_event(None, db, args) == 0  # type: ignore[arg-type]
    assert "Cancelled: yes" in capsys.readouterr().out


@pytest.mark.parametrize("status", [403, 400])
def test_an_event_refused_for_good_does_not_stop_the_round(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, status: int
) -> None:
    """A permanent status on one event's GET is logged; the round stores the rest and moves its cursor."""
    fake = corpus()
    fake.status = {"S": status}
    with capture_logs() as logs:
        c = sync_calendar(make_graph(fake), db, pool, now=NOW)
    assert c.downloaded == 2 and set(rows(db)) == {"M", "S", "X", "O1"}
    assert rows(db)["S"]["version"] is None
    assert [e["status"] for e in logs if e["event"] == "event_fetch_failed"] == [status]
    assert db.execute("SELECT delta_link FROM sync_state WHERE key='calendar'").fetchone()[0]


@pytest.mark.parametrize("status", [429, 401, 500])
def test_a_transient_event_failure_stops_the_round_before_its_cursor(
    db: sqlite3.Connection, pool: ThreadPoolExecutor, status: int
) -> None:
    fake = corpus()
    fake.status = {"X": status}
    with pytest.raises(httpx.HTTPStatusError):
        sync_calendar(make_graph(fake), db, pool, now=NOW)
    assert (
        db.execute("SELECT next_link FROM sync_state WHERE key='calendar'").fetchone()[0].endswith("page=1")
    )


def test_an_unchanged_relisting_leaves_rows_untouched(
    db: sqlite3.Connection, pool: ThreadPoolExecutor
) -> None:
    """Occurrences come back on every listing: no rewrite (no index churn) unless something changed."""
    fake = corpus()
    graph = make_graph(fake)
    sync_calendar(graph, db, pool, now=NOW)
    before = {k: r["updated_at"] for k, r in rows(db).items()}
    fake.changes = [OCCURRENCE, MASTER, SINGLE]
    sync_calendar(graph, db, pool, now=NOW)
    assert {k: r["updated_at"] for k, r in rows(db).items()} == before
    fake.changes = [{**OCCURRENCE, "isCancelled": True}]
    sync_calendar(graph, db, pool, now=NOW)
    assert rows(db)["O1"]["updated_at"] != before["O1"] and rows(db)["O1"]["is_cancelled"] == 1


@pytest.mark.parametrize(
    ("online", "thread"),
    [
        ({"joinUrl": JOIN}, THREAD),
        ({"joinUrl": "https://teams.microsoft.com/l/meetup-join/19:meeting_Zm9vYmFy@thread.v2/0"}, THREAD),
        ({"joinUrl": "https://teams.microsoft.com/meet/123456?p=abc"}, None),
        ({"joinUrl": "https://zoom.example.test/j/123?pwd=19%3ameeting"}, None),
        ({"joinUrl": None}, None),
        (None, None),
    ],
)
def test_meeting_thread_id(online: dict[str, Any] | None, thread: str | None) -> None:
    """001-AC19: the Teams thread id is URL-decoded out of joinUrl; other providers have none."""
    assert meeting_thread_id({"onlineMeeting": online}) == thread


def invitation(uid_lines: str, encoding: str = "7bit") -> bytes:
    body = f"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n{uid_lines}\r\nSUMMARY:x\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    payload = body if encoding == "7bit" else __import__("base64").encodebytes(body.encode()).decode()
    return (
        'Subject: Invitation\r\nContent-Type: multipart/alternative; boundary="b"\r\n\r\n'
        "--b\r\nContent-Type: text/plain\r\n\r\nsee invite\r\n"
        f"--b\r\nContent-Type: text/calendar; method=REQUEST\r\nContent-Transfer-Encoding: {encoding}\r\n\r\n"
        f"{payload}\r\n--b--\r\n"
    ).encode()


@pytest.mark.parametrize(
    ("raw", "uid"),
    [
        (invitation("UID:040000008200E00074C5B7101A82E008"), "040000008200E00074C5B7101A82E008"),
        (invitation("UID:0400000082\r\n 00E00074\r\n\t C5"), "040000008200E00074 C5"),
        (invitation("uid;X-ALT=1:abc@example.test"), "abc@example.test"),
        (invitation("UID:b64-uid", "base64"), "b64-uid"),
        (b"Subject: plain\r\nContent-Type: text/plain\r\n\r\nUID:not-a-calendar\r\n", None),
    ],
)
def test_calendar_uid_from_the_text_calendar_part(raw: bytes, uid: str | None) -> None:
    """001-AC19: the UID line of the text/calendar part, unfolded (RFC 5545), any encoding."""
    assert calendar_uid(parse(raw)) == uid


# --- CLI ------------------------------------------------------------------------------------------


@pytest.fixture
def paris(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "Europe/Paris")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def agenda_archive(env: Path, paris: None) -> CalGraph:
    fake = corpus()
    evil = ev("E", hour=18)
    cancelled, deleted, far = ev("C", hour=16), ev("D", hour=12), ev("Far", day=20)
    fake.pages[0] += [evil, cancelled, deleted, far]
    fake.full |= {
        "E": full(evil, subject="\x1b[31mred‮", location={"displayName": "Room\nforged"}),
        "C": full(cancelled, isCancelled=True),
        "D": full(deleted),
        "Far": full(far),
    }
    with closing(open_db(env / "data")) as db, ThreadPoolExecutor(2) as pool:
        graph = make_graph(fake)
        sync_calendar(graph, db, pool)
        fake.changes = [{"id": "D", "@removed": {"reason": "deleted"}}]
        sync_calendar(graph, db, pool)
        for msg_id, uid in (("inv-series", "SERIES-M"), ("inv-single", "UID-S"), ("other", "UID-OTHER")):
            db.execute(
                "INSERT INTO messages (id, folder_id, subject, received_at, meta, ical_uid, first_seen_at, "
                "updated_at) VALUES (?, 'F1', ?, '2026-10-01T00:00:00Z', '{}', ?, 't', 't')",
                (msg_id, f"Invitation {msg_id}", uid),
            )
        db.commit()
    return fake


def test_agenda_lists_the_period_in_local_time(
    agenda_archive: CalGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    """001-AC15: every live, non-cancelled event overlapping the period, occurrences expanded (never
    the master), local time, organizer, location, Teams flag; terminal controls neutralised."""
    assert cli.main(["agenda", "--from", "2026-10-12", "--to", "2026-10-12"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        f"{short_id('O1')}  Mon 2026-10-12 10:00–11:00  Weekly sync  (Ann Example <ann@example.test>)"
        "  [Teams]",
        f"{short_id('S')}  Mon 2026-10-12 11:00–12:00  Budget review  @ Salle Été",
        f"{short_id('E')}  Mon 2026-10-12 20:00–21:00  �[31mred�  @ Room forged",
    ]
    assert cli.main(["agenda", "--from", "2026-10-13", "--to", "2026-10-13", "--json"]) == 0
    (hit,) = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert hit["id"] == "X" and hit["subject"] == "Weekly sync (moved)" and hit["teams"] is True
    assert hit["meeting_thread_id"] == THREAD and hit["start"] == "Tue 2026-10-13 12:00"


def test_agenda_default_period_is_today_and_the_next_7_days(
    agenda_archive: CalGraph, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Day(cli.date):
        @classmethod
        def today(cls) -> Day:
            return cls(2026, 10, 6)

    monkeypatch.setattr(cli, "date", Day)
    assert cli.main(["agenda", "--json"]) == 0
    ids = [json.loads(line)["id"] for line in capsys.readouterr().out.splitlines()]
    assert ids == ["O1", "S", "E", "X"]


def test_event_shows_linked_mails_and_the_teams_chat(
    agenda_archive: CalGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    """001-AC19: an occurrence inherits its series' Teams thread id and invitation mails (series uid);
    a single event links the mails carrying its iCalUId."""
    assert cli.main(["event", short_id("O1")[:8]]) == 0
    out = capsys.readouterr().out
    assert f"Teams meeting chat: {THREAD}" in out and "Organizer: Ann Example <ann@example.test>" in out
    assert f"Mail: {short_id('inv-series')}  2026-10-01T00:00:00Z  Invitation inv-series" in out
    assert "inv-single" not in out and "other" not in out
    assert cli.main(["event", "S", "--json"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in found["mails"]] == ["inv-single"] and found["meeting_thread_id"] is None
    assert found["attendees"] == ["Zoë Example <zoe@example.test>"]
    assert cli.main(["event", "E"]) == 0
    assert "\x1b" not in capsys.readouterr().out
    assert cli.main(["event", "nope"]) == 2


@pytest.mark.parametrize(
    ("query", "ids"),
    [
        ("budget", ["S"]),  # subject
        ("visibleword", ["S"]),  # HTML body, visible text
        ("classword", []),  # …never its markup
        ("stylish", []),
        ("salle ete", ["S"]),  # location, accent-insensitive
        ("zoe", ["S"]),  # attendee name
        ("zoe@example.test", ["S"]),  # attendee address
        ("weekly", ["X", "M"]),  # master and exception; occurrences not repeated
    ],
)
def test_search_events(
    agenda_archive: CalGraph, capsys: pytest.CaptureFixture[str], query: str, ids: list[str]
) -> None:
    """001-AC18: subject, body, location and attendees, accent-insensitive."""
    assert cli.main(["search", "--events", "--json", query]) == 0
    assert [json.loads(line)["id"] for line in capsys.readouterr().out.splitlines()] == ids


def test_search_events_text_deleted_and_folder(
    agenda_archive: CalGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["search", "--events", "subj-D"]) == 0
    assert capsys.readouterr().out == ""
    assert cli.main(["search", "--events", "--deleted", "subj-D"]) == 0
    assert capsys.readouterr().out.splitlines()[0].endswith("subj-D  (deleted)")
    assert cli.main(["search", "--events", "--folder", "Inbox", "x"]) == 2


def test_sync_includes_the_calendar_unless_folders_are_picked(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .test_cli import _seed
    from .test_mail import FakeGraph

    fake = FakeGraph([[]])
    _seed(env, monkeypatch, fake)
    assert cli.main(["sync", "--folder", "Inbox"]) == 0
    assert not any("calendarView" in u for u in fake.urls)
    assert cli.main(["sync"]) == 0
    assert any("calendarView" in u for u in fake.urls)
    with closing(sqlite3.connect(env / "data" / "mail.db")) as db:
        assert db.execute("SELECT window_end FROM sync_state WHERE key='calendar'").fetchone()[0]


def test_reindex_sets_ical_uid_from_stored_mime(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """001-AC19: existing messages gain their calendar UID from the stored MIME, no download."""
    from .test_search import sync

    sync(env / "data", {"inv": (invitation("UID:uid-1"), {}), "plain": (b"Subject: p\r\n\r\nhello", {})})
    with closing(sqlite3.connect(env / "data" / "mail.db")) as db:
        assert dict(db.execute("SELECT id, ical_uid FROM messages")) == {"inv": "uid-1", "plain": None}
        db.execute("UPDATE messages SET ical_uid=NULL")
        db.commit()
    assert cli.main(["reindex"]) == 0
    with closing(sqlite3.connect(env / "data" / "mail.db")) as db:
        assert dict(db.execute("SELECT id, ical_uid FROM messages")) == {"inv": "uid-1", "plain": None}


def test_non_utc_times_fail_the_round_loudly() -> None:
    with pytest.raises(ValueError, match="time zone"):
        calendar.utc({"dateTime": "2026-10-12T09:00:00", "timeZone": "Romance Standard Time"})
    assert calendar.utc(None) is None and calendar.utc({"dateTime": ""}) is None


def all_day(event_id: str, first: str, end: str) -> dict[str, Any]:
    """Graph's all-day event: midnight to midnight UTC, `end` exclusive."""
    return {
        "id": event_id,
        "type": "singleInstance",
        "changeKey": "k1",
        "isAllDay": True,
        "start": {"dateTime": f"{first}T00:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": f"{end}T00:00:00.0000000", "timeZone": "UTC"},
    }


@pytest.fixture(params=["Europe/Paris", "America/New_York"])
def zone(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_all_day_events_keep_their_own_days_in_any_time_zone(
    env: Path, zone: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """001-AC15: an all-day event is listed on exactly its own day(s), shown as dates, whatever the
    local offset (positive or negative); a multi-day one on each of its days."""
    one, span = all_day("A1", "2026-10-14", "2026-10-15"), all_day("A3", "2026-10-16", "2026-10-19")
    fake = CalGraph([[one, span]], {"A1": full(one), "A3": full(span)})
    with closing(open_db(env / "data")) as db, ThreadPoolExecutor(2) as pool:
        sync_calendar(make_graph(fake), db, pool)
    days = {}
    for day in range(13, 21):
        assert cli.main(["agenda", "--from", f"2026-10-{day}", "--to", f"2026-10-{day}", "--json"]) == 0
        days[day] = [json.loads(line)["id"] for line in capsys.readouterr().out.splitlines()]
    assert days == {13: [], 14: ["A1"], 15: [], 16: ["A3"], 17: ["A3"], 18: ["A3"], 19: [], 20: []}
    assert cli.main(["agenda", "--from", "2026-10-13", "--to", "2026-10-20"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"{short_id('A1')}  Wed 2026-10-14 (all day)  subj-A1",
        f"{short_id('A3')}  Fri 2026-10-16 – Sun 2026-10-18 (all day)  subj-A3",
    ]
    assert cli.main(["event", "A3"]) == 0
    assert "When: Fri 2026-10-16 – Sun 2026-10-18 (all day)" in capsys.readouterr().out
