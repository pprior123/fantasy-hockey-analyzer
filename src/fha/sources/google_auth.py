"""Access tokens for Google APIs (Firestore, Sheets) from a service-account key.

The key's JSON *content* comes from the environment (``FIRESTORE_SERVICE_ACCOUNT_JSON``,
SPEC §8), not a file, because Vercel has no persistent filesystem. The
provider signs a JWT with google-auth's signer and exchanges it at the key's
token endpoint with httpx, so no ``requests`` dependency. google-auth is
imported only when the first token is needed, keeping it off the cold-start
path. Tokens are cached until a minute before they expire, and a refresh is
single-flight. No error message carries the key or a token.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

TokenProvider = Callable[[], Awaitable[str]]  # returns a bearer token

DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
LIFETIME = 3600  # seconds, Google's maximum for a self-signed assertion
LEEWAY = 60  # refresh this long before expiry
EMULATOR_TOKEN = "owner"  # noqa: S105 - the emulators' documented fake credential


class GoogleAuthError(Exception):
    """The key is unusable or Google refused it. Never carries a secret."""


async def emulator_token() -> str:
    """The emulators accept any bearer token; "owner" bypasses security rules."""
    return EMULATOR_TOKEN


class ServiceAccountTokens:
    """A ``TokenProvider`` for one service account and set of scopes."""

    def __init__(
        self,
        key_json: str,
        scopes: Sequence[str],
        http: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        try:
            info: Any = json.loads(key_json)
        except ValueError:
            raise GoogleAuthError("the service-account key is not valid JSON") from None
        if not isinstance(info, dict):
            raise GoogleAuthError("the service-account key is not a JSON object")
        missing = [f for f in ("client_email", "private_key") if not isinstance(info.get(f), str)]
        if missing:
            raise GoogleAuthError(f"the service-account key has no {' or '.join(missing)}")
        if not scopes:
            raise GoogleAuthError("at least one scope is needed")
        self._info = info
        self._scopes = " ".join(scopes)
        self._token_uri = str(info.get("token_uri") or DEFAULT_TOKEN_URI)
        self._http = http
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    @property
    def client_email(self) -> str:
        return str(self._info["client_email"])

    async def __call__(self) -> str:
        async with self._lock:
            if self._token is None or self._clock() >= self._expires_at - LEEWAY:
                self._token, self._expires_at = await self._fetch()
            return self._token

    def _assertion(self, now: int) -> str:
        from google.auth import crypt, jwt  # lazy: off the cold-start path

        payload = {
            "iss": self.client_email,
            "scope": self._scopes,
            "aud": self._token_uri,
            "iat": now,
            "exp": now + LIFETIME,
        }
        try:  # signing too: a valid non-RSA key (e.g. EC) only fails here
            signer = crypt.RSASigner.from_service_account_info(self._info)  # type: ignore[no-untyped-call]
            assertion: bytes = jwt.encode(signer, payload)  # type: ignore[no-untyped-call]
        except (ValueError, TypeError, AttributeError):
            raise GoogleAuthError("the service-account private key can't be used") from None
        return assertion.decode()

    async def _fetch(self) -> tuple[str, float]:
        now = int(self._clock())
        form = {"grant_type": JWT_BEARER, "assertion": self._assertion(now)}
        try:
            response = await self._http.post(self._token_uri, data=form)
        except httpx.HTTPError as e:
            raise GoogleAuthError(f"token request failed: {type(e).__name__}") from None
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        if response.status_code != httpx.codes.OK:
            detail = body.get("error") if isinstance(body, dict) else None
            reason = f" ({detail})" if isinstance(detail, str) else ""
            raise GoogleAuthError(
                f"Google refused the token request: HTTP {response.status_code}{reason}"
            )
        token = body.get("access_token") if isinstance(body, dict) else None
        expires_in = body.get("expires_in", LIFETIME) if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise GoogleAuthError("Google's token response has no access_token")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, int | float)
            or expires_in <= 0
        ):
            raise GoogleAuthError("Google's token response has no positive expires_in")
        return token, now + float(expires_in)
