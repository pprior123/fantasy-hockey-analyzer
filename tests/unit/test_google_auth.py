"""Service-account access tokens for Google APIs (mocked token endpoint, a throwaway key)."""

import asyncio
import json
import time
from functools import cache
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import jwt

from fha.sources.google_auth import (
    DEFAULT_TOKEN_URI,
    EMULATOR_TOKEN,
    JWT_BEARER,
    GoogleAuthError,
    ServiceAccountTokens,
    emulator_token,
)

SCOPE = "https://www.googleapis.com/auth/datastore"
NOW = float(int(time.time()))  # the signature check compares iat/exp with the real clock


@cache
def keypair() -> tuple[str, bytes]:
    """A throwaway RSA key: (private PEM, public PEM)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, public


def key_json(**overrides: Any) -> str:
    info = {
        "type": "service_account",
        "client_email": "fha@demo-fha.iam.gserviceaccount.com",
        "private_key_id": "kid-1",
        "private_key": keypair()[0],
        "token_uri": "https://oauth2.example.test/token",
        **overrides,
    }
    return json.dumps({k: v for k, v in info.items() if v is not None})


class Google:
    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status = status
        self.body = {"access_token": "ya29.token-1", "expires_in": 3599} if body is None else body
        self.forms: list[dict[str, list[str]]] = []
        self.urls: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        self.forms.append(parse_qs(request.content.decode()))
        await asyncio.sleep(0)
        if isinstance(self.body, str):
            return httpx.Response(self.status, text=self.body)
        return httpx.Response(self.status, json=self.body)


def tokens(google: Google, clock: list[float] | None = None, **key: Any) -> ServiceAccountTokens:
    now = clock if clock is not None else [NOW]
    http = httpx.AsyncClient(transport=httpx.MockTransport(google))
    return ServiceAccountTokens(key_json(**key), [SCOPE], http, clock=lambda: now[0])


async def test_the_assertion_is_a_signed_jwt_for_the_scope_and_token_endpoint() -> None:
    google = Google()
    assert await tokens(google)() == "ya29.token-1"
    [form] = google.forms
    assert form["grant_type"] == [JWT_BEARER]
    claims = jwt.decode(
        form["assertion"][0], certs=keypair()[1], audience="https://oauth2.example.test/token"
    )
    assert claims["iss"] == "fha@demo-fha.iam.gserviceaccount.com"
    assert claims["scope"] == SCOPE
    assert claims["exp"] - claims["iat"] == 3600
    assert google.urls == ["https://oauth2.example.test/token"]


async def test_without_a_token_uri_google_s_endpoint_is_used() -> None:
    google = Google()
    await tokens(google, token_uri=None)()
    assert google.urls == [DEFAULT_TOKEN_URI]


async def test_the_token_is_cached_until_a_minute_before_expiry() -> None:
    google = Google(body={"access_token": "t", "expires_in": 3600})
    clock = [NOW]
    provider = tokens(google, clock)
    await provider()
    clock[0] = NOW + 3600 - 61
    await provider()
    assert len(google.forms) == 1
    clock[0] = NOW + 3600 - 60
    await provider()
    assert len(google.forms) == 2


async def test_concurrent_callers_share_one_token_request() -> None:
    google = Google()
    provider = tokens(google)
    assert await asyncio.gather(*(provider() for _ in range(5))) == ["ya29.token-1"] * 5
    assert len(google.forms) == 1


def test_the_client_email_is_exposed_for_sharing_the_sheet() -> None:
    assert tokens(Google()).client_email == "fha@demo-fha.iam.gserviceaccount.com"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("{not json", "not valid JSON"),
        ("[1]", "not a JSON object"),
        (json.dumps({"private_key": "x"}), "has no client_email"),
        (json.dumps({}), "has no client_email or private_key"),
    ],
)
def test_an_unusable_key_is_refused(raw: str, message: str) -> None:
    with pytest.raises(GoogleAuthError, match=message):
        ServiceAccountTokens(raw, [SCOPE], httpx.AsyncClient())


def test_at_least_one_scope_is_needed() -> None:
    with pytest.raises(GoogleAuthError, match="at least one scope"):
        ServiceAccountTokens(key_json(), [], httpx.AsyncClient())


async def test_a_valid_non_rsa_key_is_refused_not_a_type_error() -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    ec_key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(GoogleAuthError, match="private key can't be used"):
        await tokens(Google(), private_key=ec_key.decode())()


async def test_a_bad_private_key_is_refused_without_echoing_it() -> None:
    with pytest.raises(GoogleAuthError, match="private key can't be used") as caught:
        await tokens(Google(), private_key="-----BEGIN PRIVATE KEY-----\nnot-a-key\n")()
    assert "not-a-key" not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (400, {"error": "invalid_grant", "error_description": "x"}, r"HTTP 400 \(invalid_grant\)$"),
        (400, {"error": "invalid_grant for fha@p.iam"}, r"refused the token request: HTTP 400$"),
        (400, {"error": "Invalid_grant"}, r"refused the token request: HTTP 400$"),
        (400, {"error": "x" * 41}, r"refused the token request: HTTP 400$"),
        (500, "<html>down</html>", "HTTP 500$"),
        (200, {"expires_in": 3600}, "no access_token"),
        (200, {"access_token": "", "expires_in": 3600}, "no access_token"),
        (200, {"access_token": "t", "expires_in": 0}, "no positive expires_in"),
        (200, {"access_token": "t", "expires_in": True}, "no positive expires_in"),
        (200, [1], "no access_token"),
    ],
)
async def test_a_refused_or_malformed_token_response_is_an_error(
    status: int, body: Any, message: str
) -> None:
    with pytest.raises(GoogleAuthError, match=message) as caught:
        await tokens(Google(status, body))()
    assert "BEGIN PRIVATE KEY" not in str(caught.value)
    assert "eyJ" not in str(caught.value)  # no JWT assertion either


async def test_a_missing_expires_in_means_an_hour() -> None:
    google = Google(body={"access_token": "t"})
    clock = [NOW]
    provider = tokens(google, clock)
    await provider()
    clock[0] = NOW + 3600 - 61
    await provider()
    assert len(google.forms) == 1


async def test_a_transport_failure_is_an_auth_error() -> None:
    async def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    http = httpx.AsyncClient(transport=httpx.MockTransport(offline))
    with pytest.raises(GoogleAuthError, match="token request failed: ConnectError"):
        await ServiceAccountTokens(key_json(), [SCOPE], http)()


async def test_the_emulator_token_is_the_documented_fake() -> None:
    assert await emulator_token() == EMULATOR_TOKEN == "owner"
