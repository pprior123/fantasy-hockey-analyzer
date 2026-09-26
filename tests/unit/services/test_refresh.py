"""The refresh service: TTL with an injected clock, forced refresh, failures (SPEC §2)."""

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from fha.services.refresh import (
    CACHE,
    DEFAULT_TTL_SECONDS,
    Cached,
    RefreshService,
    load_cached,
    save_cached,
)
from fha.sources.yahoo.client import YahooHTTPError
from fha.sources.yahoo.fake import FakeYahooSource
from fha.sources.yahoo.models import LeagueSnapshot
from fha.storage.memory import InMemoryRepository
from fha.storage.repository import RepositoryError
from tests.unit.services.snapshots import synthetic_snapshot

T0 = 1_800_000_000.0


class FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.t = now

    def now(self) -> float:
        return self.t


class FailingSource:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
        self.calls += 1
        raise YahooHTTPError(503, "league/x/settings", "Service unavailable")


@pytest.fixture
async def snap() -> LeagueSnapshot:
    return await synthetic_snapshot()


def service(source: Any, repo: InMemoryRepository, clock: FakeClock, **kw: Any) -> RefreshService:
    return RefreshService(source, repo, clock, **kw)


async def test_the_default_ttl_is_thirty_minutes() -> None:
    assert DEFAULT_TTL_SECONDS == 1800


async def test_no_cache_fetches_stores_and_serves(snap: LeagueSnapshot) -> None:
    repo, clock, source = InMemoryRepository(), FakeClock(), FakeYahooSource(snap)
    got = await service(source, repo, clock).current()
    assert got == Cached(snap, T0)
    assert source.calls == [True]
    assert await load_cached(repo) == Cached(snap, T0)


@pytest.mark.parametrize(("age", "fetches"), [(0, 0), (1799.9, 0), (1800, 1), (5000, 1)])
async def test_the_cache_is_served_until_the_ttl(
    snap: LeagueSnapshot, age: float, fetches: int
) -> None:
    repo, clock = InMemoryRepository(), FakeClock(T0 + age)
    old = replace(snap, available=snap.available[:3])
    await save_cached(repo, Cached(old, T0))
    source = FakeYahooSource(snap)
    got = await service(source, repo, clock).current()
    assert len(source.calls) == fetches
    assert got == (Cached(old, T0) if fetches == 0 else Cached(snap, T0 + age))


async def test_force_refreshes_a_fresh_cache(snap: LeagueSnapshot) -> None:
    repo, clock = InMemoryRepository(), FakeClock(T0 + 10)
    await save_cached(repo, Cached(replace(snap, available=()), T0))
    source = FakeYahooSource(snap)
    got = await service(source, repo, clock).current(force=True)
    assert got == Cached(snap, T0 + 10)
    assert len(source.calls) == 1


async def test_a_timestamp_from_the_future_is_stale(snap: LeagueSnapshot) -> None:
    repo, clock = InMemoryRepository(), FakeClock(T0)
    await save_cached(repo, Cached(snap, T0 + 60))
    source = FakeYahooSource(snap)
    assert (await service(source, repo, clock).current()).fetched_at == T0
    assert len(source.calls) == 1


async def test_a_custom_ttl_and_zero_ttl(snap: LeagueSnapshot) -> None:
    repo, clock = InMemoryRepository(), FakeClock(T0 + 61)
    await save_cached(repo, Cached(snap, T0))
    source = FakeYahooSource(snap)
    await service(source, repo, clock, ttl_seconds=120).current()
    assert source.calls == []
    await service(source, repo, clock, ttl_seconds=0).current()
    assert len(source.calls) == 1


def test_a_negative_ttl_is_refused(snap: LeagueSnapshot) -> None:
    with pytest.raises(ValueError, match="ttl_seconds must be >= 0"):
        RefreshService(FakeYahooSource(snap), InMemoryRepository(), FakeClock(), ttl_seconds=-1)


async def test_a_failed_refresh_serves_the_stale_cache_with_the_error(
    snap: LeagueSnapshot,
) -> None:
    repo, clock = InMemoryRepository(), FakeClock(T0 + 9999)
    await save_cached(repo, Cached(snap, T0))
    got = await service(FailingSource(), repo, clock).current()
    assert (got.snapshot, got.fetched_at) == (snap, T0)
    assert got.refresh_error is not None
    assert got.refresh_error == (
        "YahooHTTPError: Yahoo returned HTTP 503 for league/x/settings: Service unavailable"
    )
    assert await load_cached(repo) == Cached(snap, T0)  # unchanged


async def test_a_failed_refresh_without_a_cache_raises() -> None:
    with pytest.raises(YahooHTTPError):
        await service(FailingSource(), InMemoryRepository(), FakeClock()).current()


async def test_a_damaged_cache_is_refetched(snap: LeagueSnapshot) -> None:
    repo, clock = InMemoryRepository(), FakeClock()
    await save_cached(repo, Cached(snap, T0))
    await repo.delete(CACHE, "stats-0")
    assert await load_cached(repo) is None
    source = FakeYahooSource(snap)
    assert (await service(source, repo, clock).current()) == Cached(snap, T0)
    assert len(source.calls) == 1


@pytest.mark.parametrize("fetched_at", [None, "yesterday", True])
async def test_a_cache_without_a_usable_timestamp_is_refetched(
    snap: LeagueSnapshot, fetched_at: Any
) -> None:
    repo = InMemoryRepository()
    await save_cached(repo, Cached(snap, T0))
    meta = await repo.get(CACHE, "meta")
    assert meta is not None
    await repo.put(CACHE, "meta", {**meta, "fetched_at": fetched_at})
    assert await load_cached(repo) is None


async def test_a_backend_failure_is_not_mistaken_for_no_cache(snap: LeagueSnapshot) -> None:
    class Down(InMemoryRepository):
        async def all(self, collection: str) -> dict[str, Any]:
            raise RepositoryError("Firestore HTTP 503")

    source = FakeYahooSource(snap)
    with pytest.raises(RepositoryError, match="503"):
        await service(source, Down(), FakeClock()).current()
    assert source.calls == []


async def test_concurrent_requests_share_one_refresh(snap: LeagueSnapshot) -> None:
    class SlowSource(FakeYahooSource):
        async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
            for _ in range(20):
                await asyncio.sleep(0)
            return await super().fetch_snapshot(last_season=last_season)

    repo, clock, source = InMemoryRepository(), FakeClock(), SlowSource(snap)
    refresh = service(source, repo, clock)
    results = await asyncio.gather(*(refresh.current() for _ in range(5)))
    assert len(source.calls) == 1
    assert all(r == Cached(snap, T0) for r in results)


async def test_a_forced_refresh_waiting_behind_another_uses_its_result(
    snap: LeagueSnapshot,
) -> None:
    repo, clock = InMemoryRepository(), FakeClock(T0 + 10)
    await save_cached(repo, Cached(replace(snap, available=()), T0))

    class SlowSource(FakeYahooSource):
        async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
            for _ in range(20):
                await asyncio.sleep(0)
            return await super().fetch_snapshot(last_season=last_season)

    source = SlowSource(snap)
    refresh = service(source, repo, clock)
    first, second = await asyncio.gather(refresh.current(force=True), refresh.current(force=True))
    assert len(source.calls) == 1
    assert first == second == Cached(snap, T0 + 10)
