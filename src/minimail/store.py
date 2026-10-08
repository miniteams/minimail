"""SQLite archive (`data/mail.db`): one file, one writer, readers never block it (WAL)."""

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    display_name TEXT NOT NULL,
    path TEXT NOT NULL,
    total_count INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT
);
"""


def open_db(data_dir: Path) -> sqlite3.Connection:
    """Open (creating if needed) the archive. Mail is personal data: the directory is owner-only and
    the process umask (set by the CLI) keeps the db and its -wal/-shm files owner-only too."""
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o700)
    db = sqlite3.connect(data_dir / "mail.db")
    db.row_factory = sqlite3.Row
    # WAL: readers (search, MCP) don't block the sync writer; a crash rolls the open transaction back.
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(_SCHEMA)
    return db


def open_readonly(data_dir: Path) -> sqlite3.Connection:
    """Reader that never brings an archive into existence. Raises sqlite3.OperationalError if absent."""
    db = sqlite3.connect(f"file:{data_dir / 'mail.db'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db
