"""`minimail` command line."""

import argparse
import hashlib
import json
import mimetypes
import os
import signal
import sqlite3
import sys
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from compression import zstd
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, suppress
from pathlib import Path

import httpx
import structlog
from pydantic import ValidationError

from . import __version__
from .auth import AuthExpired, AuthUnavailable, TokenSource
from .config import Settings
from .folders import sync_folders
from .graph import Graph
from .live import Cadence, run_live, sync_pass
from .logging import setup_logging
from .mail import DOWNLOAD_WORKERS, local_counts, path_id, retry_failed
from .mime import Attachment, attachments, body_text, extract, header, parse
from .search import resolve, search, short_id
from .store import needs_migration, open_db, open_readonly, rebuild_fts

log = structlog.get_logger()


def _graph(settings: Settings) -> Graph:
    return Graph(TokenSource(settings), settings.graph_url, max_rps=settings.max_rps)


def _printable(text: str) -> str:
    """Mail text safe for a terminal: every control/format character (escape sequences, C1, bidi
    overrides) except newline and tab becomes U+FFFD; Unicode spaces (NBSP…) stay."""
    return "".join(
        c if c.isprintable() or c in "\n\t" or unicodedata.category(c) == "Zs" else "\ufffd" for c in text
    )


def _say(text: str) -> None:
    # Security: subjects, senders and bodies are attacker-controlled (CWE-150 terminal injection).
    # Mail bodies keep CRLF: a bare \r would otherwise become U+FFFD at every line end.
    print(_printable(text.replace("\r\n", "\n").replace("\r", "\n")))


def _one_line(value: object) -> str:
    # Security: a newline in a subject or folder name would forge extra output lines.
    return " ".join(str(value).split())


def _print_folders(db: sqlite3.Connection, folders: Sequence[sqlite3.Row]) -> int:
    """Server items, server messages and local messages per folder; `≠` marks (and the return value
    counts) folders whose local count differs from the server's message count."""
    local = local_counts(db)
    differ = 0
    _say(f"{'items':>8} {'messages':>8} {'local':>8}  path")
    for folder in folders:
        mine = local.get(folder["id"], 0)
        messages = folder["message_count"]
        expected = folder["total_count"] if messages is None else messages
        differ += mine != expected
        mark = "" if mine == expected else "  ≠"
        shown = "?" if messages is None else messages
        _say(f"{folder['total_count']:>8} {shown:>8} {mine:>8}  {_one_line(folder['path'])}{mark}")
    return differ


def cmd_folders(settings: Settings, db: sqlite3.Connection, args: argparse.Namespace) -> int:
    graph = _graph(settings)
    try:
        folders = sync_folders(graph, db)
    finally:
        graph.close()
    _print_folders(db, folders)
    return 0


def cmd_sync(settings: Settings, db: sqlite3.Connection, args: argparse.Namespace) -> int:
    if args.live and args.folder:
        print("minimail: --live syncs every folder; it does not take --folder", file=sys.stderr)
        return 2
    graph = _graph(settings)
    executor = ThreadPoolExecutor(DOWNLOAD_WORKERS, thread_name_prefix="mime")
    clean = False
    selected: list[sqlite3.Row] = []
    try:
        # --live walks the tree inside its loop, where a sign-in or network outage is retried.
        if not args.live:
            folders = sync_folders(graph, db)
            selected = [f for f in folders if not args.folder or f["path"] in args.folder]
            if args.folder and len(selected) != len(set(args.folder)):
                print(f"minimail: unknown folder path among {args.folder}", file=sys.stderr)
                return 2
        if args.retry_failed:
            log.info("retry_failed", reset=retry_failed(db))
        if args.live:
            run_live(
                graph,
                db,
                executor,
                Cadence(settings.inbox_every, settings.folders_every, settings.tree_every),
            )
        else:
            total = sync_pass(graph, db, executor, selected)
            log.info("sync_done", folders=len(selected), **vars(total))
        clean = True
    finally:
        # Close first: it wakes workers asleep in a back-off or a pacing slot, so exit is prompt.
        graph.close()
        # Interrupted: drop queued downloads instead of finishing the whole page first.
        executor.shutdown(wait=clean, cancel_futures=not clean)
    if not args.live:
        _print_folders(db, selected)
    return 0


