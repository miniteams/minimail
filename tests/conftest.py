"""Shared fakes: a static token source and a routed Graph mock."""

import os
import resource
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import httpx
import pytest
import structlog

from minimail.config import Settings
from minimail.graph import Graph
from minimail.store import open_db

# A runaway test (or mutant) must die with MemoryError, not take the host down; 1 GiB ≈ 10× the suite's peak.
_AS_CAP = 1 << 30
_soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
resource.setrlimit(
    resource.RLIMIT_AS, (_AS_CAP if _hard == resource.RLIM_INFINITY else min(_AS_CAP, _hard), _hard)
)

BASE = "https://graph.test/v1.0"

Handler = Callable[[httpx.Request], httpx.Response]


class StaticTokens:
    def __init__(self) -> None:
        self.forced: list[bool] = []

    def token(self, force_refresh: bool = False) -> str:
        self.forced.append(force_refresh)
        return "tok"


def make_graph(
    handler: Handler, sleeps: list[float] | None = None, tokens: StaticTokens | None = None
) -> Graph:
    record = sleeps if sleeps is not None else []
    source = tokens or StaticTokens()
    return Graph(source, BASE, transport=httpx.MockTransport(handler), sleep=record.append)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(tenant_id="tenant-test", msal_cache=tmp_path / "msal.json", data_dir=tmp_path / "data")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Environment for `cli.main`: archive under tmp_path/data, no token."""
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("MINIMAIL_TENANT_ID", "tenant-test")
    monkeypatch.setenv("MINIMAIL_MSAL_CACHE", str(tmp_path / "absent.json"))
    monkeypatch.setenv("MINIMAIL_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(open_db(tmp_path)) as conn:
        yield conn


@pytest.fixture(autouse=True)
def _restore_umask() -> object:
    # The CLI sets a process-wide umask; a test running it must not leak it to the next.
    old = os.umask(0o022)
    os.umask(old)
    yield
    os.umask(old)


@pytest.fixture(autouse=True)
def _reset_structlog() -> object:
    # The CLI binds structlog to the captured stderr of the test that ran it; later tests would
    # write to that closed stream.
    yield
    structlog.reset_defaults()
