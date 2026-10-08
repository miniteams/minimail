"""`minimail` command line."""

import argparse
import os
import sys
from collections.abc import Callable, Sequence

import httpx
import structlog
from pydantic import ValidationError

from . import __version__
from .auth import AuthExpired, AuthUnavailable, TokenSource
from .config import Settings
from .folders import sync_folders
from .graph import Graph
from .logging import setup_logging
from .store import open_db

log = structlog.get_logger()


def _graph(settings: Settings) -> Graph:
    return Graph(TokenSource(settings), settings.graph_url)


def cmd_folders(settings: Settings, args: argparse.Namespace) -> int:
    graph = _graph(settings)
    try:
        folders = sync_folders(graph, open_db(settings.data_dir))
    finally:
        graph.close()
    for folder in folders:
        print(f"{folder['total_count']:>8}  {folder['path']}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minimail", description=__doc__)
    parser.add_argument("--version", action="version", version=f"minimail {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("folders", help="sync the folder tree and list it with server item counts")
    return parser


COMMANDS: dict[str, Callable[[Settings, argparse.Namespace], int]] = {"folders": cmd_folders}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Security: the archive is personal data — every file this process creates is owner-only.
    os.umask(0o077)
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
        return COMMANDS[args.command](settings, args)
    except AuthExpired as exc:
        print(f"minimail: {exc}", file=sys.stderr)
        return 2
    except AuthUnavailable as exc:
        print(f"minimail: {exc} (retry later)", file=sys.stderr)
        return 3
    except httpx.HTTPError as exc:
        print(f"minimail: Graph request failed: {exc}", file=sys.stderr)
        return 1