def cmd_verify(settings: Settings, db: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Every stored MIME decompresses to bytes matching its recorded sha256; `--refetch N` also
    re-downloads N random messages and compares them byte for byte."""
    checked = bad = 0
    for row in db.execute("SELECT id, mime, mime_sha256 FROM messages WHERE mime IS NOT NULL"):
        checked += 1
        if hashlib.sha256(zstd.decompress(row["mime"])).hexdigest() != row["mime_sha256"]:
            bad += 1
            log.error("mime_mismatch", id=row["id"])
    refetched = refetch_bad = 0
    if args.refetch:
        graph = _graph(settings)
        try:
            sample = db.execute(
                "SELECT id, mime FROM messages WHERE mime IS NOT NULL AND deleted_at IS NULL "
                "ORDER BY random() LIMIT ?",
                (args.refetch,),
            ).fetchall()
            for row in sample:
                live = graph.request("GET", f"me/messages/{path_id(row['id'])}/$value").content
                refetched += 1
                if live != zstd.decompress(row["mime"]):
                    refetch_bad += 1
                    log.error("mime_differs_from_server", id=row["id"])
        finally:
            graph.close()
    print(f"checked={checked} mismatches={bad} refetched={refetched} refetch_mismatches={refetch_bad}")
    return 1 if bad or refetch_bad else 0


def cmd_reindex(settings: Settings, db: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Rebuild the search index, then recompute every message's search text from its stored MIME (no
    download). Stop `sync` first: the archive has a single writer."""
    # First: the triggers below can only update an index that matches its rows.
    rebuild_fts(db)
    reader = open_readonly(settings.data_dir)
    done = 0
    with closing(reader):
        for row in reader.execute("SELECT id, mime FROM messages WHERE mime IS NOT NULL"):
            text = extract(zstd.decompress(row["mime"])).text
            db.execute("UPDATE messages SET search_text=? WHERE id=?", (text, row["id"]))
            done += 1
            if done % 500 == 0:
                db.commit()
    db.commit()
    print(f"reindexed={done}")
    return 0


def cmd_search(settings: Settings, db: sqlite3.Connection, args: argparse.Namespace) -> int:
    if args.folder and not db.execute("SELECT 1 FROM folders WHERE path=?", (args.folder,)).fetchone():
        print(f"minimail: unknown folder {args.folder!r}", file=sys.stderr)
        return 2
    for row in search(db, args.query, args.folder, args.limit, args.deleted):
        hit = {**dict(row), "short_id": short_id(row["id"]), "snippet": " ".join(row["snippet"].split())}
        if args.json:
            print(json.dumps(hit))  # ASCII: C1 controls and bidi overrides leave \u-escaped
            continue
        gone = "  (deleted)" if row["deleted_at"] else ""
        who = "  ".join(_one_line(row[k]) for k in ("received_at", "folder", "sender", "subject"))
        _say(f"{hit['short_id']}  {who}{gone}")
        _say(f"    {hit['snippet']}")
    return 0


def _stored_mime(db: sqlite3.Connection, ref: str) -> tuple[sqlite3.Row, bytes] | int:
    """The message row and its verbatim MIME, or the exit code after telling the user why not."""
    try:
        msg_id = resolve(db, ref)
    except LookupError as exc:
        print(f"minimail: {exc.args[0]}", file=sys.stderr)
        return 2
    row = db.execute(
        "SELECT m.*, f.path AS folder FROM messages m LEFT JOIN folders f ON f.id = m.folder_id WHERE m.id=?",
        (msg_id,),
    ).fetchone()
    if row["mime"] is None:
        print(
            f"minimail: no MIME stored for {short_id(msg_id)} ({row['mime_error'] or 'not fetched yet'})",
            file=sys.stderr,
        )
        return 1
    return row, zstd.decompress(row["mime"])


def cmd_show(settings: Settings, db: sqlite3.Connection, args: argparse.Namespace) -> int:
    found = _stored_mime(db, args.id)
    if isinstance(found, int):
        return found
    row, raw = found
    if args.raw:
        sys.stdout.flush()
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
        return 0
    try:
        msg = parse(raw)
        body = body_text(msg)
        found_attachments = attachments(raw)
    except RecursionError:
        print("minimail: message too deeply nested to display (use --raw)", file=sys.stderr)
        return 1
    meta = json.loads(row["meta"])
    for name in ("From", "To", "Cc", "Date", "Subject"):
        if value := header(msg, name):
            _say(f"{name}: {value}")
    _say(f"Folder: {_one_line(row['folder'])}")
    flag = (meta.get("flag") or {}).get("flagStatus", "notFlagged")
    _say(f"Read: {'yes' if meta.get('isRead') else 'no'}  Flag: {_one_line(flag)}")
    if categories := meta.get("categories"):
        _say(f"Categories: {', '.join(map(_one_line, categories))}")
    if row["deleted_at"]:
        _say(f"Deleted: {_one_line(row['deleted_at'])}")
    _say(f"Id: {_one_line(row['id'])}")
    for att in found_attachments:
        name = _one_line(att.filename) or "(no name)"
        _say(f"Attachment: {name} ({_one_line(att.content_type)}, {len(att.data)} bytes)")
    print()
    _say(body)
    return 0


# Bytes, not characters: NAME_MAX is 255 bytes and a UTF-8 character takes up to 4.
_MAX_NAME = 200


def safe_filename(name: str, content_type: str, index: int) -> str:
    """A plain file name: no path separator, control character or leading dot (no `..`, no hidden
    file); unnamed parts are named after their position and content type."""
    name = "".join("_" if c in "/\\" else c for c in name if c.isprintable()).strip().lstrip(".").strip()
    if not name:
        name = f"part-{index}{mimetypes.guess_extension(content_type) or '.bin'}"
    stem, ext = os.path.splitext(name)
    if len(ext.encode()) > 16:
        stem, ext = name, ""
    return stem.encode()[: _MAX_NAME - len(ext.encode())].decode(errors="ignore") + ext


def save_attachments(found: Sequence[Attachment], out: Path) -> Iterator[Path]:
    """Write each attachment into `out`, owner-only, never overwriting: a taken name gets `-N`.
    Yields each path once written; a failed write leaves no partial file behind."""
    out.mkdir(parents=True, exist_ok=True)
    out = out.resolve()
    # Security: names are opened relative to the directory fd, so a swapped path cannot redirect them.
    dir_fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, att in enumerate(found, 1):
            stem, ext = os.path.splitext(safe_filename(att.filename, att.content_type, index))
            for n in range(1000):
                name = f"{stem}-{n}{ext}" if n else f"{stem}{ext}"
                try:
                    fd = os.open(
                        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd
                    )
                except FileExistsError:
                    continue
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(att.data)
                except BaseException:  # Ctrl-C / SIGTERM mid-write too
                    with suppress(OSError):
                        os.unlink(name, dir_fd=dir_fd)
                    raise
                yield out / name
                break
            else:
                raise FileExistsError(f"no free name for {stem}{ext} in {out}")
    finally:
        os.close(dir_fd)


def cmd_attachments(settings: Settings, db: sqlite3.Connection, args: argparse.Namespace) -> int:
    found = _stored_mime(db, args.id)
    if isinstance(found, int):
        return found
    try:
        found_attachments = attachments(found[1])
    except RecursionError:
        print("minimail: message too deeply nested to extract (use show --raw)", file=sys.stderr)
        return 1
    try:
        for path in save_attachments(found_attachments, args.out):
            print(path, flush=True)
    except OSError as exc:
        print(
            f"minimail: cannot write attachments: {exc} (the files listed above were written)",
            file=sys.stderr,
        )
        return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minimail", description=__doc__)
    parser.add_argument("--version", action="version", version=f"minimail {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("folders", help="sync the folder tree; list server vs local counts")
    sync = sub.add_parser("sync", help="mirror every folder's messages (first run = full backfill)")
    sync.add_argument("--live", action="store_true", help="keep syncing forever (Ctrl-C / SIGTERM stops)")
    sync.add_argument("--retry-failed", action="store_true", help="retry messages whose download failed")
    sync.add_argument("--folder", action="append", metavar="PATH", help="only this folder path (repeatable)")
    verify = sub.add_parser("verify", help="check every stored MIME against its sha256")
    verify.add_argument(
        "--refetch", type=int, default=0, metavar="N", help="also compare N random messages to the server"
    )
    sub.add_parser(
        "reindex", help="recompute search text and the search index from stored MIME (stop sync first)"
    )
    search_p = sub.add_parser("search", help="full-text search (words ANDed, accent-insensitive)")
    search_p.add_argument("query")
    search_p.add_argument("--folder", metavar="PATH", help="only this folder path")
    search_p.add_argument("--limit", type=int, default=20, metavar="N", help="at most N hits (default 20)")
    search_p.add_argument("--deleted", action="store_true", help="include messages deleted on the server")
    search_p.add_argument("--json", action="store_true", help="one JSON object per line")
    show = sub.add_parser("show", help="headers, flags, attachment list and text of a message")
    show.add_argument("id", help="message id, or a unique prefix of it or of its short id")
    show.add_argument("--raw", action="store_true", help="write the verbatim MIME to stdout")
    att = sub.add_parser("attachments", help="extract a message's attachments")
    att.add_argument("id", help="message id, or a unique prefix of it or of its short id")
    att.add_argument("--out", type=Path, required=True, metavar="DIR", help="directory to write into")
    return parser


# Read-only commands never create, chmod or migrate the archive, and never take its write lock.
READERS = frozenset({"search", "show", "attachments"})

COMMANDS: dict[str, Callable[[Settings, sqlite3.Connection, argparse.Namespace], int]] = {
    "folders": cmd_folders,
    "sync": cmd_sync,
    "verify": cmd_verify,
    "reindex": cmd_reindex,
    "search": cmd_search,
    "show": cmd_show,
    "attachments": cmd_attachments,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Security: the archive is personal data — every file this process creates is owner-only.
    os.umask(0o077)
    # SIGTERM (systemd, kill) stops like Ctrl-C: same cleanup, same resumable cursor.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    # A process started in the background by a non-interactive shell inherits SIGINT ignored.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        settings = Settings()  # type: ignore[call-arg]  # tenant_id comes from the environment
    except ValidationError as exc:
        names = [f"MINIMAIL_{str(e['loc'][0]).upper()}" for e in exc.errors() if e["loc"]]
        missing = ", ".join(names) or str(exc)
        print(f"minimail: invalid or missing setting(s): {missing} (env or .env)", file=sys.stderr)
        return 2
    setup_logging(settings.log_level)
    log.info("startup", version=__version__, command=args.command)
    reader = args.command in READERS
    if reader and not settings.db_path.exists():
        print(f"minimail: no archive at {settings.db_path} (check MINIMAIL_DATA_DIR)", file=sys.stderr)
        return 2
    try:
        with closing((open_readonly if reader else open_db)(settings.data_dir)) as db:
            if reader and needs_migration(db):
                print(
                    "minimail: the archive predates this version: "
                    "stop `sync`, then run `minimail reindex` once",
                    file=sys.stderr,
                )
                return 2
            return COMMANDS[args.command](settings, db, args)
    except KeyboardInterrupt:
        log.info("interrupted")
        return 130
    except AuthExpired as exc:
        print(f"minimail: {exc}", file=sys.stderr)
        return 2
    except AuthUnavailable as exc:
        print(f"minimail: {exc} (retry later)", file=sys.stderr)
        return 3
    except httpx.HTTPError as exc:
        print(f"minimail: Graph request failed: {exc}", file=sys.stderr)
        return 1
    except sqlite3.OperationalError as exc:
        # The traceback goes to the log: the same error class covers a locked archive and a code bug.
        log.exception("archive_error", db=str(settings.db_path))
        print(f"minimail: archive error: {exc} ({settings.db_path})", file=sys.stderr)
        return 1
