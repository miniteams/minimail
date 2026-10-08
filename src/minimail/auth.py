"""Silent Graph tokens from the MSAL cache miniteams maintains — minimail never prompts.

The cache holds a FOCI family refresh token for the Teams client. AAD rotates it on every
redemption, and miniteams processes redeem it too, so the file is re-read whenever another process
replaced it, and written back (atomically, owner-only) whenever we rotated it. Same contract as
miniteams' `TokenSource`, minus every interactive path.
"""

import os
import threading
from pathlib import Path
from typing import Any

import msal

from .config import Settings

GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Everything else AAD can answer (temporarily_unavailable, server_error…) heals by waiting; only
# these mean the cached credential itself is gone.
_DEAD_CREDENTIAL = frozenset(
    {"invalid_grant", "interaction_required", "invalid_client", "unauthorized_client"}
)


class AuthExpired(RuntimeError):
    """No usable credential in the cache: the user must sign in again with miniteams."""


class AuthUnavailable(RuntimeError):
    """AAD failed transiently; the credential may still be good, so retry later."""


def _stamp(path: Path) -> tuple[int, int, int] | None:
    # Inode first: writers swap the file in with os.replace, so a rewrite always moves the inode
    # even when it shares our mtime tick.
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


class TokenSource:
    def __init__(self, settings: Settings) -> None:
        self._path = settings.msal_cache
        self._cache = msal.SerializableTokenCache()
        self._stamp: tuple[int, int, int] | None = None
        # Reload → mint → persist must be atomic per process, or a thread could reload the file
        # over a token another thread just rotated in memory, restoring the revoked one.
        self._lock = threading.Lock()
        self._settings = settings
        self._app: msal.PublicClientApplication | None = None

    def _client(self) -> msal.PublicClientApplication:
        # Lazy: construction runs OIDC discovery over the network, which must fail as a retryable
        # AuthUnavailable inside the sync loop, not crash it at start-up.
        if self._app is None:
            try:
                self._app = msal.PublicClientApplication(
                    self._settings.client_id, authority=self._settings.authority, token_cache=self._cache
                )
            except ValueError as exc:
                raise AuthUnavailable(f"authority discovery failed: {str(exc)[:200]}") from exc
        return self._app

    def _reload(self) -> None:
        stamp = _stamp(self._path)
        if stamp is None:
            raise AuthExpired(f"no token cache at {self._path}: sign in with `miniteams login`")
        if stamp != self._stamp:
            self._cache.deserialize(self._path.read_text())
            self._stamp = stamp

    def _persist(self) -> None:
        if not self._cache.has_state_changed:
            return
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            # Security: refresh tokens inside — owner-only from the first byte.
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
                fh.write(self._cache.serialize())
            # Stamp our own file before it goes live: a sibling replacing it right after must not
            # be mistaken for us.
            stamp = _stamp(tmp)
            os.replace(tmp, self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self._cache.has_state_changed = False
        self._stamp = stamp

    def token(self, force_refresh: bool = False, scope: str = GRAPH_SCOPE) -> str:
        with self._lock:
            self._reload()
            app = self._client()
            accounts = app.get_accounts()
            if not accounts:
                raise AuthExpired(f"no signed-in account in {self._path}: sign in with `miniteams login`")
            result: dict[str, Any] | None = app.acquire_token_silent_with_error(
                [scope], account=accounts[0], force_refresh=force_refresh
            )
            self._persist()
        if result and "access_token" in result:
            return str(result["access_token"])
        error = (result or {}).get("error")
        if result is None:
            raise AuthExpired("no refreshable token for this account: sign in again with `miniteams login`")
        if error in _DEAD_CREDENTIAL:
            raise AuthExpired(f"token refresh refused ({error}): sign in again with `miniteams login`")
        raise AuthUnavailable(f"token refresh failed: {error}: {result.get('error_description', '')[:200]}")
