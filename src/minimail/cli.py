"""`minimail` command line."""

import argparse
import hashlib
import os
import signal
import sqlite3
import sys
from collections.abc import Callable, Sequence
from compression import zstd
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

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
from .mime import extract
from .store import open_db, open_readonly

log = structlog.get_logger()


def _graph(settings: Settings) -> Graph:
    return Graph(TokenSource(settings), settings.graph_url, max_rps=settings.max_rps)


def _print_folders(db: sqlite3.Connection, folders: Sequence[sqlite3.Row]) -> int:
    """Server items, server messages and local messages per folder; `≠` marks (and the return value
    counts) folders whose local count differs from the server's message count."""
    local = local_counts(db)
    differ = 0
    print(f"{'items':>8} {'messages':>8} {'local':>8}  path")
    for folder in folders:
        mine = local.get(folder["id"], 0)
        messages = folder["message_count"]
        expected = folder["total_count"] if messages is None else messages
        differ += mine != expected
        mark = "" if mine == expected else "  ≠"
        shown = "?" if messages is None else messages
        print(f"{folder['total_count']:>8} {shown:>8} {mine:>8}  {folder['path']}{mark}")
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
    """Recompute every message's search text from its stored MIME (no download). Stop `sync` first:
    the archive has a single writer."""
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
    sub.add_parser("reindex", help="recompute search text from stored MIME (stop sync first)")
    return parser


COMMANDS: dict[str, Callable[[Settings, sqlite3.Connection, argparse.Namespace], int]] = {
    "folders": cmd_folders,
    "sync": cmd_sync,
    "verify": cmd_verify,
    "reindex": cmd_reindex,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Security: the archive is personal data — every file this process creates is owner-only.
    os.umask(0o077)
    # SIGTERM (systemd, kill) stops like Ctrl-C: same cleanup, same resumable cursor.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        settings = Settings()  # type: ignore[call-arg]  # tenant_id comes from the environment
    except ValidationError as exc:
        names = [f"MINIMAIL_{str(e['loc'][0]).upper()}" for e in exc.errors() if e["loc"]]
        missing = ", ".join(names) or str(exc)
        print(f"minimail: invalid or missing setting(s): {missing} (env or .env)", file=sys.stderr)
        return 2
    setup_logging(settings.log_level)
    log.info("startup", version=__version__, command=args.command)
    try:
        with closing(open_db(settings.data_dir)) as db:
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
