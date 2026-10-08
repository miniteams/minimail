import os
import signal
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from minimail import cli

from .conftest import BASE, make_graph


def test_no_token_exits_2_pointing_at_miniteams_login(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """001-AC10: without a usable token the command exits non-zero with a clear message."""
    assert cli.main(["folders"]) == 2
    assert "miniteams login" in capsys.readouterr().err


def test_missing_tenant_exits_2(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MINIMAIL_TENANT_ID")
    assert cli.main(["folders"]) == 2
    assert "MINIMAIL_TENANT_ID" in capsys.readouterr().err


@pytest.mark.parametrize("precreated", [False, True])
def test_cli_archive_is_owner_only(env: Path, monkeypatch: pytest.MonkeyPatch, precreated: bool) -> None:
    """001-AC13: through the CLI, whatever the inherited umask or an existing (e.g. NAS) data dir's
    mode, the archive is 0700 and its files 0600."""
    os.umask(0o002)
    data = env / "data"
    if precreated:
        data.mkdir(mode=0o755)
        data.chmod(0o755)
    monkeypatch.setattr(cli, "sync_folders", lambda graph, db: [])
    assert cli.main(["folders"]) == 0
    assert data.stat().st_mode & 0o777 == 0o700
    modes = {p.name: p.stat().st_mode & 0o777 for p in data.iterdir()}
    assert modes and set(modes.values()) == {0o600}, modes


def _seed(env: Path, monkeypatch: pytest.MonkeyPatch, fake: object) -> None:
    from minimail.store import open_db

    from .conftest import make_graph

    def folders(graph: object, db: object) -> list[object]:
        db.execute(  # type: ignore[attr-defined]
            "INSERT OR REPLACE INTO folders (id, display_name, path, total_count) "
            "VALUES ('F1', 'Inbox', 'Inbox', 2)"
        )
        db.commit()  # type: ignore[attr-defined]
        return db.execute("SELECT * FROM folders").fetchall()  # type: ignore[attr-defined, no-any-return]

    monkeypatch.setattr(cli, "sync_folders", folders)
    monkeypatch.setattr(cli, "_graph", lambda settings: make_graph(fake))  # type: ignore[arg-type]
    open_db(env / "data").close()


def test_sync_then_verify_then_corruption(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """001-AC2: verify passes on a fresh archive, refetch matches the server, a flipped byte fails."""
    from compression import zstd

    from minimail.store import open_db

    from .test_mail import FakeGraph, item

    fake = FakeGraph([[item("m1"), item("m2")]])
    _seed(env, monkeypatch, fake)
    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "       2        ?        2  Inbox" in out and "≠" not in out
    assert cli.main(["verify", "--refetch", "2"]) == 0
    assert "checked=2 mismatches=0 refetched=2 refetch_mismatches=0" in capsys.readouterr().out
    with closing(open_db(env / "data")) as db:
        bad = zstd.compress(b"Subject: tampered\r\n\r\nx")
        db.execute("UPDATE messages SET mime=? WHERE id='m1'", (bad,))
        db.commit()
        assert cli.main(["verify"]) == 1
        assert "mismatches=1" in capsys.readouterr().out


def test_verify_refetch_detects_server_drift(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """001-AC2: the stored bytes are compared to a fresh GET $value, not only to their own hash."""
    from minimail.store import open_db

    from .test_mail import FakeGraph, item

    fake = FakeGraph([[item("m1")]])
    _seed(env, monkeypatch, fake)
    assert cli.main(["sync"]) == 0
    with closing(open_db(env / "data")) as db:
        import hashlib
        from compression import zstd

        other = b"Subject: other\r\n\r\ny"
        db.execute(
            "UPDATE messages SET mime=?, mime_sha256=?",
            (zstd.compress(other), hashlib.sha256(other).hexdigest()),
        )
        db.commit()
        capsys.readouterr()
        assert cli.main(["verify", "--refetch", "1"]) == 1
        assert "refetch_mismatches=1" in capsys.readouterr().out


def test_sync_unknown_folder_exits_2(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from .test_mail import FakeGraph

    _seed(env, monkeypatch, FakeGraph([[]]))
    assert cli.main(["sync", "--folder", "Nope"]) == 2


def test_reindex_recomputes_search_text(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from minimail.store import open_db

    from .test_mail import FakeGraph, item

    _seed(env, monkeypatch, FakeGraph([[item("m1")]]))
    assert cli.main(["sync"]) == 0
    with closing(open_db(env / "data")) as db:
        db.execute("UPDATE messages SET search_text='stale'")
        db.commit()
        assert cli.main(["reindex"]) == 0
        assert "reindexed=1" in capsys.readouterr().out
        assert "déjà" in db.execute("SELECT search_text FROM messages").fetchone()[0]


def test_live_rejects_folder_before_any_graph_call(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_graph(settings: object) -> None:
        raise AssertionError("no Graph client for a rejected command line")

    monkeypatch.setattr(cli, "_graph", no_graph)
    assert cli.main(["sync", "--live", "--folder", "Inbox"]) == 2
    assert "--live" in capsys.readouterr().err


@pytest.fixture
def _sigterm() -> Iterator[None]:
    # main() maps SIGTERM to KeyboardInterrupt process-wide: give pytest its handler back. Until
    # main() installs its own, a SIGTERM is ignored rather than killing the test run.
    old = signal.signal(signal.SIGTERM, lambda signum, frame: None)
    yield
    signal.signal(signal.SIGTERM, old)


@pytest.mark.usefixtures("_sigterm")
@pytest.mark.parametrize("stop", ["sigterm", "keyboard_interrupt"])
def test_live_stops_with_130_on_sigterm_or_ctrl_c(
    env: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    """`sync --live` runs with the configured cadences until SIGTERM / Ctrl-C, then exits 130."""
    from .test_mail import FakeGraph

    _seed(env, monkeypatch, FakeGraph([[]]))
    monkeypatch.setenv("MINIMAIL_INBOX_EVERY", "7")
    cadences: list[object] = []

    def run_live(graph: object, db: object, executor: object, cadence: object) -> None:
        cadences.append(cadence)
        if stop == "sigterm":
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_live", run_live)
    assert cli.main(["sync", "--live"]) == 130
    assert cadences == [cli.Cadence(7.0, 300.0, 900.0)]


@pytest.fixture
def _sigint_ignored() -> Iterator[None]:
    # What a non-interactive shell's `cmd &` hands its child; pytest's own handler comes back after.
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    yield
    signal.signal(signal.SIGINT, old)


@pytest.mark.usefixtures("_sigint_ignored")
def test_sigint_stops_even_when_inherited_ignored(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`kill -INT` / Ctrl-C stops a process launched in the background with SIGINT ignored: exit 130."""
    from .test_mail import FakeGraph

    _seed(env, monkeypatch, FakeGraph([[]]))
    monkeypatch.setattr(cli, "run_live", lambda *args: os.kill(os.getpid(), signal.SIGINT))
    assert cli.main(["sync", "--live"]) == 130


def test_graph_is_paced_by_max_rps(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from minimail.config import Settings

    assert cli._graph(Settings())._interval == 1 / 8  # type: ignore[call-arg]
    monkeypatch.setenv("MINIMAIL_MAX_RPS", "2")
    assert cli._graph(Settings())._interval == 0.5  # type: ignore[call-arg]


def test_live_starts_and_retries_while_signed_out(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """001-AC10 (live): `sync --live` with no usable token does not exit at start-up; it logs the
    failed cycle and keeps retrying (here stopped after two waits)."""
    import json
    from functools import partial

    from minimail import live

    waits: list[float] = []
    now = [0.0]

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        now[0] += seconds
        return len(waits) == 2

    monkeypatch.setattr(cli, "run_live", partial(live.run_live, wait=wait, clock=lambda: now[0]))
    assert cli.main(["sync", "--live"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [e["error"] for e in events if e["event"] == "live_cycle_failed"] == ["AuthExpired"] * 2
    assert waits == [30.0, 30.0]


def test_interrupt_closes_graph_before_draining_workers(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Closing the client first wakes workers asleep in a back-off, so the stop is not held by them."""
    from concurrent.futures import ThreadPoolExecutor

    from .conftest import make_graph
    from .test_mail import FakeGraph

    order: list[str] = []
    graph = make_graph(FakeGraph([[]]))
    real_close = graph.close

    def close() -> None:
        order.append("graph.close")
        real_close()

    class Pool(ThreadPoolExecutor):
        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            order.append("executor.shutdown")
            super().shutdown(wait, cancel_futures=cancel_futures)

    def interrupted(*args: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(graph, "close", close)
    monkeypatch.setattr(cli, "_graph", lambda settings: graph)
    monkeypatch.setattr(cli, "ThreadPoolExecutor", Pool)
    monkeypatch.setattr(cli, "run_live", interrupted)
    assert cli.main(["sync", "--live"]) == 130
    assert order == ["graph.close", "executor.shutdown"]


def test_folder_listing_compares_local_to_the_message_count(
    db: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """001-AC1: ≠ flags a folder whose local count differs from messages/$count, not from the item
    count (which includes non-mail items); without a message count, the item count is the reference."""
    rows = [
        ("A", "Deleted Items", 9, 7),  # local 7: matches the messages, not the items
        ("B", "Inbox", 3, 3),  # local 2: one message missing
        ("C", "Odd", 4, None),  # local 4: no $count, matches the items
        ("D", "Odd2", 5, None),  # local 4: no $count, differs from the items
    ]
    local = {"A": 7, "B": 2, "C": 4, "D": 4}
    for fid, path, items, messages in rows:
        db.execute(
            "INSERT INTO folders (id, display_name, path, total_count, message_count) VALUES (?, ?, ?, ?, ?)",
            (fid, path, path, items, messages),
        )
        for n in range(local[fid]):
            db.execute(
                "INSERT INTO messages (id, folder_id, meta, first_seen_at, updated_at) "
                "VALUES (?, ?, '{}', 't', 't')",
                (f"{fid}{n}", fid),
            )
    folders = db.execute("SELECT * FROM folders ORDER BY path").fetchall()
    assert cli._print_folders(db, folders) == 2
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "   items messages    local  path",
        "       9        7        7  Deleted Items",
        "       3        3        2  Inbox  ≠",
        "       4        ?        4  Odd",
        "       5        ?        4  Odd2  ≠",
    ]


def _stderr(capsys: pytest.CaptureFixture[str]) -> tuple[list[str], list[dict[str, object]]]:
    """minimail: lines, and the JSON log records (one per line, nothing else allowed)."""
    import json

    lines = capsys.readouterr().err.splitlines()
    messages = [line for line in lines if line.startswith("minimail:")]
    records = [json.loads(line) for line in lines if not line.startswith("minimail:")]
    return messages, records


def test_locked_archive_is_a_one_line_error(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Another writer holding the archive past the busy timeout: exit 1 with one stderr line; the
    traceback sits in a single JSON log record."""
    from minimail import store

    data = env / "data"
    data.mkdir()
    with closing(sqlite3.connect(data / "mail.db")) as old:
        old.execute(
            "CREATE TABLE folders (id TEXT PRIMARY KEY, parent_id TEXT, display_name TEXT NOT NULL, "
            "path TEXT NOT NULL, total_count INTEGER NOT NULL DEFAULT 0, deleted_at TEXT)"
        )
        old.commit()
    monkeypatch.setattr(store, "BUSY_TIMEOUT", 0.05)
    with closing(sqlite3.connect(data / "mail.db", isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        assert cli.main(["folders"]) == 1
        writer.execute("ROLLBACK")
    messages, records = _stderr(capsys)
    assert len(messages) == 1 and "archive error: database is locked" in messages[0]
    [record] = [r for r in records if r["event"] == "archive_error"]
    assert "OperationalError: database is locked" in str(record["exception"])


def test_archive_code_bug_keeps_its_traceback(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An OperationalError from a code bug (e.g. a misspelt column) is not passed off as a busy
    archive: neutral message, exit 1, and the traceback names the failing call."""

    def buggy(settings: object, db: sqlite3.Connection, args: object) -> int:
        db.execute("SELECT no_such_column FROM folders")
        return 0

    monkeypatch.setitem(cli.COMMANDS, "folders", buggy)
    assert cli.main(["folders"]) == 1
    messages, records = _stderr(capsys)
    assert len(messages) == 1 and "archive error: no such column" in messages[0]
    [record] = [r for r in records if r["event"] == "archive_error"]
    assert "in buggy" in str(record["exception"])


def test_folder_listings_never_print_terminal_controls(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hostile = "\x1b]0;x\x07 evil \u202e \x9b\nfake-line\r"
    folder = {"id": "A", "displayName": hostile, "childFolderCount": 0, "totalItemCount": 0}
    tree = {"me/mailFolders": [folder]}

    def handler(req: httpx.Request) -> httpx.Response:
        key = req.url.path.removeprefix("/v1.0/")
        if key.endswith("/delta"):
            return httpx.Response(200, json={"value": [], "@odata.deltaLink": f"{BASE}/{key}?deltatoken=t"})
        return httpx.Response(200, json={"value": tree[key]})

    monkeypatch.setattr(cli, "_graph", lambda settings: make_graph(handler))
    for argv in (["folders"], ["sync"]):
        capsys.readouterr()
        assert cli.main(argv) == 0
        out = capsys.readouterr().out
        assert "evil" in out, argv
        assert not [line for line in out.splitlines() if line.startswith("fake")], argv
        assert not any(bad in out for bad in ("\x1b", "\x07", "\x9b", "\u202e")), argv
