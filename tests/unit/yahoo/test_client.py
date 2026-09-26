"""The Yahoo client: JSON, bounded concurrency, token refresh (SPEC §2, §4)."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from fha.sources.yahoo import oauth
from fha.sources.yahoo.client import (
    API_BASE,
    YahooClient,
    YahooError,
    YahooHTTPError,
    YahooRateLimitError,
    make_http_client,
)
from fha.sources.yahoo.oauth import Credentials, Token, YahooAuthError

CREDS = Credentials("cid", "csecret")
NOW = 1_000_000.0
FRESH = Token("access-1", "refresh-1", NOW + 3600)


class MemoryStore:
    def __init__(self, token: Token | None = FRESH) -> None:
        self.token = token
        self.loads = 0
        self.saved: list[Token] = []

    async def load(self) -> Token | None:
        self.loads += 1
        return self.token

    async def save(self, token: Token) -> None:
        self.saved.append(token)
        self.token = token


Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


class FakeYahoo:
    """Serves the API and the token endpoint; counts requests in flight."""

    def __init__(self, api: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.api = api or (lambda r: httpx.Response(200, json={"fantasy_content": {"ok": 1}}))
        self.requests: list[httpx.Request] = []
        self.refreshes = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.valid = {"access-1"}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == oauth.TOKEN_URL:
            self.refreshes += 1
            access = f"access-{self.refreshes + 1}"
            self.valid = {access}
            return httpx.Response(200, json={"access_token": access, "expires_in": 3600})
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            for _ in range(5):  # let other requests start
                await asyncio.sleep(0)
            token = request.headers["Authorization"].removeprefix("Bearer ")
            if token not in self.valid:
                return httpx.Response(401, json={"error": {"description": "expired"}})
            return self.api(request)
        finally:
            self.in_flight -= 1


def client(
    fake: FakeYahoo, store: MemoryStore | None = None, now: float = NOW, **kw: Any
) -> YahooClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return YahooClient(http, CREDS, store or MemoryStore(), clock=lambda: now, **kw)


async def test_get_sends_bearer_and_json_format_and_returns_fantasy_content() -> None:
    fake = FakeYahoo()
    assert await client(fake).get("league/465.l.8076/settings") == {"ok": 1}
    (request,) = fake.requests
    assert str(request.url) == API_BASE + "league/465.l.8076/settings?format=json"
    assert request.headers["Authorization"] == "Bearer access-1"


async def test_matrix_parameters_are_sent_unescaped() -> None:
    fake = FakeYahoo()
    await client(fake).get("players;player_keys=465.p.1,465.p.2/stats;type=season")
    assert fake.requests[0].url.raw_path == (
        b"/fantasy/v2/players;player_keys=465.p.1,465.p.2/stats;type=season?format=json"
    )


async def test_token_is_loaded_once() -> None:
    store = MemoryStore()
    yahoo = client(FakeYahoo(), store)
    await asyncio.gather(*(yahoo.get(f"x/{i}") for i in range(5)))
    await yahoo.get("y")
    assert store.loads == 1


async def test_no_stored_token_tells_the_owner_to_run_consent() -> None:
    with pytest.raises(YahooAuthError, match=r"scripts/yahoo_auth\.py"):
        await client(FakeYahoo(), MemoryStore(None)).get("x")


async def test_expiring_token_is_refreshed_before_the_request_and_saved() -> None:
    fake = FakeYahoo()
    store = MemoryStore(Token("access-1", "refresh-1", NOW + 30))  # inside the 60 s leeway
    await client(fake, store).get("x")
    assert fake.refreshes == 1
    assert fake.requests[0].headers["Authorization"] == "Bearer access-2"
    assert store.saved == [Token("access-2", "refresh-1", NOW + 3600)]


async def test_401_refreshes_once_and_retries() -> None:
    fake = FakeYahoo()
    fake.valid = {"someone-else"}  # Yahoo revoked access-1 early
    store = MemoryStore()
    assert await client(fake, store).get("x") == {"ok": 1}
    assert fake.refreshes == 1
    assert [r.headers["Authorization"] for r in fake.requests] == [
        "Bearer access-1",
        "Bearer access-2",
    ]
    assert [t.access_token for t in store.saved] == ["access-2"]


async def test_concurrent_401s_share_one_refresh() -> None:
    fake = FakeYahoo()
    fake.valid = set()
    yahoo = client(fake, max_concurrency=8)
    results = await asyncio.gather(*(yahoo.get(f"x/{i}") for i in range(8)))
    assert results == [{"ok": 1}] * 8
    assert fake.refreshes == 1
    assert len(fake.requests) == 16  # each request: one 401, one retry


class RefusingYahoo(FakeYahoo):
    """Every token is rejected, and so is every refresh (e.g. a revoked grant)."""

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == oauth.TOKEN_URL:
            self.refreshes += 1
            return httpx.Response(400, json={"error": "invalid_grant"})
        return await super().__call__(request)


async def test_concurrent_401s_share_one_refused_refresh() -> None:
    fake = RefusingYahoo()
    fake.valid = set()
    yahoo = client(fake, max_concurrency=8)
    results = await asyncio.gather(*(yahoo.get(f"x/{i}") for i in range(8)), return_exceptions=True)
    assert fake.refreshes == 1
    assert all(isinstance(r, YahooAuthError) and "invalid_grant" in str(r) for r in results)


async def test_an_expired_token_is_refused_once_for_every_waiting_request() -> None:
    fake = RefusingYahoo()
    yahoo = client(fake, MemoryStore(Token("access-1", "refresh-1", NOW - 1)), max_concurrency=8)
    results = await asyncio.gather(*(yahoo.get(f"x/{i}") for i in range(8)), return_exceptions=True)
    assert fake.refreshes == 1
    assert all(isinstance(r, YahooAuthError) for r in results)
    assert fake.requests == []


async def test_401_after_a_refresh_is_an_auth_error() -> None:
    def always_401(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    fake = FakeYahoo(always_401)
    with pytest.raises(YahooAuthError, match="freshly refreshed token for x"):
        await client(fake).get("x")
    assert fake.refreshes == 1


async def test_requests_are_concurrent_and_bounded() -> None:
    fake = FakeYahoo()
    yahoo = client(fake, max_concurrency=3)
    await asyncio.gather(*(yahoo.get(f"x/{i}") for i in range(10)))
    assert len(fake.requests) == 10
    assert fake.max_in_flight == 3


def test_concurrency_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        client(FakeYahoo(), max_concurrency=0)


def respond(status: int, **kwargs: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, **kwargs)


async def test_999_is_rate_limiting() -> None:
    with pytest.raises(YahooRateLimitError) as excinfo:
        await client(FakeYahoo(respond(999, text="slow down"))).get("x")
    assert excinfo.value.status == 999


async def test_http_error_carries_yahoo_description() -> None:
    body = {"error": {"lang": "en-US", "description": "Invalid league key"}}
    with pytest.raises(YahooHTTPError, match="HTTP 400 for league/1: Invalid league key") as e:
        await client(FakeYahoo(respond(400, json=body))).get("league/1")
    assert (e.value.status, e.value.path) == (400, "league/1")


@pytest.mark.parametrize("kwargs", [{"text": "<html/>"}, {"json": {"error": "x"}}, {"json": []}])
async def test_http_error_without_a_description(kwargs: dict[str, Any]) -> None:
    with pytest.raises(YahooHTTPError, match=r"^Yahoo returned HTTP 503 for x$"):
        await client(FakeYahoo(respond(503, **kwargs))).get("x")


@pytest.mark.parametrize("kwargs", [{"text": "<xml/>"}, {"json": {"other": 1}}, {"json": [1]}])
async def test_ok_response_without_fantasy_content_is_an_error(kwargs: dict[str, Any]) -> None:
    with pytest.raises(YahooError, match="no fantasy_content"):
        await client(FakeYahoo(respond(200, **kwargs))).get("x")


async def test_production_http_client_has_timeouts_and_no_redirects() -> None:
    async with make_http_client() as http:
        assert http.timeout.read == 10.0
        assert http.timeout.connect == 5.0
        assert not http.follow_redirects
