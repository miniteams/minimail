import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from minimail.graph import Graph, retry_delay

from .conftest import BASE, StaticTokens, make_graph


def test_retry_after_honoured_on_429_then_success() -> None:
    """001-AC11: a 429 waits Retry-After and the call still succeeds."""
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.headers["Authorization"])
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True})

    sleeps: list[float] = []
    assert make_graph(handler, sleeps).get("me") == {"ok": True}
    assert sleeps == [7.0]
    assert len(calls) == 2


@pytest.mark.parametrize("status", [503, 504])
def test_transient_5xx_retried_with_backoff(status: int) -> None:
    """001-AC11: 503/504 without Retry-After back off exponentially."""
    n = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        return httpx.Response(status if n < 3 else 200, json={})

    sleeps: list[float] = []
    make_graph(handler, sleeps).get("me")
    assert sleeps == [1.0, 2.0]


def test_retries_are_capped() -> None:
    sleeps: list[float] = []
    graph = make_graph(lambda req: httpx.Response(429), sleeps)
    with pytest.raises(httpx.HTTPStatusError):
        graph.get("me")
    assert len(sleeps) == 6


def test_401_forces_one_refresh_then_raises() -> None:
    tokens = StaticTokens()
    with pytest.raises(httpx.HTTPStatusError):
        make_graph(lambda req: httpx.Response(401), tokens=tokens).get("me")
    # The cached token was rejected: the retry must bypass MSAL's cache, exactly once.
    assert tokens.forced == [False, True]


def test_force_refresh_only_on_the_call_after_401() -> None:
    statuses = iter([401, 429, 200])
    tokens = StaticTokens()
    make_graph(lambda req: httpx.Response(next(statuses), json={}), tokens=tokens).get("me")
    assert tokens.forced == [False, True, False]


@pytest.mark.parametrize("link", ["https://evil.test/v1.0/me", "https://graph.test.evil.test/x"])
def test_token_never_sent_off_host(link: str) -> None:
    hosts: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hosts.append(req.url.host)
        return httpx.Response(
            200, json={"value": [], **({} if len(hosts) > 1 else {"@odata.nextLink": link})}
        )

    with pytest.raises(ValueError, match="refusing"):
        list(make_graph(handler).items("x"))
    assert hosts == ["graph.test"]


def test_other_errors_not_retried() -> None:
    n = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        return httpx.Response(404)

    with pytest.raises(httpx.HTTPStatusError):
        make_graph(handler).get("me")
    assert n == 1


def test_pages_follow_next_link_and_ask_immutable_ids() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert 'IdType="ImmutableId"' in req.headers["Prefer"]
        if "skip" in str(req.url):
            return httpx.Response(200, json={"value": [2]})
        return httpx.Response(200, json={"value": [1], "@odata.nextLink": f"{BASE}/x?skip=1"})

    assert list(make_graph(handler).items("x")) == [1, 2]


def test_retry_delay_non_numeric_header_falls_back() -> None:
    resp = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert retry_delay(resp, 3) == 8.0


def _paced(
    handler: Callable[[httpx.Request], httpx.Response], sleeps: list[float], clock: Callable[[], float]
) -> Graph:
    return Graph(
        StaticTokens(),
        BASE,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        clock=clock,
        max_rps=8,
    )


def test_every_request_is_paced_including_retries_and_pages() -> None:
    """Requests start 1/max_rps apart: a 429 retry and a nextLink page count like any other call."""
    statuses = iter([429, 200, 200])

    def handler(req: httpx.Request) -> httpx.Response:
        status = next(statuses)
        if status == 429:
            return httpx.Response(429, headers={"Retry-After": "0"})
        more = {} if "skip" in str(req.url) else {"@odata.nextLink": f"{BASE}/x?skip=1"}
        return httpx.Response(200, json={"value": [1], **more})

    sleeps: list[float] = []
    assert list(_paced(handler, sleeps, lambda: 0.0).items("x")) == [1, 1]
    assert sleeps == [0.0, 0.125, 0.25]  # Retry-After 0, then the 2nd and 3rd slots


