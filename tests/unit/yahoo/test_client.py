"""The Yahoo client: JSON, bounded concurrency, token refresh (SPEC §2, §4)."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from fha.sources.yahoo import oauth
from fha.sources.yahoo.client import (
    API_BASE,
    REFUSAL_REPLAY_SECONDS,
    SAVE_RETRY_SECONDS,
    YahooClient,
    YahooError,
    YahooHTTPError,
    YahooRateLimitError,
    make_http_client,
)
from fha.sources.yahoo.oauth import Credentials, Token, YahooAuthError
from fha.storage.repository import RepositoryError

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


async def test_a_refusal_is_replayed_briefly_then_yahoo_and_the_store_are_asked_again() -> None:
    """M4R11A-1: one refused renewal (even a one-off 503) wedged a warm instance: every
    later request replayed it, and a re-run consent was never loaded."""
    fake = RefusingYahoo()
    store = MemoryStore(Token("access-1", "refresh-1", NOW - 1))
    now = [NOW]
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    yahoo = YahooClient(http, CREDS, store, clock=lambda: now[0])
    for _ in range(2):  # the same fetch: the refusal is shared, Yahoo asked once
        with pytest.raises(YahooAuthError, match="invalid_grant"):
            await yahoo.get("x")
    assert fake.refreshes == 1
    now[0] += REFUSAL_REPLAY_SECONDS
    with pytest.raises(YahooAuthError):
        await yahoo.get("x")
    assert fake.refreshes == 2  # a later fetch asks Yahoo again
    store.token = Token("access-9", "refresh-9", now[0] + 3600)  # the owner re-ran consent
    fake.valid = {"access-9"}
    assert await yahoo.get("x") == {"ok": 1}
    assert fake.refreshes == 2


async def test_a_warm_instance_whose_token_is_refused_loads_a_new_consent() -> None:
    """M4R12A-1: the in-memory token expires and its renewal is refused; once the
    window passes, a token the owner stored since is used, not the refused one."""

    class RefusingAfterFirst(FakeYahoo):
        refuse = False

        async def __call__(self, request: httpx.Request) -> httpx.Response:
            if str(request.url) == oauth.TOKEN_URL and self.refuse:
                self.refreshes += 1
                return httpx.Response(400, json={"error": "invalid_grant"})
            return await super().__call__(request)

    fake = RefusingAfterFirst()
    store = MemoryStore()
    now = [NOW]
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    yahoo = YahooClient(http, CREDS, store, clock=lambda: now[0])
    assert await yahoo.get("x") == {"ok": 1}  # warm: the token is held in memory
    fake.refuse = True
    now[0] += 3600
    with pytest.raises(YahooAuthError):
        await yahoo.get("x")
    now[0] += REFUSAL_REPLAY_SECONDS
    store.token = Token("access-9", "refresh-9", now[0] + 3600)
    fake.valid = {"access-9"}
    assert await yahoo.get("x") == {"ok": 1}


async def test_a_clock_that_steps_back_doesnt_extend_a_refusal() -> None:
    """M4R12A-3: a refusal "in the future" (the clock went back) isn't replayed."""
    fake = RefusingYahoo()
    now = [NOW]
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    store = MemoryStore(Token("access-1", "refresh-1", NOW - 3600))
    yahoo = YahooClient(http, CREDS, store, clock=lambda: now[0])
    with pytest.raises(YahooAuthError):
        await yahoo.get("x")
    now[0] -= 1
    with pytest.raises(YahooAuthError):
        await yahoo.get("x")
    assert fake.refreshes == 2


class FlakySave(MemoryStore):
    """A store whose saves fail while ``down``."""

    down = True
    attempts = 0

    async def save(self, token: Token) -> None:
        self.attempts += 1
        if self.down:
            raise OSError("store down")
        await super().save(token)


