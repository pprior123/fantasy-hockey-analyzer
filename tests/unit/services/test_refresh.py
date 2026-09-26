"""The refresh service: TTL with an injected clock, forced refresh, failures (SPEC §2)."""

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from fha.services.refresh import (
    CACHE,
    DEFAULT_TTL_SECONDS,
    Cached,
    RefreshError,
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


@pytest.mark.parametrize("failing", ["get", "all"])
async def test_a_backend_failure_is_not_mistaken_for_no_cache(
    snap: LeagueSnapshot, failing: str
) -> None:
    class Down(InMemoryRepository):
        async def get(self, collection: str, doc_id: str) -> Any:
            if failing == "get":
                raise RepositoryError("Firestore HTTP 503")
            return await super().get(collection, doc_id)

        async def all(self, collection: str) -> dict[str, Any]:
            raise RepositoryError("Firestore HTTP 503")

    repo = Down()
    await save_cached(repo, Cached(snap, T0))
    source = FakeYahooSource(snap)
    with pytest.raises(RepositoryError, match="503"):
        await service(source, repo, FakeClock()).current()
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


class CountingReads(InMemoryRepository):
    def __init__(self) -> None:
        super().__init__()
        self.gets = 0
        self.alls = 0

    async def get(self, collection: str, doc_id: str) -> Any:
        self.gets += 1
        return await super().get(collection, doc_id)

    async def all(self, collection: str) -> dict[str, Any]:
        self.alls += 1
        return await super().all(collection)


async def test_a_warm_process_reads_only_the_meta_document(snap: LeagueSnapshot) -> None:
    repo, clock = CountingReads(), FakeClock()
    refresh = service(FakeYahooSource(snap), repo, clock)
    await refresh.current()  # fetched and remembered
    repo.gets = repo.alls = 0
    for _ in range(3):
        assert await refresh.current() == Cached(snap, T0)
    assert (repo.gets, repo.alls) == (3, 0)


async def test_a_cold_process_loads_the_cache_once_then_remembers_it(snap: LeagueSnapshot) -> None:
    repo, clock = CountingReads(), FakeClock(T0 + 10)
    await save_cached(repo, Cached(snap, T0))
    refresh = service(FakeYahooSource(snap), repo, clock)
    assert await refresh.current() == Cached(snap, T0)
    assert await refresh.current() == Cached(snap, T0)
    assert repo.alls == 1


async def test_another_instances_refresh_is_picked_up(snap: LeagueSnapshot) -> None:
    repo, clock = InMemoryRepository(), FakeClock(T0 + 10)
    await save_cached(repo, Cached(replace(snap, available=()), T0))
    refresh = service(FakeYahooSource(snap), repo, clock)
    await refresh.current()
    await save_cached(repo, Cached(snap, T0 + 5))  # written by another serverless instance
    assert await refresh.current() == Cached(snap, T0 + 5)


class SlowFailingSource(FailingSource):
    async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
        for _ in range(20):
            await asyncio.sleep(0)
        return await super().fetch_snapshot(last_season=last_season)


async def test_waiters_behind_a_failed_refresh_dont_call_yahoo_again(
    snap: LeagueSnapshot,
) -> None:
    repo, clock = InMemoryRepository(), FakeClock(T0 + 9999)
    await save_cached(repo, Cached(snap, T0))
    source = SlowFailingSource()
    refresh = service(source, repo, clock)
    results = await asyncio.gather(*(refresh.current() for _ in range(5)))
    assert source.calls == 1
    assert all(r.snapshot == snap and r.refresh_error for r in results)
    assert results[1].refresh_error is not None
    assert "retrying after 60 s" in results[1].refresh_error


async def test_after_the_backoff_a_refresh_is_tried_again(snap: LeagueSnapshot) -> None:
    repo, clock = InMemoryRepository(), FakeClock(T0 + 9999)
    await save_cached(repo, Cached(snap, T0))
    source = FailingSource()
    refresh = service(source, repo, clock)
    await refresh.current()
    clock.t += 30
    await refresh.current(force=True)  # within the backoff: even a forced refresh waits
    assert source.calls == 1
    clock.t += 31
    await refresh.current()
    assert source.calls == 2


async def test_a_failure_without_a_cache_raises_then_backs_off() -> None:
    source = FailingSource()
    refresh = service(source, InMemoryRepository(), FakeClock())
    with pytest.raises(YahooHTTPError):
        await refresh.current()
    with pytest.raises(RefreshError, match=r"refresh failed: YahooHTTPError: .* \(0 s ago"):
        await refresh.current()
    assert source.calls == 1


async def test_success_clears_the_backoff(snap: LeagueSnapshot) -> None:
    class Flaky(FakeYahooSource):
        fail = True

        async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
            if Flaky.fail:
                Flaky.fail = False
                raise YahooHTTPError(503, "x", None)
            return await super().fetch_snapshot(last_season=last_season)

    repo, clock = InMemoryRepository(), FakeClock(T0 + 9999)
    await save_cached(repo, Cached(snap, T0))
    refresh = service(Flaky(snap), repo, clock)
    assert (await refresh.current()).refresh_error
    clock.t += 61
    got = await refresh.current()
    assert (got.fetched_at, got.refresh_error) == (T0 + 9999 + 61, None)


async def test_a_failed_save_still_serves_what_yahoo_returned(snap: LeagueSnapshot) -> None:
    class ReadOnly(InMemoryRepository):
        async def replace_all(self, collection: str, docs: Any) -> None:
            raise RepositoryError("Firestore HTTP 503")

    got = await service(FakeYahooSource(snap), ReadOnly(), FakeClock()).current()
    assert got.snapshot == snap
    assert got.refresh_error == "not saved: Firestore HTTP 503"


async def test_an_unreadable_cache_is_logged_without_content(
    snap: LeagueSnapshot, caplog: pytest.LogCaptureFixture
) -> None:
    repo = InMemoryRepository()
    await save_cached(repo, Cached(snap, T0))
    await repo.delete(CACHE, "stats-0")
    assert await load_cached(repo) is None
    assert caplog.messages == ["stats cache unreadable (ChunkError); refetching"]


async def test_an_empty_store_has_no_cache() -> None:
    assert await load_cached(InMemoryRepository()) is None


class RefusesSaves(InMemoryRepository):
    async def replace_all(self, collection: str, docs: Any) -> None:
        raise RepositoryError("Firestore HTTP 403 (PERMISSION_DENIED)")


class SlowSource(FakeYahooSource):
    async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
        for _ in range(20):
            await asyncio.sleep(0)
        return await super().fetch_snapshot(last_season=last_season)


@pytest.mark.parametrize("stale_cache", [True, False])
async def test_after_a_failed_save_the_fetched_snapshot_is_served_until_the_ttl(
    snap: LeagueSnapshot, stale_cache: bool
) -> None:
    repo, clock = RefusesSaves(), FakeClock(T0 + 9999)
    if stale_cache:  # an old snapshot in the store, written before saves started failing
        repo.collections = (await _saved(replace(snap, available=()), T0)).collections
    source = SlowSource(snap)
    refresh = service(source, repo, clock)
    together = await asyncio.gather(*(refresh.current() for _ in range(5)))
    later = [await refresh.current() for _ in range(3)]
    assert len(source.calls) == 1
    for got in [*together, *later]:
        assert (got.snapshot, got.fetched_at) == (snap, T0 + 9999)
        assert got.refresh_error == "not saved: Firestore HTTP 403 (PERMISSION_DENIED)"
    clock.t += DEFAULT_TTL_SECONDS  # past the TTL: fetch (and try the save) again
    await refresh.current()
    assert len(source.calls) == 2


async def _saved(snap: LeagueSnapshot, at: float) -> InMemoryRepository:
    repo = InMemoryRepository()
    await save_cached(repo, Cached(snap, at))
    return repo


async def test_concurrent_cold_requests_download_the_cache_once(snap: LeagueSnapshot) -> None:
    repo, clock = CountingReads(), FakeClock(T0 + 10)
    await save_cached(repo, Cached(snap, T0))
    refresh = service(FakeYahooSource(snap), repo, clock)
    results = await asyncio.gather(*(refresh.current() for _ in range(4)))
    assert repo.alls == 1
    assert all(r == Cached(snap, T0) for r in results)


async def test_an_older_stored_cache_never_replaces_a_newer_snapshot_in_memory(
    snap: LeagueSnapshot,
) -> None:
    repo, clock = CountingReads(), FakeClock(T0 + 9999)
    old = replace(snap, available=())
    await save_cached(repo, Cached(old, T0))
    refresh = service(SlowSource(snap), repo, clock)
    # One request refreshes (slowly) while others arrive and read the old meta.
    results = await asyncio.gather(*(refresh.current() for _ in range(3)))
    assert all(r.snapshot == snap for r in results)
    assert (await refresh.current()) == Cached(snap, T0 + 9999)


def test_a_negative_retry_after_is_refused(snap: LeagueSnapshot) -> None:
    with pytest.raises(ValueError, match="retry_after_seconds must be >= 0"):
        RefreshService(
            FakeYahooSource(snap), InMemoryRepository(), FakeClock(), retry_after_seconds=-1
        )


async def test_a_cache_without_a_timestamp_is_logged(
    snap: LeagueSnapshot, caplog: pytest.LogCaptureFixture
) -> None:
    repo = await _saved(snap, T0)
    meta = await repo.get(CACHE, "meta")
    assert meta is not None
    del meta["fetched_at"]
    await repo.put(CACHE, "meta", meta)
    assert await load_cached(repo) is None
    assert caplog.messages == ["stats cache has no usable timestamp; refetching"]


class SlowReads(CountingReads):
    """Reads that yield to the event loop, so concurrent requests really interleave."""

    async def get(self, collection: str, doc_id: str) -> Any:
        await asyncio.sleep(0)
        return await super().get(collection, doc_id)

    async def all(self, collection: str) -> dict[str, Any]:
        for _ in range(5):
            await asyncio.sleep(0)
        return await super().all(collection)


async def test_interleaved_cold_requests_download_the_cache_once(snap: LeagueSnapshot) -> None:
    repo, clock = SlowReads(), FakeClock(T0 + 10)
    await save_cached(repo, Cached(snap, T0))
    refresh = service(FakeYahooSource(snap), repo, clock)
    results = await asyncio.gather(*(refresh.current() for _ in range(4)))
    assert repo.alls == 1
    assert all(r == Cached(snap, T0) for r in results)


class SlowAll(InMemoryRepository):
    """``all()`` reads at once but answers late; saves can be made to fail."""

    refuse = False

    async def replace_all(self, collection: str, docs: Any) -> None:
        if self.refuse:
            raise RepositoryError("Firestore HTTP 503")
        await super().replace_all(collection, docs)

    async def all(self, collection: str) -> dict[str, Any]:
        docs = await super().all(collection)
        for _ in range(60):
            await asyncio.sleep(0)
        return docs


async def test_a_download_racing_a_local_refresh_never_regresses_memory(
    snap: LeagueSnapshot,
) -> None:
    repo, clock = SlowAll(), FakeClock(T0 + 10)
    await save_cached(repo, Cached(replace(snap, available=()), T0))
    refresh = service(SlowSource(snap), repo, clock)
    await refresh.current()  # warm memory with the T0 snapshot
    clock.t = T0 + 9999
    a = asyncio.create_task(refresh.current())  # refreshes slowly
    for _ in range(3):
        await asyncio.sleep(0)
    elsewhere = await _saved(replace(snap, available=snap.available[:1]), T0 + 9000)
    repo.collections = elsewhere.collections  # another instance's save lands
    repo.refuse = True  # and this instance's saves start failing
    b = asyncio.create_task(refresh.current())  # starts the slow download of T0+9000
    first, second = await asyncio.gather(a, b)
    assert first.fetched_at == T0 + 9999
    assert second.fetched_at == T0 + 9999, "served an older snapshot than one it has"
    assert (await refresh.current()).fetched_at == T0 + 9999


async def test_a_store_stamped_in_the_future_doesnt_displace_fresh_memory(
    snap: LeagueSnapshot,
) -> None:
    repo, clock = RefusesSaves(), FakeClock(T0)
    repo.collections = (await _saved(replace(snap, available=()), T0 + 600)).collections
    source = FakeYahooSource(snap)
    refresh = service(source, repo, clock)
    for _ in range(4):
        got = await refresh.current()
        assert (got.snapshot, got.fetched_at) == (snap, T0)
    assert len(source.calls) == 1  # not a Yahoo call per request


async def test_a_save_that_landed_despite_an_error_loses_its_not_saved_note(
    snap: LeagueSnapshot,
) -> None:
    class LandsThenErrs(InMemoryRepository):
        async def replace_all(self, collection: str, docs: Any) -> None:
            await super().replace_all(collection, docs)
            raise RepositoryError("Firestore commit failed: ReadTimeout")

    repo, clock = LandsThenErrs(), FakeClock()
    refresh = service(FakeYahooSource(snap), repo, clock)
    assert (
        await refresh.current()
    ).refresh_error == "not saved: Firestore commit failed: ReadTimeout"
    assert await refresh.current() == Cached(snap, T0)  # the store has it: no note


async def test_a_newer_cache_that_cant_be_decoded_is_downloaded_once(
    snap: LeagueSnapshot,
) -> None:
    repo, clock = CountingReads(), FakeClock(T0 + 10)
    refresh = service(FakeYahooSource(snap), repo, clock)
    await refresh.current()  # memory at T0 + 10
    newer = await _saved(snap, T0 + 20)
    meta = await newer.get(CACHE, "meta")
    assert meta is not None
    await newer.put(CACHE, "meta", {**meta, "format": 999})  # another deploy's format
    repo.collections = newer.collections
    repo.alls = 0
    clock.t = T0 + 30  # the newer fetch is in the past (not skew), and memory is still fresh
    for _ in range(5):
        assert (await refresh.current()).fetched_at == T0 + 10
    assert repo.alls == 1
