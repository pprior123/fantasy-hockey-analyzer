"""The NHL slate for an Eastern date, shared through the Repository cache."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

from fha.domain.names import canonical_team
from fha.domain.schedule import Game, Mark, mark, tonight_games
from fha.services.clock import Clock
from fha.sources.nhl.schedule import SCHEDULE_TIMEOUT, ScheduleError, ScheduleSource
from fha.storage.repository import Document, Repository, RepositoryError

COLLECTION = "schedule"
FAILURES = "schedule_failures"
EASTERN = ZoneInfo("America/New_York")
FRESH_SECONDS = 6 * 3600
RETRY_SECONDS = 5 * 60
log = logging.getLogger(__name__)

# Locks are shared by requests using the same store and event loop. A lock is
# discarded when no request holds or waits on it.
_locks: WeakValueDictionary[tuple[asyncio.AbstractEventLoop, int, date], asyncio.Lock] = (
    WeakValueDictionary()
)


def eastern_date(clock: Clock) -> date:
    return datetime.fromtimestamp(clock.now(), EASTERN).date()


def team_code(team: str) -> str:
    return canonical_team(team) or team


@dataclass(frozen=True)
class Tonight:
    date: date
    games: dict[str, Game]
    state: Literal["fresh", "stale", "unavailable"]
    age: float | None = None

    def matchup(self, team: str) -> Mark | None:
        code = team_code(team)
        game = self.games.get(code)
        return mark(code, game) if game is not None else None


async def tonight(
    repo: Repository, source: ScheduleSource | None, clock: Clock, *, night: date | None = None
) -> Tonight:
    """Load the date's slate, or report an unavailable schedule on a storage failure."""
    day = night if night is not None else eastern_date(clock)
    if source is None:
        return Tonight(day, {}, "unavailable")
    key = (asyncio.get_running_loop(), id(repo), day)
    lock = _locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            return await _cached_or_fresh(repo, source, clock, day)
    except RepositoryError as error:
        log.warning("NHL schedule cache unavailable (%s)", type(error).__name__)
        return Tonight(day, {}, "unavailable")


def tomorrow_of(day: date) -> date:
    """The next Eastern calendar date (a day, not 24 h, so DST changes don't matter)."""
    return day + timedelta(days=1)


async def nights(
    repo: Repository, source: ScheduleSource | None, clock: Clock, *, count: int, night: date
) -> tuple[Tonight, ...]:
    """Tonight's slate, and with ``count`` 2 tomorrow's too, loaded concurrently."""
    days = [night] if count < 2 else [night, tomorrow_of(night)]
    return tuple(await asyncio.gather(*(tonight(repo, source, clock, night=day) for day in days)))


async def _cached_or_fresh(
    repo: Repository, source: ScheduleSource, clock: Clock, day: date
) -> Tonight:
    key = day.isoformat()
    cached = await repo.get(COLLECTION, key) or {}
    now = clock.now()
    age = _age(now, cached.get("fetched_at"))
    if age is not None and age < FRESH_SECONDS:
        return Tonight(day, tonight_games(_games(cached)), "fresh")
    failure = await repo.get(FAILURES, key)
    # Older cache documents kept the failure timestamp beside the games.
    failed_ago = _age(now, (cached if failure is None else failure).get("failed_at"))
    if failed_ago is None or failed_ago >= RETRY_SECONDS:
        try:
            async with asyncio.timeout(SCHEDULE_TIMEOUT):
                games = await source.games_on(day)
        except (ScheduleError, TimeoutError):
            now = clock.now()
            await repo.put(FAILURES, key, {"failed_at": now})
            age = _age(now, cached.get("fetched_at"))
        else:
            await repo.put(
                COLLECTION,
                key,
                {
                    "games": [asdict(game) for game in games],
                    "fetched_at": clock.now(),
                },
            )
            return Tonight(day, tonight_games(games), "fresh")
    if age is None:
        return Tonight(day, {}, "unavailable")
    return Tonight(day, tonight_games(_games(cached)), "stale", age)


def _age(now: float, stamp: object) -> float | None:
    if isinstance(stamp, bool) or not isinstance(stamp, int | float):
        return None
    age = now - stamp
    return age if age >= 0 else None


def _games(cached: Document) -> list[Game]:
    raw_games = cached.get("games")
    if not isinstance(raw_games, list):
        raise RepositoryError("NHL schedule cache has no game list")
    games = []
    for raw in raw_games:
        try:
            game = Game(**raw)
        except TypeError:
            raise RepositoryError("NHL schedule cache has a malformed game") from None
        if (
            not isinstance(game.away, str)
            or not isinstance(game.home, str)
            or (game.start_utc is not None and not isinstance(game.start_utc, int | float))
            or type(game.game_type) is not int
        ):
            raise RepositoryError("NHL schedule cache has a malformed game")
        games.append(game)
    return games
