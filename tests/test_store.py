"""Archive schema: in-place migration of archives created by earlier versions."""

import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from minimail import store
from minimail.store import open_db

# folders as phase 1/2 created it: no message_count.
_OLD_FOLDERS = """CREATE TABLE folders (id TEXT PRIMARY KEY, parent_id TEXT, display_name TEXT NOT NULL,
    path TEXT NOT NULL, total_count INTEGER NOT NULL DEFAULT 0, deleted_at TEXT)"""


def test_existing_archive_gains_message_count_in_place(tmp_path: Path) -> None:
    """001-AC1: an archive from before message_count gets the column added; its rows survive and a
    second open is a no-op."""
    with closing(sqlite3.connect(tmp_path / "mail.db")) as old:
        old.execute(_OLD_FOLDERS)
        old.execute(
            "INSERT INTO folders (id, display_name, path, total_count) VALUES ('A', 'Inbox', 'Inbox', 7)"
        )
        old.commit()
    for _ in range(2):
        with closing(open_db(tmp_path)) as db:
            columns = [row["name"] for row in db.execute("PRAGMA table_info(folders)")]
            assert columns.count("message_count") == 1
            row = db.execute("SELECT id, path, total_count, message_count FROM folders").fetchone()
            assert tuple(row) == ("A", "Inbox", 7, None)


def test_fresh_archive_has_message_count(tmp_path: Path) -> None:
    with closing(open_db(tmp_path)) as db:
        assert "message_count" in [row["name"] for row in db.execute("PRAGMA table_info(folders)")]


def _old_archive(path: Path) -> None:
    """A real pre-message_count archive: every table, WAL mode."""
    with closing(open_db(path)) as db:
        db.execute("ALTER TABLE folders DROP COLUMN message_count")
        db.commit()


