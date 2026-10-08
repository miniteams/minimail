import os
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
    db = open_db(env / "data")
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
    db = open_db(env / "data")
    import hashlib
    from compression import zstd

    other = b"Subject: other\r\n\r\ny"
    db.execute(
        "UPDATE messages SET mime=?, mime_sha256=?", (zstd.compress(other), hashlib.sha256(other).hexdigest())
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
    db = open_db(env / "data")
    db.execute("UPDATE messages SET search_text='stale'")
    db.commit()
    assert cli.main(["reindex"]) == 0
    assert "reindexed=1" in capsys.readouterr().out
    assert "déjà" in db.execute("SELECT search_text FROM messages").fetchone()[0]
