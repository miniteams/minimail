import httpx
import pytest

from minimail.graph import retry_delay

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