def test_idle_client_is_not_slowed() -> None:
    now = [0.0]
    sleeps: list[float] = []
    graph = _paced(lambda req: httpx.Response(200, json={}), sleeps, lambda: now[0])
    for _ in range(3):
        graph.get("me")
        now[0] += 1.0
    assert sleeps == []


def test_pacing_is_shared_across_threads() -> None:
    """Concurrent workers each reserve a distinct slot: on a frozen clock, n calls span n-1 intervals."""
    # Switch threads as often as possible so an unlocked read-modify-write would collide.
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(10):
            sleeps: list[float] = []
            graph = _paced(lambda req: httpx.Response(200, json={}), sleeps, lambda: 0.0)
            with ThreadPoolExecutor(8) as pool:
                list(pool.map(Graph._pace, [graph] * 2000))
            assert sorted(sleeps) == [0.125 * k for k in range(1, 2000)]
    finally:
        sys.setswitchinterval(old)


@pytest.mark.parametrize("header", ["3600", "86400"])
def test_retry_after_is_capped(header: str) -> None:
    """A huge Retry-After must not park a worker past a SIGTERM stop timeout."""
    assert retry_delay(httpx.Response(429, headers={"Retry-After": header}), 0) == 60.0


def test_throttle_holds_back_every_thread() -> None:
    """A 429 seen by one worker delays the next request of every other worker, not only its own retry."""
    waits: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/a") and not waits:
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200, json={})

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 1:
            graph.get("b")  # another worker starts a request during the back-off

    graph = Graph(
        StaticTokens(),
        BASE,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
        clock=lambda: 0.0,
        max_rps=8,
    )
    graph.get("a")
    assert waits == [5.0, 5.0, 5.125]  # back-off, the other worker's slot, then the retry's slot


def test_close_wakes_a_worker_in_a_long_back_off() -> None:
    """A SIGTERM stop closes the client: a worker asleep on a 60 s Retry-After returns at once,
    without minting a token for a retry."""
    throttled = threading.Event()

    def handler(req: httpx.Request) -> httpx.Response:
        throttled.set()
        return httpx.Response(429, headers={"Retry-After": "60"})

    tokens = StaticTokens()
    graph = Graph(tokens, BASE, transport=httpx.MockTransport(handler))
    errors = _in_worker(graph, throttled.wait)
    assert errors == ["Graph client closed"]
    assert len(tokens.forced) == 1


def test_close_wakes_a_worker_waiting_for_its_pacing_slot() -> None:
    graph = Graph(
        StaticTokens(),
        BASE,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        max_rps=0.1,
    )
    graph.get("me")  # takes the free slot; the next one is 10 s away
    # Woken in _pace, the request then meets the closed client.
    assert len(_in_worker(graph, lambda: not threading.Event().wait(0.05))) == 1


def _in_worker(graph: Graph, ready: Callable[[], bool]) -> list[str]:
    """Runs graph.get in a daemon thread, closes the client once `ready()`, and returns what the
    worker raised; fails unless the worker returned within a second of the close."""
    errors: list[str] = []

    def call() -> None:
        try:
            graph.get("me")
        except RuntimeError as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=call, daemon=True)  # a hung worker must not hold pytest
    worker.start()
    assert ready()
    started = time.monotonic()
    graph.close()
    worker.join(2)
    assert not worker.is_alive() and time.monotonic() - started < 1.0
    return errors


def test_read_timeout_bounds_a_stuck_read_but_not_a_large_body() -> None:
    """close() cannot interrupt a socket read: the per-read timeout bounds how long a stop waits."""
    timeout = make_graph(lambda r: httpx.Response(200))._client.timeout
    assert (timeout.read, timeout.connect) == (30.0, 10.0)
