import os
import signal
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from minimail import cli


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("MINIMAIL_TENANT_ID", "tenant-test")
    monkeypatch.setenv("MINIMAIL_MSAL_CACHE", str(tmp_path / "absent.json"))
    monkeypatch.setenv("MINIMAIL_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


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
    assert "       2        2  Inbox" in out and "≠" not in out
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
