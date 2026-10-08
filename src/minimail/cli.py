"""`minimail` command line."""

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import sys
import unicodedata
from collections.abc import Callable, Sequence
from compression import zstd
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, suppress
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import structlog
from pydantic import ValidationError

from . import __version__, calendar
from .auth import AuthExpired, AuthUnavailable, TokenSource
from .config import Settings
from .folders import sync_folders
from .graph import GRAPH_URL, Graph
from .live import FOLDER_ERRORS, Cadence, calendar_round, folder_failed, run_live, sync_pass
from .logging import setup_logging
from .mail import (
    DOWNLOAD_WORKERS,
    Reconciled,
    local_counts,
    path_id,
    reconcile_folder,
    retry_failed,
)
from .mcp import Server
from .mime import attachments, extract, save_attachments
from .search import MimeMissing, message_view, resolve, search, short_id, stored_mime
from .store import needs_migration, open_db, open_readonly, rebuild_fts

log = structlog.get_logger()


def _graph(settings: Settings) -> Graph:
    return Graph(TokenSource(settings), GRAPH_URL, max_rps=settings.max_rps)


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
    failed = 0
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
                Cadence(
                    settings.inbox_every,
                    settings.folders_every,
                    settings.tree_every,
                    settings.reconcile_every,
                ),
            )
        else:
            total, failed = sync_pass(graph, db, executor, selected)
            if not args.folder and not calendar_round(graph, db, executor):
                failed += 1
            log.info("sync_done", folders=len(selected), failed_folders=failed, **vars(total))
        clean = True
    finally:
        # Close first: it wakes workers asleep in a back-off or a pacing slot, so exit is prompt.
        graph.close()
        # Interrupted: drop queued downloads instead of finishing the whole page first.
        executor.shutdown(wait=clean, cancel_futures=not clean)
    if not args.live:
        _print_folders(db, selected)
    return 1 if failed else 0


def cmd_reconcile(settings: Settings, db: sqlite3.Connection, args: argparse.Namespace) -> int:
    graph = _graph(settings)
    try:
        # A fresh tree: a parent the last walk has not seen would read as a deletion.
        folders = sync_folders(graph, db)
        selected = [f for f in folders if not args.folder or f["path"] in args.folder]
        if args.folder and len(selected) != len(set(args.folder)):
            print(f"minimail: unknown folder path among {args.folder}", file=sys.stderr)
            return 2
        total = Reconciled()
        failed = 0
        for folder in selected:
            try:
                c = reconcile_folder(graph, db, folder["id"])
            except FOLDER_ERRORS as exc:
                folder_failed(exc, folder["path"], "folder_reconcile_failed")
                failed += 1
                continue
            log.info("folder_reconciled", path=folder["path"], **c.summary())
            for name, value in c.summary().items():
                setattr(total, name, getattr(total, name) + value)
    finally:
        graph.close()
    print(
        f"{len(selected)} folders, {failed} failed, {total.listed} messages on the server: "
        f"{total.deleted} marked deleted, {total.moved} moved, {total.revived} restored, "
        f"{total.unsynced} not synced yet"
    )
    return 1 if failed else 0


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
            found = extract(zstd.decompress(row["mime"]))
            if found.error:
                log.warning("extract_failed", error=found.error, short_id=short_id(row["id"]))
            db.execute(
                "UPDATE messages SET search_text=?, ical_uid=? WHERE id=?",
                (found.text, found.ical_uid, row["id"]),
            )
            done += 1
            if done % 500 == 0:
                db.commit()
    db.commit()
    print(f"reindexed={done}")
    return 0