async def test_a_failed_token_save_doesnt_fail_the_request_and_is_retried(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M4R12A-2, M4R13A-1/A-2: the renewed token is kept and used (Yahoo may retire the
    old one), the request succeeds, and the save is retried until the store has it, so
    a cold instance isn't left with the retired token."""
    fake = FakeYahoo()
    store = FlakySave(Token("access-1", "refresh-1", NOW - 1))
    now = [NOW]
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    yahoo = YahooClient(http, CREDS, store, clock=lambda: now[0])
    assert await yahoo.get("x") == {"ok": 1}
    assert "Yahoo token not saved (OSError)" in caplog.text
    assert "store down" not in caplog.text
    assert await yahoo.get("x") == {"ok": 1}  # within the retry wait: no second save
    assert (fake.refreshes, store.attempts) == (1, 1)
    store.down = False
    now[0] += SAVE_RETRY_SECONDS
    assert await yahoo.get("x") == {"ok": 1}
    assert store.attempts == 2
    assert store.saved == [Token("access-2", "refresh-1", NOW + 3600)]  # the renewed one
    now[0] += SAVE_RETRY_SECONDS
    await yahoo.get("x")
    assert store.attempts == 2  # saved: no more retries
    assert fake.refreshes == 1


class StalledSave(MemoryStore):
    """A store whose saves wait for ``release``, then fail while ``down``."""

    def __init__(self, token: Token | None = FRESH) -> None:
        super().__init__(token)
        self.release = asyncio.Event()
        self.attempts = 0
        self.down = False

    async def save(self, token: Token) -> None:
        self.attempts += 1
        await self.release.wait()
        if self.down:
            raise OSError("store down")
        await super().save(token)


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


RENEWED = Token("access-2", "refresh-1", NOW + 3600)


async def stalled_renewal(down: bool) -> tuple[YahooClient, StalledSave, FakeYahoo, list[float]]:
    """A renewal whose save is still stalled when the caller's timeout fires (FR-2)."""
    fake = FakeYahoo()
    store = StalledSave(Token("access-1", "refresh-1", NOW - 1))
    store.down = down
    now = [NOW]
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    yahoo = YahooClient(http, CREDS, store, clock=lambda: now[0])
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await yahoo.get("x")
    assert (fake.refreshes, store.attempts, store.saved) == (1, 1, [])
    return yahoo, store, fake, now


async def test_a_timeout_during_the_token_save_lets_the_save_finish() -> None:
    yahoo, store, fake, now = await stalled_renewal(down=False)
    store.release.set()
    await settle()
    assert store.saved == [RENEWED]  # the shielded save finished after the timeout
    now[0] += SAVE_RETRY_SECONDS
    assert await yahoo.get("x") == {"ok": 1}
    assert (fake.refreshes, store.attempts) == (1, 1)  # saved: no retry, no new renewal
    assert fake.requests[-1].headers["Authorization"] == "Bearer access-2"


async def test_a_timed_out_save_that_then_fails_is_retried() -> None:
    yahoo, store, fake, now = await stalled_renewal(down=True)
    store.release.set()
    await settle()
    assert store.saved == []
    store.down = False
    assert await yahoo.get("x") == {"ok": 1}  # within the retry wait: no second save
    assert store.attempts == 1
    now[0] += SAVE_RETRY_SECONDS
    assert await yahoo.get("x") == {"ok": 1}
    assert store.attempts == 2
    assert store.saved == [RENEWED]
    assert fake.refreshes == 1


async def test_a_retry_waits_for_a_stalled_save_then_saves_again() -> None:
    yahoo, store, fake, now = await stalled_renewal(down=False)
    now[0] += SAVE_RETRY_SECONDS
    retry = asyncio.ensure_future(yahoo.get("x"))  # waits behind the stalled save
    await settle()
    assert not retry.done()
    assert store.attempts == 1  # no second save while the first may still land
    store.release.set()
    assert await retry == {"ok": 1}
    assert store.attempts == 2
    assert store.saved == [RENEWED, RENEWED]
    assert fake.refreshes == 1


async def test_a_later_renewal_is_saved_after_an_earlier_stalled_save() -> None:
    yahoo, store, fake, now = await stalled_renewal(down=False)
    now[0] += 7200  # the renewed token has expired too
    later = asyncio.ensure_future(yahoo.get("x"))
    await settle()
    assert fake.refreshes == 2
    assert store.attempts == 1  # the second save waits for the first
    store.release.set()
    assert await later == {"ok": 1}
    assert [t.access_token for t in store.saved] == ["access-2", "access-3"]  # in order
    assert store.token == store.saved[-1]  # the store ends with the newest token


async def test_a_newer_token_cancelled_behind_an_earlier_save_is_saved_later() -> None:
    yahoo, store, fake, now = await stalled_renewal(down=False)
    now[0] += 7200  # the renewed token has expired too
    with pytest.raises(TimeoutError):  # cancelled while waiting for the first save
        async with asyncio.timeout(0.05):
            await yahoo.get("x")
    assert fake.refreshes == 2
    store.release.set()
    await settle()
    assert [t.access_token for t in store.saved] == ["access-2"]  # the older one landed
    now[0] += SAVE_RETRY_SECONDS
    assert await yahoo.get("x") == {"ok": 1}
    assert [t.access_token for t in store.saved] == ["access-2", "access-3"]  # then the newer
    assert fake.refreshes == 2


async def test_settle_with_no_save_pending_or_one_finished_is_immediate() -> None:
    yahoo = client(FakeYahoo())
    assert await yahoo.settle(0) is True  # never saved
    store = MemoryStore(Token("access-1", "refresh-1", NOW - 1))
    yahoo = client(FakeYahoo(), store)
    await yahoo.get("x")
    assert await yahoo.settle(0) is True  # saved already
    assert store.saved == [RENEWED]


async def test_settle_waits_for_a_stalled_save_within_its_bound(
    caplog: pytest.LogCaptureFixture,
) -> None:
    yahoo, store, _, _ = await stalled_renewal(down=False)
    assert await yahoo.settle(0.05) is False
    assert "Yahoo token save still pending after 0 s" in caplog.text
    assert store.saved == []
    asyncio.get_running_loop().call_later(0.05, store.release.set)
    assert await yahoo.settle(5) is True
    assert store.saved == [RENEWED]


async def test_settle_does_not_raise_when_the_pending_save_fails() -> None:
    yahoo, store, _, _ = await stalled_renewal(down=True)
    store.release.set()
    assert await yahoo.settle(5) is True  # finished: its retry mark stays for later
    assert store.saved == []


async def test_a_failed_token_save_logs_the_store_error_s_safe_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PR #17 review: a Firestore 403 here was logged as a bare RepositoryError."""

    class Denied(MemoryStore):
        async def save(self, token: Token) -> None:
            raise RepositoryError(
                "Firestore put secrets/yahoo_token: HTTP 403 (on projects/secret-project)",
                detail="HTTP 403 PERMISSION_DENIED",
            )

    store = Denied(Token("access-1", "refresh-1", NOW - 1))
    http = httpx.AsyncClient(transport=httpx.MockTransport(FakeYahoo()))
    assert await YahooClient(http, CREDS, store, clock=lambda: NOW).get("x") == {"ok": 1}
    assert "Yahoo token not saved (RepositoryError: HTTP 403 PERMISSION_DENIED)" in caplog.text
    assert "secret-project" not in caplog.text


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
