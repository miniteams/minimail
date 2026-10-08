"""Archive queries: full-text search and message id resolution."""

import hashlib
import sqlite3

# Graph ids share a long per-mailbox prefix, so a prefix of the id itself never makes a short handle.
SHORT_ID_LEN = 12


def short_id(msg_id: str) -> str:
    return hashlib.sha256(msg_id.encode()).hexdigest()[:SHORT_ID_LEN]


def fts_query(text: str) -> str:
    """Every whitespace-separated word as a quoted FTS5 string, so operators and punctuation in user
    input are plain text; juxtaposed strings are ANDed. Blank input matches nothing (an empty MATCH
    is a syntax error)."""
    # FTS5 ends a string at NUL ("unterminated string"): it separates words like whitespace.
    words = text.replace("\x00", " ").split()
    return " ".join('"' + word.replace('"', '""') + '"' for word in words) or '""'


def search(
    db: sqlite3.Connection, text: str, folder: str | None = None, limit: int = 20, deleted: bool = False
) -> list[sqlite3.Row]:
    """Matches newest first; tombstoned messages only with `deleted`."""
    return db.execute(
        """SELECT m.id, m.received_at, f.path AS folder, m.sender, m.subject, m.deleted_at,
             snippet(messages_fts, -1, '[', ']', '…', 12) AS snippet
           FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid
           LEFT JOIN folders f ON f.id = m.folder_id
           WHERE messages_fts MATCH ? AND (? OR m.deleted_at IS NULL) AND (? IS NULL OR f.path = ?)
           ORDER BY m.received_at DESC LIMIT ?""",
        (fts_query(text), deleted, folder, folder, limit),
    ).fetchall()


def resolve(db: sqlite3.Connection, ref: str) -> str:
    """Full message id from a full id, or a unique prefix of it or of its short id. Raises
    LookupError (unknown or ambiguous)."""
    if db.execute("SELECT 1 FROM messages WHERE id=?", (ref,)).fetchone():
        return ref
    hex_ref = ref.lower()
    # ponytail: full scan hashing every id (tens of thousands: milliseconds); store short ids if it grows.
    matches: list[str] = [
        row[0]
        for row in db.execute("SELECT id FROM messages")
        if ref and (row[0].startswith(ref) or short_id(row[0]).startswith(hex_ref))
    ]
    if len(matches) != 1:
        raise LookupError(f"{'no' if not matches else 'ambiguous'} message id {ref!r}")
    return matches[0]
