import json
from pathlib import Path
from typing import Any

import pytest

from minimail import auth
from minimail.auth import AuthExpired, AuthUnavailable, TokenSource
from minimail.config import Settings


def test_missing_cache_is_auth_expired(settings: Settings) -> None:
    """001-AC10: no cache file → AuthExpired naming `miniteams login`, and no file is created."""
    with pytest.raises(AuthExpired, match="miniteams login"):
        TokenSource(settings).token()
    assert not settings.msal_cache.exists()


class _FakeApp:
    def __init__(
        self, result: dict[str, Any] | None, source: TokenSource, accounts: int, rotate: bool
    ) -> None:
        self.result, self.source, self.accounts, self.rotate = result, source, accounts, rotate

    def get_accounts(self) -> list[dict[str, str]]:
        return [{"username": "user@example.test"}] * self.accounts

    def acquire_token_silent_with_error(
        self, scopes: list[str], account: Any, force_refresh: bool = False
    ) -> dict[str, Any] | None:
        # A real redemption rotates the refresh token, which marks the cache dirty.
        self.source._cache.has_state_changed = self.rotate
        return self.result


def _source(
    settings: Settings, result: dict[str, Any] | None, accounts: int = 1, rotate: bool = False
) -> TokenSource:
    settings.msal_cache.write_text(json.dumps({}))
    source = TokenSource(settings)
    source._app = _FakeApp(result, source, accounts, rotate)
    return source


def test_cache_without_account_is_auth_expired(settings: Settings) -> None:
    """001-AC10: a cache holding no account is not a credential."""
    with pytest.raises(AuthExpired, match="miniteams login"):
        _source(settings, {"access_token": "x"}, accounts=0).token()


def test_unreachable_authority_is_auth_unavailable(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """001-AC10: discovery failing (offline) is retryable, not a crash."""

    def boom(*args: Any, **kwargs: Any) -> None:
        raise ValueError("Unable to get authority configuration")

    monkeypatch.setattr(auth.msal, "PublicClientApplication", boom)
    settings.msal_cache.write_text("{}")
    with pytest.raises(AuthUnavailable):
        TokenSource(settings).token()


@pytest.mark.parametrize("result", [None, {"error": "invalid_grant"}, {"error": "interaction_required"}])
def test_dead_credential_is_auth_expired(settings: Settings, result: dict[str, Any] | None) -> None:
    """001-AC10."""
    with pytest.raises(AuthExpired):
        _source(settings, result).token()


def test_transient_aad_error_is_auth_unavailable(settings: Settings) -> None:
    with pytest.raises(AuthUnavailable):
        _source(settings, {"error": "temporarily_unavailable", "error_description": "busy"}).token()


def test_token_returned(settings: Settings) -> None:
    assert _source(settings, {"access_token": "abc"}).token() == "abc"


def test_rotated_cache_is_written_owner_only_and_reloaded(settings: Settings, tmp_path: Path) -> None:
    source = _source(settings, {"access_token": "abc"}, rotate=True)
    source.token()
    assert settings.msal_cache.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))
    # Another process replaces the file: the next mint must re-read it.
    loaded: list[str] = []
    source._cache.deserialize = loaded.append  # type: ignore[method-assign]
    settings.msal_cache.write_text('{"AccessToken": {}}')
    source.token()
    assert loaded == ['{"AccessToken": {}}']


def test_dead_credential_set_is_stable() -> None:
    assert "invalid_grant" in auth._DEAD_CREDENTIAL
