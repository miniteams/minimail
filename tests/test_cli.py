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
