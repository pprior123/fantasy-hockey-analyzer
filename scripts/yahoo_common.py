"""Shared by the owner-run Yahoo scripts: credentials from env, the dev token file.

The token file is dev-only (SPEC §4: "in dev, a file under the gitignored
``private/`` directory"); production keeps the token in the Repository (M3).
Nothing here prints or logs a secret: a missing variable is named, never its
value.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from fha.sources.yahoo.oauth import DEFAULT_REDIRECT_URI, Credentials, Token, YahooAuthError

REPO = Path(__file__).resolve().parent.parent
TOKEN_PATH = REPO / "private" / "yahoo_token.json"
ENV_HINT = "run with: uv run --env-file .env python -m scripts.<name>"


class SetupError(Exception):
    """Something the owner must fix before the script can run."""


def credentials_from_env(environ: Mapping[str, str]) -> Credentials:
    missing = [n for n in ("YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET") if not environ.get(n)]
    if missing:
        raise SetupError(f"{' and '.join(missing)} not set ({ENV_HINT})")
    return Credentials(
        client_id=environ["YAHOO_CLIENT_ID"],
        client_secret=environ["YAHOO_CLIENT_SECRET"],
        redirect_uri=environ.get("YAHOO_REDIRECT_URI") or DEFAULT_REDIRECT_URI,
    )


class JsonFileTokenStore:
    """The token as JSON in one owner-only file (mode 0600), replaced atomically."""

    def __init__(self, path: Path = TOKEN_PATH) -> None:
        self.path = path

    async def load(self) -> Token | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text())
        except ValueError:  # the message would quote the file: say what, not what's in it
            raise YahooAuthError(f"the stored token {self.path} is not JSON") from None
        return Token.from_dict(data)

    async def save(self, token: Token) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.unlink(missing_ok=True)  # a leftover could be readable by others
        # O_EXCL: a new file, so the 0600 mode applies before any byte is written.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(token.to_dict(), f)
        os.replace(tmp, self.path)
