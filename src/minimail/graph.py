"""Microsoft Graph client: bearer from the shared MSAL cache, throttling-aware retries, paging.

Every request asks for immutable ids: a default Graph message id changes when the message moves
folder, which would turn every move into a delete + a new download.
"""

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, Protocol

import httpx
import structlog

log = structlog.get_logger()

# Graph throttles per mailbox (429) and sheds load with 503/504; all carry Retry-After or heal soon.
_RETRYABLE = frozenset({429, 503, 504})
# Left after retries, these are about the session or the whole mailbox, never one folder or item.
SESSION_STATUSES = frozenset({401, 429})
_MAX_RETRIES = 6
_MAX_BACKOFF = 60.0
PAGE_SIZE = 100


class Tokens(Protocol):
    def token(self, force_refresh: bool = False) -> str: ...


def retry_delay(resp: httpx.Response, attempt: int) -> float:
    header = resp.headers.get("Retry-After", "")
    # Capped: a worker asleep on a huge Retry-After would hold a SIGTERM exit past systemd's timeout.
    return min(float(header) if header.isdigit() else 2.0**attempt, _MAX_BACKOFF)


class Graph:
    def __init__(
        self,
        tokens: Tokens,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_rps: float | None = None,
    ) -> None:
        self._tokens = tokens
        self._base = base_url.rstrip("/")
        self._host = httpx.URL(self._base).host
        # Waits end early on close(): a worker asleep in a back-off must not hold a SIGTERM exit.
        self._closed = threading.Event()
        self._sleep = sleep or self._closed.wait
        self._clock = clock
        self._interval = 1.0 / max_rps if max_rps else 0.0
        self._next_slot = 0.0
        self._pace_lock = threading.Lock()
        self._client = httpx.Client(
            # Per socket read, so large $value bodies are unaffected; bounds how long close() waits.
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
            headers={"Prefer": f'IdType="ImmutableId", odata.maxpagesize={PAGE_SIZE}'},
        )

    def close(self) -> None:
        self._closed.set()
        self._client.close()

    def url(self, path: str) -> str:
        # nextLink / deltaLink come back absolute; everything else is a path under the base.
        if not path.startswith("https://"):
            return f"{self._base}/{path.lstrip('/')}"
        # Security: the bearer goes wherever this points — never off the Graph host.
        if httpx.URL(path).host != self._host:
            raise ValueError(f"refusing to send the Graph token to {httpx.URL(path).host}")
        return path

    def _pace(self) -> None:
        """Space request starts `1 / max_rps` apart, across every thread sharing this client."""
        if not self._interval:
            return
        # Reserve a slot under the lock, sleep outside it: waiting threads queue in order.
        with self._pace_lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
        if slot > now:
            self._sleep(slot - now)

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One call with retries on throttling/transient 5xx and a single re-mint on 401.
        Raises httpx.HTTPStatusError for any other error status."""
        url = self.url(path)
        extra = kwargs.pop("headers", {})
        refreshed = force = False
        attempt = 0
        while True:
            if self._closed.is_set():
                # Before minting a token: a refresh could stall the stop on a slow AAD.
                raise RuntimeError("Graph client closed")
            headers = {"Authorization": f"Bearer {self._tokens.token(force_refresh=force)}", **extra}
            force = False
            self._pace()
            resp = self._client.request(method, url, headers=headers, **kwargs)
            if resp.status_code == 401 and not refreshed:
                # MSAL would hand back the same cached token: the retry must bypass it.
                refreshed = force = True
                continue
            if resp.status_code in _RETRYABLE and attempt < _MAX_RETRIES:
                delay = retry_delay(resp, attempt)
                log.warning("graph_retry", status=resp.status_code, attempt=attempt + 1, delay=delay)
                with self._pace_lock:
                    # Graph throttles the mailbox, not the thread: every worker backs off.
                    self._next_slot = max(self._next_slot, self._clock() + delay)
                self._sleep(delay)
                attempt += 1
                continue
            resp.raise_for_status()
            return resp

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        data: dict[str, Any] = self.request("GET", path, params=params or None).json()
        return data

    def pages(self, path: str, **params: Any) -> Iterator[dict[str, Any]]:
        """Each page of a collection, following @odata.nextLink (which already embeds the query)."""
        page = self.get(path, **params)
        yield page
        while next_link := page.get("@odata.nextLink"):
            page = self.get(next_link)
            yield page

    def items(self, path: str, **params: Any) -> Iterator[dict[str, Any]]:
        for page in self.pages(path, **params):
            yield from page.get("value", [])