def _search_events(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    if args.folder:
        print("minimail: --folder does not apply to --events", file=sys.stderr)
        return 2
    for row in calendar.search_events(db, args.query, args.limit, args.deleted):
        hit = {**dict(row), "short_id": short_id(row["id"]), "snippet": " ".join(row["snippet"].split())}
        if args.json:
            print(json.dumps(hit))  # ASCII: C1 controls and bidi overrides leave \u-escaped
            continue
        gone = "  (deleted)" if row["deleted_at"] else ""
        _say(f"{hit['short_id']}  {calendar.local(row['start_utc'])}  {_one_line(row['subject'])}{gone}")
        _say(f"    {hit['snippet']}")
    return 0


def cmd_search(settings: Settings, db: sqlite3.Connection, args: argparse.Namespace) -> int:
    if args.events:
        return _search_events(db, args)
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
        return stored_mime(db, ref)
    except LookupError as exc:
        print(f"minimail: {exc.args[0]}", file=sys.stderr)
        return 2
    except MimeMissing as exc:
        print(f"minimail: {exc}", file=sys.stderr)
        return 1


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
        shown = message_view(row, raw)
    except RecursionError:
        print("minimail: message too deeply nested to display (use --raw)", file=sys.stderr)
        return 1
    for name, value in shown["headers"].items():
        _say(f"{name}: {value}")
    _say(f"Folder: {_one_line(shown['folder'])}")
    _say(f"Read: {'yes' if shown['read'] else 'no'}  Flag: {_one_line(shown['flag'])}")
    if shown["categories"]:
        _say(f"Categories: {', '.join(map(_one_line, shown['categories']))}")
    if shown["deleted_at"]:
        _say(f"Deleted: {_one_line(shown['deleted_at'])}")
    _say(f"Id: {_one_line(shown['id'])}")
    for att in shown["attachments"]:
        name = _one_line(att["filename"]) or "(no name)"
        _say(f"Attachment: {name} ({_one_line(att['content_type'])}, {att['size']} bytes)")
    print()
    _say(shown["body"])
    return 0


def _when(hit: dict[str, Any]) -> str:
    if hit["all_day"]:
        return f"{hit['start']}{'' if hit['end'] == hit['start'] else ' – ' + hit['end']} (all day)"
    until = hit["end"].rsplit(" ", 1)[1] if hit["end"][:14] == hit["start"][:14] else hit["end"]
    return f"{hit['start']}–{until}"


def cmd_agenda(settings: Settings, db: sqlite3.Connection, args: argparse.Namespace) -> int:
    first = args.since or date.today()
    for row, data in calendar.agenda(db, first, args.until or first + timedelta(days=7)):
        hit = calendar.summary(row, data)
        if args.json:
            print(json.dumps(hit))
            continue
        line = f"{hit['short_id']}  {_when(hit)}  {_one_line(hit['subject'])}"
        if hit["organizer"]:
            line += f"  ({_one_line(hit['organizer'])})"
        if hit["location"]:
            line += f"  @ {_one_line(hit['location'])}"
        _say(line + ("  [Teams]" if hit["teams"] else ""))
    return 0


def cmd_event(settings: Settings, db: sqlite3.Connection, args: argparse.Namespace) -> int:
    try:
        event_id = resolve(db, args.id, "events")
    except LookupError as exc:
        print(f"minimail: {exc.args[0]}", file=sys.stderr)
        return 2
    found, body = calendar.view(db, event_id)
    if args.json:
        print(json.dumps(found))
        return 0
    _say(f"Subject: {_one_line(found['subject'])}")
    _say(f"When: {_when(found)}")
    for label, key in (("Type", "type"), ("Organizer", "organizer"), ("Location", "location")):
        if found[key]:
            _say(f"{label}: {_one_line(found[key])}")
    if found["attendees"]:
        _say(f"Attendees: {', '.join(map(_one_line, found['attendees']))}")
    if found["cancelled"]:
        _say("Cancelled: yes")
    if found["deleted_at"]:
        _say(f"Deleted: {_one_line(found['deleted_at'])}")
    if found["meeting_thread_id"]:
        _say(f"Teams meeting chat: {_one_line(found['meeting_thread_id'])}")
    if found["ical_uid"]:
        _say(f"iCalUId: {_one_line(found['ical_uid'])}")
    for mail in found["mails"]:
        who = "  ".join(_one_line(mail[k] or "") for k in ("received_at", "subject"))
        _say(f"Mail: {mail['short_id']}  {who}{'  (deleted)' if mail['deleted_at'] else ''}")
    _say(f"Id: {_one_line(found['id'])}")
    print()
    _say(body)
    return 0


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
        args.out.mkdir(parents=True, exist_ok=True)
        out = args.out.resolve()
        dir_fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for path in save_attachments(found_attachments, dir_fd, out):
                print(path, flush=True)
        finally:
            os.close(dir_fd)
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
    reconcile = sub.add_parser(
        "reconcile", help="mark deleted/moved the local messages a folder no longer lists on the server"
    )
    reconcile.add_argument(
        "--folder", action="append", metavar="PATH", help="only this folder path (repeatable)"
    )
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
    search_p.add_argument("--events", action="store_true", help="search calendar events instead of mail")
    agenda_p = sub.add_parser("agenda", help="calendar events overlapping a period, local time")
    agenda_p.add_argument(
        "--from", dest="since", type=date.fromisoformat, metavar="DATE", help="first day (default today)"
    )
    agenda_p.add_argument(
        "--to", dest="until", type=date.fromisoformat, metavar="DATE", help="last day (default first + 7)"
    )
    agenda_p.add_argument("--json", action="store_true", help="one JSON object per line")
    event_p = sub.add_parser("event", help="an event's details, linked mails and Teams meeting chat id")
    event_p.add_argument("id", help="event id, or a unique prefix of it or of its short id")
    event_p.add_argument("--json", action="store_true", help="one JSON object")
    show = sub.add_parser("show", help="headers, flags, attachment list and text of a message")
    show.add_argument("id", help="message id, or a unique prefix of it or of its short id")
    show.add_argument("--raw", action="store_true", help="write the verbatim MIME to stdout")
    att = sub.add_parser("attachments", help="extract a message's attachments")
    att.add_argument("id", help="message id, or a unique prefix of it or of its short id")
    att.add_argument("--out", type=Path, required=True, metavar="DIR", help="directory to write into")
    sub.add_parser("mcp", help="MCP server over stdio for Claude (read-only on the archive, no network)")
    return parser


# Read-only commands never create, chmod or migrate the archive, and never take its write lock.
READERS = frozenset({"search", "show", "attachments", "agenda", "event"})

COMMANDS: dict[str, Callable[[Settings, sqlite3.Connection, argparse.Namespace], int]] = {
    "folders": cmd_folders,
    "sync": cmd_sync,
    "reconcile": cmd_reconcile,
    "verify": cmd_verify,
    "reindex": cmd_reindex,
    "search": cmd_search,
    "show": cmd_show,
    "attachments": cmd_attachments,
    "agenda": cmd_agenda,
    "event": cmd_event,
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
    if args.command == "mcp":
        # The server opens the archive per call: a missing or old one is a tool error, not a dead server.
        with suppress(KeyboardInterrupt):
            return Server(settings).serve()
        return 130
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
