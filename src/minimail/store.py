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


# External content: the index stores no second copy of the text. VACUUM may renumber `messages`
# rowids (no INTEGER PRIMARY KEY), which `minimail reindex` repairs with a rebuild.
_FTS = (
    """CREATE VIRTUAL TABLE messages_fts USING fts5(subject, sender, recipients, search_text,
         content='messages', tokenize='unicode61 remove_diacritics 2')""",
    """CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
         INSERT INTO messages_fts (rowid, subject, sender, recipients, search_text)
         VALUES (new.rowid, new.subject, new.sender, new.recipients, new.search_text);
       END""",
    """CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
         INSERT INTO messages_fts (messages_fts, rowid, subject, sender, recipients, search_text)
         VALUES ('delete', old.rowid, old.subject, old.sender, old.recipients, old.search_text);
       END""",
    """CREATE TRIGGER messages_fts_update AFTER UPDATE OF subject, sender, recipients, search_text
       ON messages BEGIN
         INSERT INTO messages_fts (messages_fts, rowid, subject, sender, recipients, search_text)
         VALUES ('delete', old.rowid, old.subject, old.sender, old.recipients, old.search_text);
         INSERT INTO messages_fts (rowid, subject, sender, recipients, search_text)
         VALUES (new.rowid, new.subject, new.sender, new.recipients, new.search_text);
       END""",
    "INSERT INTO messages_fts (messages_fts) VALUES ('rebuild')",
)


def _has_message_count(db: sqlite3.Connection) -> bool:
    return "message_count" in {row["name"] for row in db.execute("PRAGMA table_info(folders)")}


def _has_fts(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts'").fetchone() is not None


def needs_migration(db: sqlite3.Connection) -> bool:
    return not (_has_message_count(db) and _has_fts(db))


def _migrate(db: sqlite3.Connection) -> None:
    """Archives from earlier versions are migrated in place, never recreated."""
    if not needs_migration(db):
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        # Re-checked under the write lock: another process may have migrated while we waited.
        if not _has_message_count(db):
            db.execute("ALTER TABLE folders ADD COLUMN message_count INTEGER")
        if not _has_fts(db):
            for statement in _FTS:
                db.execute(statement)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def rebuild_fts(db: sqlite3.Connection) -> None:
    with db:
        db.execute(_FTS[-1])


def open_readonly(data_dir: Path) -> sqlite3.Connection:
    """Reader that never brings an archive into existence. Raises sqlite3.OperationalError if absent."""
    db = sqlite3.connect(f"file:{data_dir / 'mail.db'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db