def test_migration_rechecks_under_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another process migrating between our check and our lock must not make us add the column twice."""
    _old_archive(tmp_path)
    real = store._has_message_count
    answers = iter([False])  # our first look predates the other process's ALTER

    def has_message_count(db: sqlite3.Connection) -> bool:
        return next(answers, None) is not False and real(db)

    with closing(sqlite3.connect(tmp_path / "mail.db")) as other:
        other.execute("ALTER TABLE folders ADD COLUMN message_count INTEGER")
        other.commit()
    monkeypatch.setattr(store, "_has_message_count", has_message_count)
    with closing(open_db(tmp_path)) as db:
        assert [r["name"] for r in db.execute("PRAGMA table_info(folders)")].count("message_count") == 1


def test_concurrent_first_opens_migrate_once(tmp_path: Path) -> None:
    for attempt in range(10):
        data = tmp_path / str(attempt)
        data.mkdir()
        _old_archive(data)
        errors: list[BaseException] = []
        start = threading.Barrier(4)

        def opener(
            data: Path = data, errors: list[BaseException] = errors, start: threading.Barrier = start
        ) -> None:
            start.wait()
            try:
                open_db(data).close()
            except BaseException as exc:  # noqa: BLE001 — the test reports what a racer raised
                errors.append(exc)

        threads = [threading.Thread(target=opener) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


def test_migration_blocked_by_another_writer_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer holding the lock past the busy timeout → OperationalError, and the archive unchanged."""
    _old_archive(tmp_path)
    monkeypatch.setattr(store, "BUSY_TIMEOUT", 0.05)
    with closing(sqlite3.connect(tmp_path / "mail.db", isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            open_db(tmp_path)
        writer.execute("ROLLBACK")
    with closing(sqlite3.connect(tmp_path / "mail.db")) as check:
        assert "message_count" not in [r[1] for r in check.execute("PRAGMA table_info(folders)")]


def _pre_calendar_archive(path: Path) -> None:
    """An archive as phase 4 left it: no events, no messages.ical_uid, no sync_state.window_end."""
    with closing(open_db(path)) as db:
        for statement in (
            *(f"DROP TRIGGER events_fts_{op}" for op in ("insert", "delete", "update")),
            "DROP TABLE events_fts",
            "DROP TABLE events",
            "DROP INDEX messages_ical_uid",
            "ALTER TABLE messages DROP COLUMN ical_uid",
            "ALTER TABLE sync_state DROP COLUMN window_end",
            "INSERT INTO messages (id, folder_id, meta, first_seen_at, updated_at) "
            "VALUES ('m', 'F', '{}', 't', 't')",
            "INSERT INTO sync_state (key, delta_link, updated_at) VALUES ('folder:F', 'd', 't')",
        ):
            db.execute(statement)
        db.commit()


def _event(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO events (id, subject, raw, search_text, first_seen_at, updated_at) "
        "VALUES ('e', 'Agenda review', '{}', 'Salle été', 't', 't')"
    )


def test_existing_archive_gains_the_calendar_in_place(tmp_path: Path) -> None:
    """001-AC14, 001-AC19: events, events_fts, messages.ical_uid and sync_state.window_end are added in
    place; rows survive; readers refuse the archive until then; a second open is a no-op."""
    _pre_calendar_archive(tmp_path)
    with closing(store.open_readonly(tmp_path)) as reader:
        assert store.needs_migration(reader)
    for attempt in range(2):
        with closing(open_db(tmp_path)) as db:
            assert not store.needs_migration(db)
            assert tuple(db.execute("SELECT id, ical_uid FROM messages").fetchone()) == ("m", None)
            assert tuple(db.execute("SELECT key, window_end FROM sync_state").fetchone()) == (
                "folder:F",
                None,
            )
            if not attempt:
                _event(db)
                db.commit()
            hits = db.execute("SELECT rowid FROM events_fts WHERE events_fts MATCH 'salle ete'").fetchall()
            assert len(hits) == 1


def test_calendar_migration_rechecks_under_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another process adding the calendar between our check and our lock must not make us add it twice."""
    _pre_calendar_archive(tmp_path)
    open_db(tmp_path).close()  # the "other process"
    real = store._has_calendar
    answers = iter([False])

    def has_calendar(db: sqlite3.Connection) -> bool:
        return next(answers, None) is not False and real(db)

    monkeypatch.setattr(store, "_has_calendar", has_calendar)
    with closing(open_db(tmp_path)) as db:
        assert [r["name"] for r in db.execute("PRAGMA table_info(messages)")].count("ical_uid") == 1


def test_calendar_migration_blocked_by_another_writer_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pre_calendar_archive(tmp_path)
    monkeypatch.setattr(store, "BUSY_TIMEOUT", 0.05)
    with closing(sqlite3.connect(tmp_path / "mail.db", isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            open_db(tmp_path)
        writer.execute("ROLLBACK")
    with closing(sqlite3.connect(tmp_path / "mail.db")) as check:
        assert check.execute("SELECT name FROM sqlite_master WHERE name LIKE 'events%'").fetchall() == []
        assert "ical_uid" not in [r[1] for r in check.execute("PRAGMA table_info(messages)")]


def test_reindex_rebuilds_the_event_index(tmp_path: Path) -> None:
    with closing(open_db(tmp_path)) as db:
        _event(db)
        db.execute("INSERT INTO events_fts (events_fts) VALUES ('delete-all')")
        db.commit()
        store.rebuild_fts(db)
        assert len(db.execute("SELECT rowid FROM events_fts WHERE events_fts MATCH 'agenda'").fetchall()) == 1


_OLD_FTS_UPDATE = """CREATE TRIGGER messages_fts_update
       AFTER UPDATE OF subject, sender, recipients, search_text ON messages BEGIN
         INSERT INTO messages_fts (messages_fts, rowid, subject, sender, recipients, search_text)
         VALUES ('delete', old.rowid, old.subject, old.sender, old.recipients, old.search_text);
         INSERT INTO messages_fts (rowid, subject, sender, recipients, search_text)
         VALUES (new.rowid, new.subject, new.sender, new.recipients, new.search_text);
       END"""


def _fts_writes(db: sqlite3.Connection, sql: str) -> int:
    before = db.total_changes
    db.execute(sql)
    return db.total_changes - before - 1  # the row itself; FTS5 counts its own shadow-table writes


@pytest.mark.parametrize("migrated", [False, True])
def test_fts_is_rewritten_only_when_an_indexed_column_changes(tmp_path: Path, migrated: bool) -> None:
    """A metadata-only upsert (subject/sender/recipients rewritten unchanged) leaves the index alone;
    an archive with the earlier trigger, and no mime_attempts, is migrated in place."""
    if migrated:
        with closing(open_db(tmp_path)) as db:
            db.execute("DROP TRIGGER messages_fts_update")
            db.execute(_OLD_FTS_UPDATE)
            db.execute("ALTER TABLE messages DROP COLUMN mime_retry_at")
            db.execute("ALTER TABLE events DROP COLUMN fetch_error")
            db.commit()
        with closing(store.open_readonly(tmp_path)) as reader:
            assert store.needs_migration(reader)
    with closing(open_db(tmp_path)) as db:
        assert not store.needs_migration(db)
        db.execute(
            "INSERT INTO messages (id, folder_id, subject, sender, recipients, meta, first_seen_at, "
            "updated_at) VALUES ('m', 'F', 'hello', 'a@example.test', '', '{}', 't', 't')"
        )
        db.commit()  # FTS5 flushes its pending writes here; they must not land in the counts below
        assert _fts_writes(db, "UPDATE messages SET subject='hello', sender=sender, meta='{\"x\":1}'") == 0
        assert _fts_writes(db, "UPDATE messages SET search_text='bonjour'") > 0
        assert (
            db.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'bonjour'").fetchone()[0]
            == 1
        )
        assert tuple(db.execute("SELECT mime_attempts, mime_retry_at FROM messages").fetchone()) == (0, None)
        assert db.execute("SELECT fetch_error, event_attempts, event_retry_at FROM events").fetchall() == []


def test_trigger_migration_rechecks_under_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another process replacing the trigger between our check and our lock: we must not drop it again."""
    open_db(tmp_path).close()
    real = store._has_fts_when
    answers = iter([False])

    def has_fts_when(db: sqlite3.Connection) -> bool:
        return next(answers, None) is not False and real(db)

    monkeypatch.setattr(store, "_has_fts_when", has_fts_when)
    with closing(open_db(tmp_path)) as db:
        assert not store.needs_migration(db)
