"""A thin async client for the Yahoo Fantasy Sports API (SPEC §2, §4).

- JSON via ``?format=json``; returns the ``fantasy_content`` object.
- At most ``max_concurrency`` requests in flight (SPEC §2: bounded, e.g. 8).
- The token comes from a ``TokenStore``. It is refreshed shortly before
  expiry, or on a 401, then saved back (Yahoo may rotate the refresh token).
  Concurrent 401s share one refresh; each request then retries once.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx

from fha.sources.yahoo import oauth
from fha.sources.yahoo.oauth import Credentials, Token, TokenStore, YahooAuthError

API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2/"
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
RATE_LIMITED = 999  # Yahoo's status code for "too many requests"

JsonObject = dict[str, Any]


class YahooError(Exception):
    """A Yahoo API request failed."""


class YahooHTTPError(YahooError):
    def __init__(self, status: int, path: str, description: str | None) -> None:
        self.status = status
        self.path = path
        detail = f": {description}" if description else ""
        super().__init__(f"Yahoo returned HTTP {status} for {path}{detail}")


class YahooRateLimitError(YahooHTTPError):
    """Yahoo's 999: too many requests. Retry later, not now."""


def make_http_client() -> httpx.AsyncClient:
    """The production httpx client (explicit timeouts, no redirects)."""
    return httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=False)


class YahooClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        creds: Credentials,
        store: TokenStore,
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {max_concurrency}")
        self._http = http
        self._creds = creds
        self._store = store
        self._clock = clock
        self._slots = asyncio.Semaphore(max_concurrency)
        self._token_lock = asyncio.Lock()
        self._token: Token | None = None
        self._refused: tuple[Token, YahooAuthError] | None = None  # a refresh Yahoo refused

    async def get(self, path: str) -> JsonObject:
        """GET ``API_BASE + path`` and return its ``fantasy_content``.

        ``path`` is Yahoo's resource path, e.g. ``league/465.l.8076/settings``;
        its ``;key=value`` matrix parameters are sent as written.
        """
        async with self._slots:
            token = await self._current_token()
            response = await self._send(path, token)
            if response.status_code == httpx.codes.UNAUTHORIZED:
                token = await self._refresh(stale=token)
                response = await self._send(path, token)
                if response.status_code == httpx.codes.UNAUTHORIZED:
                    raise YahooAuthError(f"Yahoo rejected a freshly refreshed token for {path}")
        return _content(response, path)

    async def _send(self, path: str, token: Token) -> httpx.Response:
        return await self._http.get(
            API_BASE + path,
            params={"format": "json"},
            headers={"Authorization": f"Bearer {token.access_token}"},
        )

    async def _current_token(self) -> Token:
        async with self._token_lock:
            token = self._token or await self._store.load()
            if token is None:
                raise YahooAuthError(
                    "no Yahoo token stored: the owner must run scripts/yahoo_auth.py"
                )
            if token.expired(self._clock()):
                token = await self._renew(token)
            self._token = token
            return token

    async def _refresh(self, stale: Token) -> Token:
        """Refresh after a 401, unless another request already replaced ``stale``."""
        async with self._token_lock:
            token = self._token
            if token is None or token is stale:
                token = await self._renew(stale)
                self._token = token
            return token

    async def _renew(self, token: Token) -> Token:
        """Refresh ``token`` (under the lock). If Yahoo refuses, requests waiting on
        the same token get that refusal instead of each asking again."""
        if self._refused is not None and self._refused[0] is token:
            raise YahooAuthError(str(self._refused[1])) from self._refused[1]
        try:
            renewed = await oauth.refresh(
                self._http, self._creds, token.refresh_token, self._clock()
            )
        except YahooAuthError as refused:
            self._refused = (token, refused)
            raise
        await self._store.save(renewed)
        return renewed


def _content(response: httpx.Response, path: str) -> JsonObject:
    try:
        body = response.json()
    except ValueError:
        body = None
    status = response.status_code
    if status == RATE_LIMITED:
        raise YahooRateLimitError(status, path, None)
    if not 200 <= status < 300:
        raise YahooHTTPError(status, path, _description(body))
    content = body.get("fantasy_content") if isinstance(body, dict) else None
    if not isinstance(content, dict):
        raise YahooError(f"Yahoo response for {path} has no fantasy_content object")
    return content


def _description(body: Any) -> str | None:
    error = body.get("error") if isinstance(body, dict) else None
    description = error.get("description") if isinstance(error, dict) else None
    return description if isinstance(description, str) else None
