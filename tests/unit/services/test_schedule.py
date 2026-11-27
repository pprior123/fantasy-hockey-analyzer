"""The Eastern-date schedule cache with an injected clock, store and source."""

import asyncio
from datetime import date, datetime
from typing import Any

import pytest

from fha.domain.schedule import Game
from fha.services.schedule import COLLECTION, FAILURES, eastern_date, tonight
from fha.sources.nhl.schedule import ScheduleError
from fha.storage.memory import InMemoryRepository
from fha.storage.repository import RepositoryError
from tests.unit.services.test_refresh import FakeClock

DAY = date(2026, 11, 27)
NOW = datetime.fromisoformat("2026-11-27T15:00:00Z").timestamp()
GAME = Game("TBL", "BOS", datetime.fromisoformat("2026-11-28T00:00:00Z").timestamp(), 2)


class Source:
    def __init__(self, games: tuple[Game, ...] = (GAME,), *, fail: bool = False) -> None:
        self.calls: list[date] = []
        self.games = games
        self.fail = fail

    async def games_on(self, day: date) -> tuple[Game, ...]:
        self.calls.append(day)
        if self.fail:
            raise ScheduleError("test provider failure")
        return self.games


@pytest.mark.parametrize(
    ("before", "after", "first", "second"),
    [
        ("2026-11-27T04:59:00Z", "2026-11-27T05:00:00Z", date(2026, 11, 26), date(2026, 11, 27)),
        ("2026-07-11T03:59:00Z", "2026-07-11T04:00:00Z", date(2026, 7, 10), date(2026, 7, 11)),
        ("2026-11-27T23:59:00Z", "2026-11-28T00:00:00Z", date(2026, 11, 27), date(2026, 11, 27)),
        ("2026-07-11T23:59:00Z", "2026-07-12T00:00:00Z", date(2026, 7, 11), date(2026, 7, 11)),
    ],
    ids=["eastern-standard", "eastern-daylight", "utc-standard", "utc-daylight"],
)
async def test_tonight_changes_at_eastern_midnight(
    before: str,
    after: str,
    first: date,
    second: date,
) -> None:
    repo, source = InMemoryRepository(), Source(())
    clock = FakeClock(datetime.fromisoformat(before).timestamp())
    assert eastern_date(clock) == first
    slate = await tonight(repo, source, clock)
    assert slate.date == first
    clock.t = datetime.fromisoformat(after).timestamp()
    assert eastern_date(clock) == second
    slate = await tonight(repo, source, clock)
    assert slate.date == second
    assert source.calls == ([first] if first == second else [first, second])


async def test_fresh_games_are_stored_and_reused_for_six_hours() -> None:
    repo, clock, source = InMemoryRepository(), FakeClock(NOW), Source()
    first = await tonight(repo, source, clock)
    assert (first.state, first.date, first.age) == ("fresh", DAY, None)
    assert first.games == {"TBL": GAME, "BOS": GAME}
    assert await repo.get(COLLECTION, DAY.isoformat()) == {
        "games": [{"away": "TBL", "home": "BOS", "start_utc": GAME.start_utc, "game_type": 2}],
        "fetched_at": NOW,
    }
    clock.t += 6 * 3600 - 1
    assert await tonight(repo, source, clock) == first
    assert source.calls == [DAY]
    clock.t += 1
    source.games = ()
    off_day = await tonight(repo, source, clock)
    assert (off_day.state, off_day.games) == ("fresh", {})
    assert source.calls == [DAY, DAY]


@pytest.mark.parametrize("cached", [False, True], ids=["unavailable", "stale"])
async def test_failure_backoff_is_shared_by_instances_and_expires(cached: bool) -> None:
    repo, clock = InMemoryRepository(), FakeClock(NOW)
    if cached:
        await tonight(repo, Source(), clock)
        clock.t += 6 * 3600
    failing, other = Source(fail=True), Source(())
    first = await tonight(repo, failing, clock)
    assert first.state == ("stale" if cached else "unavailable")
    assert first.age == (6 * 3600 if cached else None)
    assert first.games == ({"TBL": GAME, "BOS": GAME} if cached else {})
    stored = await repo.get(FAILURES, DAY.isoformat())
    assert stored is not None
    assert stored["failed_at"] == clock.now()
    clock.t += 299
    again = await tonight(repo, other, clock)
    assert again.state == first.state
    assert again.age == (6 * 3600 + 299 if cached else None)
    assert other.calls == []
    clock.t += 1
    assert (await tonight(repo, other, clock)).state == "fresh"
    assert other.calls == [DAY]
    saved = await repo.get(COLLECTION, DAY.isoformat())
    assert saved == {"games": [], "fetched_at": clock.now()}
    assert await repo.get(FAILURES, DAY.isoformat()) == stored


