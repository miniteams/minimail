"""SQLite archive (`data/mail.db`): one file, one writer, readers never block it (WAL)."""

import sqlite3
from pathlib import Path

# Seconds a connection waits for another writer's lock before "database is locked".
BUSY_TIMEOUT = 5.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    display_name TEXT NOT NULL,
    path TEXT NOT NULL,
    total_count INTEGER NOT NULL DEFAULT 0,  -- Graph totalItemCount: includes non-mail items
    deleted_at TEXT,
    message_count INTEGER               -- messages/$count; NULL if Graph would not count them
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,                -- Graph immutable id: stable across folder moves
    folder_id TEXT NOT NULL,
    internet_message_id TEXT,
    received_at TEXT,
    subject TEXT, sender TEXT, recipients TEXT,
    meta TEXT NOT NULL,                 -- latest Graph metadata JSON (read, flag, categories…)
    mime BLOB,                          -- zstd of the verbatim MIME; NULL until fetched
    mime_sha256 TEXT,                   -- of the uncompressed bytes, for `verify`
    mime_size INTEGER,
    mime_error TEXT,                    -- why the MIME could not be fetched
    search_text TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS messages_folder ON messages(folder_id);
CREATE INDEX IF NOT EXISTS messages_received ON messages(received_at);
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,               -- `folder:<id>` (later `calendar`)
    next_link TEXT,                     -- mid-round cursor: resume here after a crash
    delta_link TEXT,                    -- end-of-round cursor: next incremental round
    updated_at TEXT NOT NULL
);
"""


def open_db(data_dir: Path) -> sqlite3.Connection:
    """Open (creating if needed) the archive. Mail is personal data: the directory is owner-only and
    the process umask (set by the CLI) keeps the db and its -wal/-shm files owner-only too."""
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o700)
    db = sqlite3.connect(data_dir / "mail.db", timeout=BUSY_TIMEOUT)
    db.row_factory = sqlite3.Row
    try:
        # WAL: readers (search, MCP) don't block the sync writer; a crash rolls the open transaction back.
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(_SCHEMA)
        _migrate(db)
    except BaseException:
        db.close()  # e.g. "database is locked": the caller never gets a connection to close
        raise
    return db


def _has_message_count(db: sqlite3.Connection) -> bool:
    return "message_count" in {row["name"] for row in db.execute("PRAGMA table_info(folders)")}


def _migrate(db: sqlite3.Connection) -> None:
    """Archives created before message_count existed are migrated in place, never recreated."""
    if _has_message_count(db):
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        # Re-checked under the write lock: another process may have migrated while we waited.
        if not _has_message_count(db):
            db.execute("ALTER TABLE folders ADD COLUMN message_count INTEGER")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def open_readonly(data_dir: Path) -> sqlite3.Connection:
    """Reader that never brings an archive into existence. Raises sqlite3.OperationalError if absent."""
    db = sqlite3.connect(f"file:{data_dir / 'mail.db'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db
