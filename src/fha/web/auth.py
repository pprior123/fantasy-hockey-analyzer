"""The single-password login and its signed session cookie (SPEC §7, "App authentication").

- The password is ``APP_PASSWORD``, compared in constant time.
- A session is an itsdangerous-signed, timestamped token in an HttpOnly,
  SameSite=Lax cookie (Secure unless local dev), valid ``session_days``.
- Failed logins are throttled per process: after ``MAX_FAILURES`` failures in
  ``WINDOW_SECONDS``, logins are refused (HTTP 429) until the window passes.
  Serverless instances each keep their own count; with one user and a strong
  password that is enough to stop casual guessing.
"""

from __future__ import annotations

import hmac
from collections import deque
from urllib.parse import urlsplit

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE = "fha_session"
SALT = "fha-session-v1"
MAX_FAILURES = 5
WINDOW_SECONDS = 60.0
DEFAULT_NEXT = "/players"


def password_ok(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


class Sessions:
    def __init__(self, secret: str, max_age_seconds: int) -> None:
        self._serializer = URLSafeTimedSerializer(secret, salt=SALT)
        self.max_age = max_age_seconds

    def issue(self) -> str:
        return self._serializer.dumps({"v": 1})

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            data: object = self._serializer.loads(token, max_age=self.max_age)
        except (SignatureExpired, BadSignature):
            return False
        return data == {"v": 1}


class LoginThrottle:
    """Refuses logins after too many recent failures (times from the injected clock)."""

    def __init__(self, max_failures: int = MAX_FAILURES, window: float = WINDOW_SECONDS) -> None:
        self._failures: deque[float] = deque()
        self._max = max_failures
        self._window = window

    def _prune(self, now: float) -> None:
        while self._failures and now - self._failures[0] >= self._window:
            self._failures.popleft()

    def blocked(self, now: float) -> bool:
        self._prune(now)
        return len(self._failures) >= self._max

    def failed(self, now: float) -> None:
        self._failures.append(now)


def safe_next(raw: str | None) -> str:
    """Where to go after login: a path on this site only (no open redirect)."""
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return DEFAULT_NEXT
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc or any(ord(ch) < 0x20 for ch in raw):
        return DEFAULT_NEXT
    return raw