async def test_an_older_cache_record_still_observes_its_failure_backoff() -> None:
    repo, clock, source = InMemoryRepository(), FakeClock(NOW), Source()
    await repo.put(COLLECTION, DAY.isoformat(), {"failed_at": NOW})
    assert (await tonight(repo, source, clock)).state == "unavailable"
    assert source.calls == []
    clock.t += 300
    assert (await tonight(repo, source, clock)).state == "fresh"
    assert source.calls == [DAY]


@pytest.mark.parametrize("warm", [False, True], ids=["missing-slate", "expired-slate"])
async def test_a_late_failure_cannot_overwrite_a_slate_saved_by_another_instance(
    warm: bool,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowFailure(Source):
        async def games_on(self, day: date) -> tuple[Game, ...]:
            self.calls.append(day)
            entered.set()
            await release.wait()
            raise ScheduleError("late provider failure")

    repo_a, repo_b, clock = InMemoryRepository(), InMemoryRepository(), FakeClock(NOW)
    repo_b.collections = repo_a.collections  # separate clients for the same backing store
    if warm:
        await tonight(repo_a, Source((Game("EDM", "VAN", None, 2),)), clock)
        clock.t += 6 * 3600
    pending = asyncio.create_task(tonight(repo_a, SlowFailure(), clock))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        fresh = await tonight(repo_b, Source(), clock)
        assert fresh.state == "fresh"
        saved = await repo_b.get(COLLECTION, DAY.isoformat())
        clock.t += 1
    finally:
        release.set()
        await pending
    assert await repo_a.get(COLLECTION, DAY.isoformat()) == saved
    assert await repo_a.get(FAILURES, DAY.isoformat()) == {"failed_at": clock.now()}
    source = Source(fail=True)
    assert await tonight(repo_a, source, clock) == fresh
    assert source.calls == []


async def test_an_explicit_date_only_uses_that_dates_games() -> None:
    repo, clock = InMemoryRepository(), FakeClock(NOW)
    await tonight(repo, Source(), clock)
    source = Source(fail=True)
    prior = date(2026, 11, 26)
    slate = await tonight(repo, source, clock, night=prior)
    assert (slate.date, slate.state, slate.games) == (prior, "unavailable", {})
    assert source.calls == [prior]


async def test_concurrent_misses_share_one_call() -> None:
    class Slow(Source):
        async def games_on(self, day: date) -> tuple[Game, ...]:
            for _ in range(4):
                await asyncio.sleep(0)
            return await super().games_on(day)

    repo, clock, source = InMemoryRepository(), FakeClock(NOW), Slow()
    slates = await asyncio.gather(*(tonight(repo, source, clock) for _ in range(5)))
    assert source.calls == [DAY]
    assert all(slate.state == "fresh" and slate.games["BOS"] == GAME for slate in slates)


@pytest.mark.parametrize("step", ["read", "write", "write-backoff"])
async def test_storage_failures_leave_the_schedule_unavailable(step: str) -> None:
    class FailingStore(InMemoryRepository):
        async def get(self, collection: str, doc_id: str) -> Any:
            if step == "read":
                raise RepositoryError("test read failure")
            return await super().get(collection, doc_id)

        async def put(self, collection: str, doc_id: str, doc: Any) -> None:
            raise RepositoryError("test write failure")

    source = Source(fail=step == "write-backoff")
    slate = await tonight(FailingStore(), source, FakeClock(NOW))
    assert (slate.state, slate.games, slate.age) == ("unavailable", {}, None)
    assert source.calls == ([] if step == "read" else [DAY])


async def test_service_budget_cancels_a_slow_source() -> None:
    class Slow(Source):
        cancelled = False

        async def games_on(self, day: date) -> tuple[Game, ...]:
            self.calls.append(day)
            try:
                await asyncio.sleep(3.2)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return self.games

    source = Slow()
    repo = InMemoryRepository()
    assert (await tonight(repo, source, FakeClock(NOW))).state == "unavailable"
    assert source.cancelled
    assert source.calls == [DAY]
    assert await repo.get(COLLECTION, DAY.isoformat()) is None
    assert await repo.get(FAILURES, DAY.isoformat()) == {"failed_at": NOW}
