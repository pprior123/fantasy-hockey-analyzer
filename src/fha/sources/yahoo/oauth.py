"""Yahoo OAuth 2.0: authorization URL, code exchange, token refresh (SPEC §4).

Secrets never appear in reprs, exception messages or logs: ``Credentials``
and ``Token`` hide them from ``repr``, and errors carry Yahoo's error code,
never the request or response body.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"  # noqa: S105 - a URL
DEFAULT_REDIRECT_URI = "https://localhost:8000"
# Fantasy Sports read access, asked for explicitly. (The 403 seen in September
# 2026 was Yahoo's pending API approval, not the scope: DECISIONS, "Yahoo API
# access: pending approval".)
FANTASY_READ_SCOPE = "fspt-r"


class YahooAuthError(Exception):
    """OAuth failed: no stored token, a rejected grant, or a malformed reply."""


@dataclass(frozen=True)
class Credentials:
    """The Yahoo app's client credentials (env ``YAHOO_CLIENT_ID`` / ``_SECRET``)."""

    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    redirect_uri: str = DEFAULT_REDIRECT_URI

    def __post_init__(self) -> None:
        if not self.client_id or not self.client_secret:
            raise YahooAuthError("Yahoo client ID and secret must both be set")


@dataclass(frozen=True)
class Token:
    """An access token, its expiry (epoch seconds) and the refresh token."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float

    def expired(self, now: float, leeway: float = 60.0) -> bool:
        """True if the access token is past, or within ``leeway`` s of, expiry."""
        return now >= self.expires_at - leeway

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Token:
        if not isinstance(data, dict):
            raise YahooAuthError("stored token is not an object")
        access, refresh, expires = (
            data.get("access_token"),
            data.get("refresh_token"),
            data.get("expires_at"),
        )
        if not isinstance(access, str) or not isinstance(refresh, str) or not refresh:
            raise YahooAuthError("stored token is missing its access or refresh token")
        if isinstance(expires, bool) or not isinstance(expires, int | float):
            raise YahooAuthError("stored token has no numeric expires_at")
        return cls(access, refresh, float(expires))


class TokenStore(Protocol):
    """Where the token lives between requests (Repository in M3; a file in dev)."""

    async def load(self) -> Token | None: ...

    async def save(self, token: Token) -> None: ...


def new_state() -> str:
    """A random ``state`` value to tie the consent redirect to this request."""
    return secrets.token_urlsafe(16)


def authorization_url(creds: Credentials, state: str) -> str:
    """The URL the owner opens to grant the app read access."""
    query = urlencode(
        {
            "client_id": creds.client_id,
            "redirect_uri": creds.redirect_uri,
            "response_type": "code",
            "scope": FANTASY_READ_SCOPE,
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


def code_from_redirect(redirected_url: str, expected_state: str) -> str:
    """Pull the authorization code out of the URL Yahoo redirected to.

    Rejects a redirect carrying an ``error``, a missing code, or a ``state``
    that doesn't match the one sent (a stale or forged redirect).
    """
    query = parse_qs(urlsplit(redirected_url.strip()).query)
    if "error" in query:
        raise YahooAuthError(f"Yahoo refused consent: {query['error'][0]}")
    codes = query.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise YahooAuthError("the pasted URL has no ?code=... parameter")
    if query.get("state", [""])[0] != expected_state:
        raise YahooAuthError("the pasted URL's state doesn't match this consent request")
    return codes[0]


async def exchange_code(
    http: httpx.AsyncClient, creds: Credentials, code: str, now: float
) -> Token:
    """Trade an authorization code for the first token."""
    return await _token_request(
        http, creds, {"grant_type": "authorization_code", "code": code}, now, None
    )


async def refresh(
    http: httpx.AsyncClient, creds: Credentials, refresh_token: str, now: float
) -> Token:
    """Get a new access token. Keeps the old refresh token if Yahoo sends none."""
    return await _token_request(
        http,
        creds,
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
        now,
        refresh_token,
    )


async def _token_request(
    http: httpx.AsyncClient,
    creds: Credentials,
    form: dict[str, str],
    now: float,
    previous_refresh: str | None,
) -> Token:
    response = await http.post(
        TOKEN_URL,
        data=form | {"redirect_uri": creds.redirect_uri},
        auth=(creds.client_id, creds.client_secret),
    )
    body = _json_or_none(response)
    if response.status_code != 200:
        code = body.get("error") if isinstance(body, dict) else None
        detail = code if isinstance(code, str) else f"HTTP {response.status_code}"
        raise YahooAuthError(f"Yahoo token request ({form['grant_type']}) failed: {detail}")
    if not isinstance(body, dict):
        raise YahooAuthError("Yahoo token response is not a JSON object")
    access = body.get("access_token")
    rotated = body.get("refresh_token")
    expires_in = body.get("expires_in")
    if not isinstance(access, str) or not access:
        raise YahooAuthError("Yahoo token response has no access_token")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int | float) or expires_in <= 0:
        raise YahooAuthError("Yahoo token response has no positive expires_in")
    refresh_token = rotated if isinstance(rotated, str) and rotated else previous_refresh
    if refresh_token is None:
        raise YahooAuthError("Yahoo token response has no refresh_token")
    return Token(access, refresh_token, now + float(expires_in))


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None
