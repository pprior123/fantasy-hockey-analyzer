"""OAuth: consent URL, redirect parsing, code exchange, refresh (SPEC §4)."""

import base64
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from fha.sources.yahoo import oauth
from fha.sources.yahoo.oauth import Credentials, Token, YahooAuthError

CREDS = Credentials("client-id-123", "client-secret-456")
NOW = 1_000_000.0


def transport(status: int, body: Any, seen: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# ---------------------------------------------------------------- models


def test_credentials_and_token_hide_secrets_from_repr() -> None:
    token = Token("access-abc", "refresh-def", NOW)
    text = repr(CREDS) + repr(token)
    for secret in ("client-id-123", "client-secret-456", "access-abc", "refresh-def"):
        assert secret not in text
    assert "https://localhost:8000" in repr(CREDS)


@pytest.mark.parametrize(("cid", "secret"), [("", "s"), ("c", "")])
def test_credentials_need_both_values(cid: str, secret: str) -> None:
    with pytest.raises(YahooAuthError, match="must both be set"):
        Credentials(cid, secret)


def test_token_expires_a_minute_early() -> None:
    token = Token("a", "r", expires_at=NOW)
    assert not token.expired(NOW - 61)
    assert token.expired(NOW - 60)
    assert token.expired(NOW + 1)
    assert not token.expired(NOW - 1, leeway=0)


def test_token_round_trips_through_a_dict() -> None:
    token = Token("a", "r", NOW)
    assert Token.from_dict(token.to_dict()) == token
    assert Token.from_dict({"access_token": "a", "refresh_token": "r", "expires_at": 5}) == Token(
        "a", "r", 5.0
    )


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "not an object"),
        ({"refresh_token": "r", "expires_at": 1}, "missing"),
        ({"access_token": "a", "refresh_token": "", "expires_at": 1}, "missing"),
        ({"access_token": "a", "refresh_token": "r"}, "expires_at"),
        ({"access_token": "a", "refresh_token": "r", "expires_at": True}, "expires_at"),
        ({"access_token": "a", "refresh_token": "r", "expires_at": "1"}, "expires_at"),
    ],
)
def test_malformed_stored_token_is_rejected(data: Any, message: str) -> None:
    with pytest.raises(YahooAuthError, match=message):
        Token.from_dict(data)


# ---------------------------------------------------------------- consent


def test_authorization_url() -> None:
    url = urlsplit(oauth.authorization_url(CREDS, "st4te"))
    assert f"{url.scheme}://{url.netloc}{url.path}" == oauth.AUTH_URL
    assert parse_qs(url.query) == {
        "client_id": ["client-id-123"],
        "redirect_uri": ["https://localhost:8000"],
        "response_type": ["code"],
        "state": ["st4te"],
    }


def test_new_state_is_random() -> None:
    assert len({oauth.new_state() for _ in range(5)}) == 5
    assert len(oauth.new_state()) >= 16


def test_code_from_redirect() -> None:
    url = "  https://localhost:8000/?code=abc123&state=st4te \n"
    assert oauth.code_from_redirect(url, "st4te") == "abc123"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://localhost:8000/?error=access_denied&state=s", "refused consent: access_denied"),
        ("https://localhost:8000/?state=s", "no \\?code"),
        ("https://localhost:8000/?code=&state=s", "no \\?code"),
        ("https://localhost:8000/?code=a&code=b&state=s", "no \\?code"),
        ("https://localhost:8000/?code=abc&state=other", "state doesn't match"),
        ("https://localhost:8000/?code=abc", "state doesn't match"),
    ],
)
def test_bad_redirects_are_rejected(url: str, message: str) -> None:
    with pytest.raises(YahooAuthError, match=message):
        oauth.code_from_redirect(url, "s")


# ---------------------------------------------------------------- token endpoint


async def test_exchange_code_posts_the_grant_with_basic_auth() -> None:
    seen: list[httpx.Request] = []
    body = {"access_token": "acc", "refresh_token": "ref", "expires_in": 3600, "token_type": "b"}
    async with transport(200, body, seen) as http:
        token = await oauth.exchange_code(http, CREDS, "the-code", NOW)
    assert token == Token("acc", "ref", NOW + 3600)
    (request,) = seen
    assert (request.method, str(request.url)) == ("POST", oauth.TOKEN_URL)
    expected = base64.b64encode(b"client-id-123:client-secret-456").decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert form(request) == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": "https://localhost:8000",
    }


async def test_refresh_posts_the_refresh_grant() -> None:
    seen: list[httpx.Request] = []
    body = {"access_token": "acc2", "refresh_token": "ref2", "expires_in": 3600}
    async with transport(200, body, seen) as http:
        token = await oauth.refresh(http, CREDS, "ref1", NOW)
    assert token == Token("acc2", "ref2", NOW + 3600)
    assert form(seen[0]) == {
        "grant_type": "refresh_token",
        "refresh_token": "ref1",
        "redirect_uri": "https://localhost:8000",
    }


async def test_refresh_keeps_the_old_refresh_token_if_none_is_returned() -> None:
    async with transport(200, {"access_token": "acc2", "expires_in": 60}) as http:
        token = await oauth.refresh(http, CREDS, "ref1", NOW)
    assert token == Token("acc2", "ref1", NOW + 60)


async def test_exchange_without_a_refresh_token_is_an_error() -> None:
    async with transport(200, {"access_token": "acc", "expires_in": 60}) as http:
        with pytest.raises(YahooAuthError, match="no refresh_token"):
            await oauth.exchange_code(http, CREDS, "code", NOW)


Call = Callable[[httpx.AsyncClient], Any]


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (400, {"error": "invalid_grant", "error_description": "ref1 bad"}, ": invalid_grant$"),
        (401, "<html>nope</html>", ": HTTP 401$"),
        (500, {"error": {"nested": 1}}, ": HTTP 500$"),
        (200, "not json", "not a JSON object"),
        (200, ["a"], "not a JSON object"),
        (200, {"refresh_token": "r", "expires_in": 60}, "no access_token"),
        (200, {"access_token": "", "expires_in": 60}, "no access_token"),
        (200, {"access_token": "a", "expires_in": 0}, "positive expires_in"),
        (200, {"access_token": "a", "expires_in": "3600"}, "positive expires_in"),
        (200, {"access_token": "a", "expires_in": True}, "positive expires_in"),
    ],
)
async def test_token_endpoint_failures(status: int, body: Any, message: str) -> None:
    async with transport(status, body) as http:
        with pytest.raises(YahooAuthError, match=message) as excinfo:
            await oauth.refresh(http, CREDS, "ref1", NOW)
    # Errors name Yahoo's error code, never the request's or response's secrets.
    assert "ref1" not in str(excinfo.value)
    assert "client-secret" not in str(excinfo.value)


def test_token_file_format_is_plain_json() -> None:
    assert json.loads(json.dumps(Token("a", "r", NOW).to_dict())) == {
        "access_token": "a",
        "refresh_token": "r",
        "expires_at": NOW,
    }
