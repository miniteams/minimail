"""Full-text search, show and attachment extraction, end to end from a synced archive."""

import base64
import json
import os
import quopri
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest

from minimail import cli, mime, store
from minimail.mail import sync_folder
from minimail.search import fts_query, resolve, search, short_id
from minimail.store import open_db

from .conftest import BASE, make_graph
from .test_mail import item


def raw(headers: str, body: bytes) -> bytes:
    return headers.strip().replace("\n", "\r\n").encode() + b"\r\n\r\n" + body


def b64(data: bytes) -> bytes:
    return base64.encodebytes(data).replace(b"\n", b"\r\n")


def sync(data: Path, mails: dict[str, tuple[bytes, dict[str, Any]]]) -> None:
    """One real delta round serving these MIMEs: the same store path as `minimail sync`."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/v1.0/")
        if path.endswith("/$value"):
            return httpx.Response(200, content=mails[path.split("/")[2]][0])
        items = [{**item(msg_id), **extra} for msg_id, (_, extra) in mails.items()]
        delta = f"{BASE}/me/mailFolders/F1/messages/delta?deltatoken=t"
        return httpx.Response(200, json={"value": items, "@odata.deltaLink": delta})

    graph = make_graph(handler)
    with closing(open_db(data)) as db, ThreadPoolExecutor(2) as pool:
        db.execute("INSERT OR IGNORE INTO folders (id, display_name, path) VALUES ('F1', 'Inbox', 'Inbox')")
        db.execute("INSERT OR IGNORE INTO folders (id, display_name, path) VALUES ('F2', 'Old', 'Old')")
        db.commit()
        sync_folder(graph, db, pool, "F1")
    graph.close()


CORPUS: dict[str, tuple[bytes, dict[str, Any]]] = {
    "qp-latin1": (
        raw(
            "Subject: a\nContent-Type: text/plain; charset=iso-8859-1\n"
            "Content-Transfer-Encoding: quoted-printable",
            b"R=E9union d'=E9t=E9 =E0 l'h=F4tel",
        ),
        {"receivedDateTime": "2026-01-03T00:00:00Z"},
    ),
    "b64-cp1252": (
        raw(
            "Subject: b\nContent-Type: text/plain; charset=windows-1252\nContent-Transfer-Encoding: base64",
            b64("Facturé 12 € pour Noël".encode("cp1252")),
        ),
        {"receivedDateTime": "2026-01-02T00:00:00Z"},
    ),
    "qp-utf8": (
        raw(
            "Subject: c\nContent-Type: text/plain; charset=utf-8\n"
            "Content-Transfer-Encoding: quoted-printable",
            quopri.encodestring("Crème brûlée chez Zoë".encode()),
        ),
        {"receivedDateTime": "2026-01-01T00:00:00Z"},
    ),
    "rfc2047": (
        raw("Subject: =?iso-8859-1?q?Pr=E9vision_budg=E9taire?=\nContent-Type: text/plain", b"see below"),
        # Graph's `subject` is the header as Exchange decoded it.
        {"subject": "Prévision budgétaire", "receivedDateTime": "2025-12-31T00:00:00Z"},
    ),
    "html-only": (
        raw(
            "Subject: h\nContent-Type: text/html; charset=utf-8",
            b"<html><head><title>titleword</title><style>.styleword{color:red}</style></head>"
            b'<body><p class="classword" data-x="attrword" style="color:inlinestyleword">visibleword'
            b"</p><script>var scriptword=1;</script><div>secondword</div></body></html>",
        ),
        {"receivedDateTime": "2025-12-30T00:00:00Z"},
    ),
}


@pytest.fixture
def archive(env: Path) -> Path:
    sync(env / "data", CORPUS)
    return env / "data"


def hits(capsys: pytest.CaptureFixture[str], *argv: str) -> list[dict[str, Any]]:
    capsys.readouterr()
    assert cli.main(["search", "--json", *argv]) == 0
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("reunion", "qp-latin1"),  # quoted-printable, ISO-8859-1
        ("hotel", "qp-latin1"),
        ("ete", "qp-latin1"),  # `ete` matches `été`
        ("ÉTÉ", "qp-latin1"),
        ("facture noel", "b64-cp1252"),  # base64, windows-1252
        ("creme brulee zoe", "qp-utf8"),  # quoted-printable, UTF-8
        ("budgetaire", "rfc2047"),  # RFC 2047 encoded subject
    ],
)
def test_search_decodes_every_encoding(
    archive: Path, capsys: pytest.CaptureFixture[str], query: str, expected: str
) -> None:
    """001-AC7: through `minimail search`, words of QP/base64 bodies in latin-1/cp1252/UTF-8 and of an
    RFC 2047 subject are found, accent- and case-insensitive."""
    assert [h["id"] for h in hits(capsys, query)] == [expected]


def test_search_html_matches_visible_text_only(archive: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """001-AC8: an HTML-only mail matches on its visible text, never on tags, attributes, styles or
    scripts."""
    assert [h["id"] for h in hits(capsys, "visibleword secondword")] == ["html-only"]
    for hidden in ("titleword", "styleword", "classword", "attrword", "inlinestyleword", "scriptword", "div"):
        assert hits(capsys, hidden) == [], hidden


@pytest.mark.parametrize(
    "query",
    [
        '"',
        '"reunion',
        "-reunion",
        "reunion -hotel",
        "col:umn",
        "reun*",
        "NEAR(reunion hotel)",
        "AND",
        "(",
        "*",
        "",
        "\x00",
        "a\x00b",
    ],
)
def test_any_user_text_is_a_valid_query(
    archive: Path, capsys: pytest.CaptureFixture[str], query: str
) -> None:
    """FTS5 operators typed by the user are plain text: never a syntax error."""
    found = hits(capsys, "--", query)  # `--`: a query may start with `-`
    assert all(h["id"] == "qp-latin1" for h in found)


def test_words_are_anded_and_operators_are_words(archive: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert [h["id"] for h in hits(capsys, "reunion hotel")] == ["qp-latin1"]
    assert hits(capsys, "reunion budgetaire") == []
    # `-hotel` is the word hotel, not a NOT.
    assert [h["id"] for h in hits(capsys, "reunion -hotel")] == ["qp-latin1"]
    assert fts_query('a "b" NEAR') == '"a" """b""" "NEAR"'


def test_newest_first_limit_and_folder(archive: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sync(archive, {"older": (raw("Subject: o", b"common"), {"receivedDateTime": "2020-01-01T00:00:00Z"})})
    sync(archive, {"newer": (raw("Subject: n", b"common"), {"receivedDateTime": "2027-01-01T00:00:00Z"})})
    assert [h["id"] for h in hits(capsys, "common")] == ["newer", "older"]
    assert [h["id"] for h in hits(capsys, "common", "--limit", "1")] == ["newer"]
    with closing(open_db(archive)) as db:
        db.execute("UPDATE messages SET folder_id='F2' WHERE id='older'")
        db.commit()
    assert [h["id"] for h in hits(capsys, "common", "--folder", "Old")] == ["older"]
    assert cli.main(["search", "common", "--folder", "Nope"]) == 2
    assert "unknown folder" in capsys.readouterr().err


def test_tombstoned_only_with_deleted(archive: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with closing(open_db(archive)) as db:
        db.execute("UPDATE messages SET deleted_at='2026-02-01T00:00:00Z' WHERE id='qp-latin1'")
        db.commit()
    assert hits(capsys, "reunion") == []
    [hit] = hits(capsys, "reunion", "--deleted")
    assert hit["deleted_at"] == "2026-02-01T00:00:00Z"
    assert cli.main(["search", "reunion", "--deleted"]) == 0
    assert "(deleted)" in capsys.readouterr().out


def test_text_output_has_what_show_needs(archive: Path, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert cli.main(["search", "hotel"]) == 0
    first, snippet = capsys.readouterr().out.splitlines()
    sid = short_id("qp-latin1")
    assert first == f"{sid}  2026-01-03T00:00:00Z  Inbox  Ann Example <ann@example.test>  s-qp-latin1"
    assert "[hôtel]" in snippet
    assert cli.main(["show", sid]) == 0


def test_fts_follows_inserts_updates_and_deletes(db: sqlite3.Connection) -> None:
    def ids(q: str) -> list[str]:
        return [r["id"] for r in search(db, q, deleted=True)]

    db.execute(
        "INSERT INTO messages (id, folder_id, subject, search_text, meta, first_seen_at, updated_at) "
        "VALUES ('x', 'F', 'alpha', 'beta', '{}', 't', 't')"
    )
    assert ids("alpha beta") == ["x"]
    db.execute("UPDATE messages SET search_text='gamma' WHERE id='x'")
    assert ids("beta") == [] and ids("gamma") == ["x"]
    db.execute("UPDATE messages SET deleted_at='t' WHERE id='x'")  # tombstone: index untouched
    assert ids("gamma") == ["x"]
    db.execute("DELETE FROM messages WHERE id='x'")
    assert ids("alpha") == []
    db.execute("INSERT INTO messages_fts (messages_fts) VALUES ('integrity-check')")


def _pre_fts_archive(path: Path) -> None:
    """An archive as phase 3 left it: no messages_fts, messages already stored."""
    with closing(open_db(path)) as db:
        for name in ("messages_fts", "messages_fts_insert", "messages_fts_delete", "messages_fts_update"):
            kind = "TABLE" if name == "messages_fts" else "TRIGGER"
            db.execute(f"DROP {kind} {name}")
        db.execute(
            "INSERT INTO messages (id, folder_id, subject, search_text, meta, first_seen_at, updated_at) "
            "VALUES ('old', 'F', 'legacy', 'archived été', '{}', 't', 't')"
        )
        db.commit()
        assert db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'messages_fts%'").fetchall() == []


def test_existing_archive_gains_a_populated_index_in_place(tmp_path: Path) -> None:
    """The index is added to an existing archive and filled from its rows; messages survive."""
    _pre_fts_archive(tmp_path)
    for _ in range(2):
        with closing(open_db(tmp_path)) as db:
            assert [r["id"] for r in search(db, "ete legacy")] == ["old"]
            assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
            db.execute("INSERT INTO messages_fts (messages_fts) VALUES ('integrity-check')")


def test_archive_already_indexed_is_left_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-opening an archive that carries messages_fts and its three triggers writes nothing: it
    opens even while another writer holds the lock, schema and index unchanged."""
    _pre_fts_archive(tmp_path)
    open_db(tmp_path).close()

    def schema(conn: sqlite3.Connection) -> list[tuple[str, str]]:
        return conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()

    with closing(sqlite3.connect(tmp_path / "mail.db", isolation_level=None)) as writer:
        before = (schema(writer), writer.execute("PRAGMA schema_version").fetchone())
        triggers = {r[0] for r in writer.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert triggers == {
            f"{t}_fts_{op}" for t in ("messages", "events") for op in ("insert", "delete", "update")
        }
        monkeypatch.setattr(store, "BUSY_TIMEOUT", 0.05)
        writer.execute("BEGIN IMMEDIATE")
        with closing(open_db(tmp_path)) as db:
            assert [r["id"] for r in search(db, "legacy")] == ["old"]
        writer.execute("ROLLBACK")
        assert (schema(writer), writer.execute("PRAGMA schema_version").fetchone()) == before
        writer.execute("INSERT INTO messages_fts (messages_fts) VALUES ('integrity-check')")


def test_fts_migration_rechecks_under_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another process creating the index between our check and our lock must not make us fail on
    a second CREATE."""
    _pre_fts_archive(tmp_path)
    with closing(open_db(tmp_path)):
        pass  # the "other process": index now exists
    real = store._has_fts
    answers = iter([False])

    def has_fts(db: sqlite3.Connection) -> bool:
        return next(answers, None) is not False and real(db)

    monkeypatch.setattr(store, "_has_fts", has_fts)
    with closing(open_db(tmp_path)) as db:
        assert [r["id"] for r in search(db, "legacy")] == ["old"]


def test_fts_migration_blocked_by_another_writer_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pre_fts_archive(tmp_path)
    monkeypatch.setattr(store, "BUSY_TIMEOUT", 0.05)
    with closing(sqlite3.connect(tmp_path / "mail.db", isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            open_db(tmp_path)
        writer.execute("ROLLBACK")
    with closing(sqlite3.connect(tmp_path / "mail.db")) as check:
        assert (
            check.execute("SELECT name FROM sqlite_master WHERE name LIKE 'messages_fts%'").fetchall() == []
        )


def test_reindex_rebuilds_a_drifted_index(archive: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`reindex` repairs an index out of step with its rows (e.g. rowids renumbered by a VACUUM)."""
    with closing(open_db(archive)) as db:
        db.execute("INSERT INTO messages_fts (messages_fts) VALUES ('delete-all')")
        db.commit()
    assert hits(capsys, "reunion") == []
    assert cli.main(["reindex"]) == 0
    assert [h["id"] for h in hits(capsys, "reunion")] == ["qp-latin1"]


# --- show -----------------------------------------------------------------------------------------

SHOWN = raw(
    """From: =?utf-8?q?Ren=C3=A9e?= <renee@example.test>
To: bob@example.test
Cc: carol@example.test
Date: Fri, 02 Jan 2026 10:00:00 +0000
Subject: =?utf-8?b?UsOpc3Vtw6k=?=
Content-Type: multipart/mixed; boundary=b""",
    b"--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p class=x>Bonjour <b>tout</b> le monde</p>\r\n"
    b"--b\r\nContent-Type: application/pdf; name=doc.pdf\r\n"
    b"Content-Disposition: attachment; filename=doc.pdf\r\n"
    b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0x\r\n--b--\r\n",
)


@pytest.fixture
def shown(env: Path) -> Path:
    meta = {"isRead": False, "flag": {"flagStatus": "flagged"}, "categories": ["Projet", "Été"]}
    sync(env / "data", {"AAMkShared-one": (SHOWN, meta), "AAMkShared-two": (raw("Subject: t", b"x"), {})})
    return env / "data"


def test_show_decodes_headers_meta_attachments_and_body(
    shown: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert cli.main(["show", short_id("AAMkShared-one")]) == 0
    out = capsys.readouterr().out
    for line in (
        "From: Renée <renee@example.test>",
        "To: bob@example.test",
        "Cc: carol@example.test",
        "Date: Fri, 02 Jan 2026 10:00:00 +0000",
        "Subject: Résumé",
        "Folder: Inbox",
        "Read: no  Flag: flagged",
        "Categories: Projet, Été",
        "Id: AAMkShared-one",
        "Attachment: doc.pdf (application/pdf, 6 bytes)",
        "Bonjour tout le monde",
    ):
        assert line in out.splitlines(), line
    assert "class" not in out


def test_show_raw_is_the_verbatim_mime(shown: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    capsysbinary.readouterr()
    assert cli.main(["show", "AAMkShared-one", "--raw"]) == 0
    assert capsysbinary.readouterr().out == SHOWN


@pytest.mark.parametrize("ref", ["AAMkShared-o", "AAMkShared-one", short_id("AAMkShared-one")[:6].upper()])
def test_show_accepts_a_unique_prefix(shown: Path, ref: str, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert cli.main(["show", ref]) == 0
    assert "Subject: Résumé" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("ref", "why"), [("AAMkShared", "ambiguous"), ("zz", "no message"), ("", "no message")]
)
def test_show_unknown_or_ambiguous_exits_2(
    shown: Path, ref: str, why: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["show", ref]) == 2
    assert why in capsys.readouterr().err
    assert cli.main(["attachments", ref, "--out", str(shown.parent / "out")]) == 2


def test_show_without_stored_mime_exits_1(shown: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with closing(open_db(shown)) as db:
        db.execute("UPDATE messages SET mime=NULL, mime_error='http 404' WHERE id='AAMkShared-two'")
        db.commit()
    assert cli.main(["show", "AAMkShared-two"]) == 1
    assert "http 404" in capsys.readouterr().err
    with closing(open_db(shown)) as db:
        db.execute("UPDATE messages SET mime_error=NULL WHERE id='AAMkShared-two'")
        db.commit()
    assert cli.main(["show", "AAMkShared-two"]) == 1
    assert "not fetched yet" in capsys.readouterr().err


def test_resolve_never_matches_case_insensitively(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO messages (id, folder_id, meta, first_seen_at, updated_at) "
        "VALUES ('AbC', 'F', '{}', 't', 't')"
    )
    assert resolve(db, "Ab") == "AbC"
    with pytest.raises(LookupError):
        resolve(db, "ab")


# --- attachments ----------------------------------------------------------------------------------

PDF = bytes(range(256)) * 3
# The CRLF before a boundary belongs to the delimiter (RFC 2046), not to the part.
DOC = "Ligne 1 é\r\nLigne 2 ü".encode()
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(40))
NESTED = b"PK\x03\x04 nested zip \x00\xff"


def att(disposition: str, content_type: str, data: bytes) -> bytes:
    return (
        f"--b\r\nContent-Type: {content_type}\r\nContent-Disposition: {disposition}\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
    ).encode() + b64(data)


INNER = raw(
    "From: x@example.test\nSubject: inner\nContent-Type: multipart/mixed; boundary=c",
    b"--c\r\nContent-Type: text/plain\r\n\r\ninner body\r\n--c\r\nContent-Type: application/zip\r\n"
    b'Content-Disposition: attachment; filename="inner.zip"\r\nContent-Transfer-Encoding: base64\r\n\r\n'
    + b64(NESTED)
    + b"--c--\r\n",
)

ATTACHED = raw(
    "Subject: files\nContent-Type: multipart/mixed; boundary=b",
    b"--b\r\nContent-Type: text/plain\r\n\r\nsee attached\r\n"
    + att("attachment; filename*=utf-8''r%C3%A9sum%C3%A9.pdf", "application/pdf", PDF)
    + (
        b"--b\r\nContent-Type: text/plain; charset=utf-8\r\n"
        b'Content-Disposition: attachment; filename="=?utf-8?q?pr=C3=A9vu.txt?="\r\n'
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\nLigne 1 =C3=A9\r\nLigne 2 =C3=BC\r\n"
    )
    + att('attachment; filename="../evil"', "application/octet-stream", b"evil-1")
    + att('attachment; filename="/etc/x"', "application/octet-stream", b"evil-2")
    + att('attachment; filename="..\\\\x"', "application/octet-stream", b"evil-3")
    + att("attachment; filename*=utf-8''nul%00name%0A.bin", "application/octet-stream", b"evil-4")
    + att('attachment; filename=".."', "application/octet-stream", b"evil-5")
    + att('attachment; filename=".bashrc"', "application/octet-stream", b"evil-6")
    + att('attachment; filename="dup.txt"', "text/plain", b"first")
    + att('attachment; filename="dup.txt"', "text/plain", b"second")
    + att("inline", "image/png", PNG)
    + b"--b\r\nContent-Type: message/rfc822\r\n\r\n"
    + INNER
    + b"\r\n--b--\r\n",
)


def test_attachments_are_byte_identical_decoded_and_confined(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """001-AC9: each attachment, nested ones included, byte-identical to its decoded part; RFC 2231
    and RFC 2047 names decoded; `../evil`, `/etc/x`, `..\\x`, a NUL name never leave DIR; a name
    taken (in this mail or already in DIR) gets a suffix, nothing is overwritten; files are 0600."""
    sync(env / "data", {"m": (ATTACHED, {})})
    out = env / "out"
    out.mkdir()
    (out / "dup.txt").write_bytes(b"pre-existing")

    def outside() -> set[Path]:
        # A reader of a WAL archive creates its -wal/-shm files: the archive dir is not "outside".
        return set(env.rglob("*")) - set(out.rglob("*")) - set((env / "data").rglob("*"))

    before = outside()
    capsys.readouterr()
    assert cli.main(["attachments", "m", "--out", str(out)]) == 0
    printed = capsys.readouterr().out.splitlines()
    files = {p.name: p.read_bytes() for p in out.iterdir()}
    assert files == {
        "résumé.pdf": PDF,
        "prévu.txt": DOC,
        "_evil": b"evil-1",
        "_etc_x": b"evil-2",
        "_x": b"evil-3",
        "nulname.bin": b"evil-4",
        "part-7.bin": b"evil-5",
        "bashrc": b"evil-6",
        "dup.txt": b"pre-existing",
        "dup-1.txt": b"first",
        "dup-2.txt": b"second",
        "part-11.png": PNG,
        "part-12.eml": INNER,
        "inner.zip": NESTED,
    }
    assert sorted(printed) == sorted(str(out.resolve() / n) for n in files if n != "dup.txt")
    assert outside() == before, "nothing written outside DIR"
    assert {p.stat().st_mode & 0o777 for p in out.iterdir() if p.name != "dup.txt"} == {0o600}


def test_attachments_never_follow_a_planted_symlink(env: Path, tmp_path: Path) -> None:
    sync(
        env / "data",
        {
            "m": (
                raw(
                    "Subject: s\nContent-Type: multipart/mixed; boundary=b",
                    att('attachment; filename="a.bin"', "application/octet-stream", b"data") + b"--b--\r\n",
                ),
                {},
            )
        },
    )
    out = env / "out"
    out.mkdir()
    target = tmp_path / "victim"
    target.write_bytes(b"untouched")
    (out / "a.bin").symlink_to(target)
    assert cli.main(["attachments", "m", "--out", str(out)]) == 0
    assert target.read_bytes() == b"untouched"
    assert (out / "a-1.bin").read_bytes() == b"data"


def test_attachments_into_an_unwritable_dir_exit_1(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sync(env / "data", {"m": (ATTACHED, {})})
    out = env / "ro"
    out.mkdir(mode=0o500)
    try:
        if os.access(out, os.W_OK):
            pytest.skip("running as root")
        assert cli.main(["attachments", "m", "--out", str(out)]) == 1
        assert "cannot write attachments" in capsys.readouterr().err
    finally:
        out.chmod(0o700)


@pytest.mark.parametrize(
    ("name", "safe"),
    [
        ("../../etc/passwd", "_.._etc_passwd"),
        ("C:\\Windows\\x.dll", "C:_Windows_x.dll"),
        ("\x00\x01\x1f\x7f", "part-3.bin"),
        ("...", "part-3.bin"),
        ("  .profile ", "profile"),
        ("e\u202egnp.exe", "egnp.exe"),
    ],
)
def test_safe_filename(name: str, safe: str) -> None:
    assert cli.safe_filename(name, "application/octet-stream", 3) == safe


def test_long_names_fit_the_filesystem() -> None:
    name = cli.safe_filename("é" * 300 + ".pdf", "application/pdf", 1)
    assert name.endswith(".pdf") and len(name.encode()) <= 200
    assert len(cli.safe_filename("a." + "x" * 300, "", 1).encode()) <= 200


# --- hostile text, forwarded mails, read-only commands, failed writes -----------------------------

ESCAPES = "\x1b]0;x\x07 \x1b[2J \x9b ‮"
HOSTILE = raw(
    "From: eve@example.test\nSubject: =?utf-8?b?"
    + base64.b64encode(f"hostile {ESCAPES}".encode()).decode()
    + "?=\nContent-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: 8bit",
    f"hostile body {ESCAPES} end".encode(),
)


def test_show_and_search_never_print_terminal_controls(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sync(env / "data", {"m": (HOSTILE, {"subject": f"hostile {ESCAPES}"})})
    capsys.readouterr()
    for argv in (["show", "m"], ["search", "hostile"]):
        assert cli.main(argv) == 0
        out = capsys.readouterr().out
        assert "hostile" in out and "�" in out
        for bad in ("\x1b", "\x07", "\x9b", "‮"):
            assert bad not in out, (argv, bad)
    capsys.readouterr()
    assert cli.main(["search", "--json", "hostile"]) == 0
    line = capsys.readouterr().out
    assert not any(bad in line for bad in ("\x1b", "\x07", "\x9b", "\u202e"))
    hit = json.loads(line)
    assert hit["subject"] == f"hostile {ESCAPES}"
    assert ESCAPES in hit["snippet"]


FORWARDED = raw(
    "Subject: fwd\nContent-Type: multipart/mixed; boundary=b",
    b"--b\r\nContent-Type: text/plain\r\n\r\nsee forwarded\r\n"
    b'--b\r\nContent-Type: message/rfc822; name="=?utf-8?q?R=C3=A9union?="\r\n'
    b"Content-Disposition: attachment\r\n\r\n" + INNER + b"\r\n"
    b'--b\r\nContent-Type: message/rfc822\r\nContent-Disposition: attachment; filename="../x/old.EML"\r\n'
    b"Content-Transfer-Encoding: 8bit\r\n\r\nSubject: caf\xc3\xa9\r\n\r\nbody\r\n--b--\r\n",
)


def test_forwarded_mail_is_written_verbatim_with_its_attachments(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """001-AC9: a forwarded mail is written as `.eml`, byte-identical to the part the outer MIME
    carries (8-bit included), named from its decoded, sanitised name or `part-N.eml` (see
    test_attachments_are_byte_identical_decoded_and_confined); its own attachments follow it."""
    sync(env / "data", {"m": (FORWARDED, {})})
    out = env / "out"
    capsys.readouterr()
    assert cli.main(["attachments", "m", "--out", str(out)]) == 0
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {
        "Réunion.eml": INNER,
        "inner.zip": NESTED,
        "_x_old.EML": b"Subject: caf\xc3\xa9\r\n\r\nbody",
    }
    assert cli.main(["show", "m"]) == 0
    assert f"Attachment: Réunion.eml (message/rfc822, {len(INNER)} bytes)" in capsys.readouterr().out


def test_read_commands_never_create_or_migrate_an_archive(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for argv in (["search", "x"], ["show", "x"], ["attachments", "x", "--out", str(env / "out")]):
        assert cli.main(argv) == 2
        assert "no archive at" in capsys.readouterr().err
    assert not (env / "data").exists()
    _pre_fts_archive(env / "data")
    before = (env / "data" / "mail.db").read_bytes()
    assert cli.main(["search", "legacy"]) == 2
    assert "stop `sync`, then run `minimail reindex`" in capsys.readouterr().err
    assert (env / "data" / "mail.db").read_bytes() == before
    assert cli.main(["reindex"]) == 0
    assert cli.main(["search", "legacy"]) == 0


def test_failed_write_removes_the_partial_file_and_lists_the_written_ones(
    env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sync(env / "data", {"m": (ATTACHED, {})})
    real = os.fdopen
    opened: list[int] = []

    class Full:
        def __init__(self, fd: int, mode: str) -> None:
            self.f = real(fd, mode)

        def __enter__(self) -> Full:
            return self

        def __exit__(self, *exc: object) -> None:
            self.f.close()

        def write(self, data: bytes) -> None:
            opened.append(1)
            self.f.write(data[:3])
            if len(opened) == 2:
                raise OSError(28, "No space left on device")
            self.f.write(data[3:])

    monkeypatch.setattr(cli.os, "fdopen", Full)
    out = env / "out"
    capsys.readouterr()
    assert cli.main(["attachments", "m", "--out", str(out)]) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [str(out.resolve() / "résumé.pdf")]
    assert "No space left" in captured.err
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {"résumé.pdf": PDF}


def test_show_keeps_crlf_body_lines_intact(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    body = b"line one\r\nline two\r\nline three\rline four\r\n"
    sync(env / "data", {"m": (raw("Subject: crlf\nContent-Type: text/plain", body), {})})
    capsys.readouterr()
    assert cli.main(["show", "m"]) == 0
    out = capsys.readouterr().out
    assert "�" not in out
    lines = out.splitlines()
    start = lines.index("line one")
    assert lines[start : start + 4] == ["line one", "line two", "line three", "line four"]


def test_deeply_forwarded_mail_is_opened_three_levels_only() -> None:
    """50 nested forwards: four `.eml` (the 4th listed, not opened), no recursion, small memory."""
    mail = raw("Subject: L50\nContent-Type: text/plain", b"x")
    for level in range(49, -1, -1):
        mail = raw(
            f"Subject: L{level}\nContent-Type: multipart/mixed; boundary=b{level}",
            f"--b{level}\r\nContent-Type: message/rfc822\r\n\r\n".encode()
            + mail
            + f"\r\n--b{level}--\r\n".encode(),
        )
    assert len(mail) < 50_000
    found = mime.attachments(mail)
    assert [a.content_type for a in found] == ["message/rfc822"] * 4
    assert found[3].data.startswith(b"Subject: L4\r\n")


def test_interrupted_write_removes_the_partial_file(
    env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sync(env / "data", {"m": (ATTACHED, {})})
    real = os.fdopen
    writes: list[int] = []

    class Interrupted:
        def __init__(self, fd: int, mode: str) -> None:
            self.f = real(fd, mode)

        def __enter__(self) -> Interrupted:
            return self

        def __exit__(self, *exc: object) -> None:
            self.f.close()

        def write(self, data: bytes) -> None:
            writes.append(1)
            self.f.write(data[:3])
            if len(writes) == 2:
                raise KeyboardInterrupt
            self.f.write(data[3:])

    monkeypatch.setattr(cli.os, "fdopen", Interrupted)
    out = env / "out"
    capsys.readouterr()
    assert cli.main(["attachments", "m", "--out", str(out)]) == 130
    assert capsys.readouterr().out.splitlines() == [str(out.resolve() / "résumé.pdf")]
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {"résumé.pdf": PDF}


# Small (~160 KB) but nested past Python's recursion limit: its email parser raises RecursionError.
DEEP = b"Subject: deep\r\n" + b"Content-Type: message/rfc822\r\n\r\n" * 5000 + b"Subject: core\r\n\r\nx"
DEEP_MULTIPART = (
    b"".join(f"Content-Type: multipart/mixed; boundary=b{i}\r\n\r\n--b{i}\r\n".encode() for i in range(5000))
    + b"Content-Type: text/plain\r\n\r\nx"
)


def test_extract_of_an_absurdly_nested_mail_is_empty() -> None:
    assert mime.extract(DEEP) == mime.Extracted()
    assert mime.extract(DEEP_MULTIPART) == mime.Extracted()


def test_reindex_and_show_survive_an_absurdly_nested_mail(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fine = raw("Subject: fine\nContent-Type: text/plain", b"findable")
    sync(env / "data", {"deep": (DEEP, {}), "flat": (DEEP_MULTIPART, {}), "ok": (fine, {})})
    assert cli.main(["reindex"]) == 0
    assert "reindexed=3" in capsys.readouterr().out
    assert [h["id"] for h in hits(capsys, "findable")] == ["ok"]
    for argv in (["show", "deep"], ["show", "flat"], ["attachments", "flat", "--out", str(env / "out")]):
        assert cli.main(argv) == 1, argv
        assert "too deeply nested" in capsys.readouterr().err
    assert cli.main(["show", "deep", "--raw"]) == 0


def test_single_line_fields_cannot_forge_lines(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mail = raw(
        "Subject: =?utf-8?q?spoof=0Afake-subject=0Dmore?=\nContent-Type: multipart/mixed; boundary=b",
        att("attachment; filename*=utf-8''a%0Afake-name%0Db.bin", "application/octet-stream", b"z")
        + b"--b--\r\n",
    )
    meta = {"subject": "spoof\nfake-subject\rmore", "categories": ["c\nfake-category"]}
    sync(env / "data", {"m": (mail, meta)})
    with closing(open_db(env / "data")) as db:
        db.execute("UPDATE folders SET path='Inbox\nfake-folder\r' WHERE id='F1'")
        db.commit()
    capsys.readouterr()
    assert cli.main(["search", "spoof"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2 and lines[0].endswith(
        "Inbox fake-folder  Ann Example <ann@example.test>  spoof fake-subject more"
    )
    assert cli.main(["show", "m"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert not [line for line in out if line.startswith("fake")]
    for line in (
        "Subject: spoof fake-subject more",
        "Folder: Inbox fake-folder",
        "Categories: c fake-category",
    ):
        assert line in out, line
