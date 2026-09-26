"""InMemoryRepository against the shared contract, and the Repository-backed TokenStore."""

import pytest

from fha.sources.yahoo.oauth import Token, TokenStore, YahooAuthError
from fha.storage.memory import InMemoryRepository
from fha.storage.repository import Repository
from fha.storage.tokens import COLLECTION, YAHOO_TOKEN, RepositoryTokenStore
from tests.unit.storage.contract import RepositoryContract


class TestInMemoryRepository(RepositoryContract):
    @pytest.fixture
    def repo(self) -> Repository:
        return InMemoryRepository()


async def test_token_store_round_trips_the_whole_token() -> None:
    repo = InMemoryRepository()
    store: TokenStore = RepositoryTokenStore(repo)
    assert await store.load() is None
    await store.save(Token("access", "refresh", 1_000.5))
    assert await store.load() == Token("access", "refresh", 1_000.5)
    assert await repo.get(COLLECTION, YAHOO_TOKEN) == {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_at": 1_000.5,
    }


async def test_token_store_rejects_a_corrupt_token() -> None:
    repo = InMemoryRepository()
    await repo.put(COLLECTION, YAHOO_TOKEN, {"access_token": "a"})
    with pytest.raises(YahooAuthError, match="missing its access or refresh token"):
        await RepositoryTokenStore(repo).load()
