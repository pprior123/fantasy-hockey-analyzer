"""The Yahoo token in the Repository: M2's ``TokenStore`` (SPEC §3, §4)."""

from __future__ import annotations

from fha.sources.yahoo.oauth import Token
from fha.storage.repository import Repository

COLLECTION = "secrets"  # server-only: Firestore rules deny every client (SPEC §8)
YAHOO_TOKEN = "yahoo_token"  # noqa: S105 - a document ID, not a secret


class RepositoryTokenStore:
    """Loads and saves the whole token (access token and expiry too), so serverless
    invocations don't refresh on every cold start."""

    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    async def load(self) -> Token | None:
        doc = await self._repo.get(COLLECTION, YAHOO_TOKEN)
        return None if doc is None else Token.from_dict(doc)

    async def save(self, token: Token) -> None:
        await self._repo.put(COLLECTION, YAHOO_TOKEN, token.to_dict())
