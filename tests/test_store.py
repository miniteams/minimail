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
