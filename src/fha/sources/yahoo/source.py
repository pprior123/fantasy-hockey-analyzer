"""``YahooSource``: one full read of the league from Yahoo (SPEC §3, §4).

The pool is every rostered player plus every available player Yahoo lists
(owner's decision, 2026-09-26: percentiles are over everyone who played, as in
the workbook; players with GP 0 don't count, so paging past them is harmless).

A refresh has four dependent stages; each stage's requests go out together,
and the client caps how many are in flight (SPEC §2):

1. the current NHL game (``game_key`` changes every season);
2. league settings + the game's stat categories;
3. the rosters (one call), the available players (pages of 25, Yahoo's
   maximum, in waves of ``PAGE_WAVE`` until a short page), and the
   scoreboards for the current and next week;
4. season stats, 25 players a call. Each page's, and the rostered players',
   are requested as soon as their players are known, so paging and stats
   share the request slots instead of taking turns.

The first failure cancels every request still running (task groups, not
``gather``), so nothing outlives a failed refresh.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import replace
from typing import Any, Protocol

from fha.sources.yahoo import parse
from fha.sources.yahoo.models import LeagueSnapshot, Player, Scoreboard, StatLine, Team
from fha.sources.yahoo.parse import YahooParseError

DEFAULT_LEAGUE_ID = 8076
PAGE_SIZE = 25  # Yahoo returns at most 25 players per request
PAGE_WAVE = 8  # pages requested together when paging to the end
MAX_AVAILABLE = 5000  # stop paging here: Yahoo lists ~1,500-2,000 NHL players
DEFAULT_AVAILABLE: int | None = None  # None: every available player
DEFAULT_AVAILABLE_SORT = "AR"  # Yahoo's actual (current) rank


class YahooSource(Protocol):
    async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
        """Read everything a refresh needs; last season's stats only if asked."""
        ...


class ApiClient(Protocol):
    async def get(self, path: str) -> dict[str, Any]: ...


PageFetcher = Callable[[int, int], Coroutine[Any, Any, tuple[Player, ...]]]  # (start, count)


def _chunks(keys: Sequence[str], size: int) -> list[list[str]]:
    return [list(keys[i : i + size]) for i in range(0, len(keys), size)]


def _first_error(group: BaseExceptionGroup[Exception]) -> Exception:
    first = group.exceptions[0]
    return _first_error(first) if isinstance(first, BaseExceptionGroup) else first


async def _together[T](*coros: Coroutine[Any, Any, T]) -> list[T]:
    """Run ``coros`` at once; the first failure cancels the rest and is raised."""
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(c) for c in coros]
    except ExceptionGroup as errors:
        raise _first_error(errors) from None
    return [t.result() for t in tasks]


class HttpYahooSource:
    def __init__(
        self,
        client: ApiClient,
        *,
        league_id: int = DEFAULT_LEAGUE_ID,
        available: int | None = DEFAULT_AVAILABLE,
        available_sort: str = DEFAULT_AVAILABLE_SORT,
    ) -> None:
        """``available``: how many available players to read; None for all of them."""
        if available is not None and available < 0:
            raise ValueError(f"available must be >= 0 or None, got {available}")
        self._client = client
        self._league_id = league_id
        self._available = available
        self._sort = available_sort

    async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
        get = self._client.get
        game = parse.parse_games(await get("games;game_codes=nhl"))
        league_key = f"{game.game_key}.l.{self._league_id}"

        settings_json, categories_json = await _together(
            get(f"league/{league_key}/settings"),
            get(f"game/{game.game_key}/stat_categories"),
        )
        settings = parse.parse_league_settings(settings_json)
        if settings.league_key != league_key or settings.season != game.season:
            raise YahooParseError(
                f"asked for league {league_key} ({game.season}), got "
                f"{settings.league_key} ({settings.season})"
            )
        weeks = [settings.current_week]
        if settings.current_week < settings.end_week:
            weeks.append(settings.current_week + 1)
        seasons = [game.season, game.season - 1] if last_season else [game.season]
        lines: dict[int, dict[str, StatLine]] = {s: {} for s in seasons}

        try:
            async with asyncio.TaskGroup() as tasks:

                def fetch_stats(players: Iterable[Player]) -> None:
                    keys = [p.player_key for p in players]
                    for batch in _chunks(keys, PAGE_SIZE):
                        for season in seasons:
                            stats = self._season_stats(batch, season, game.season, lines[season])
                            try:
                                tasks.create_task(stats)
                            except RuntimeError:  # the refresh is failing: no new work
                                stats.close()
                                raise

                async def rosters() -> tuple[Team, ...]:
                    teams = parse.parse_teams_rosters(
                        await get(f"league/{league_key}/teams/roster")
                    )
                    fetch_stats(e.player for t in teams for e in t.roster)
                    return teams

                async def page(start: int, count: int) -> tuple[Player, ...]:
                    players = await self._available_page(league_key, start, count)
                    fetch_stats(players)
                    return players

                teams_task = tasks.create_task(rosters())
                available_task = tasks.create_task(self._all_available(page))
                boards_task = tasks.create_task(self._scoreboards(league_key, weeks))
        except ExceptionGroup as group:
            raise _first_error(group) from None

        boards = boards_task.result()
        snapshot = LeagueSnapshot(
            game=game,
            settings=settings,
            game_stat_categories=parse.parse_game_stat_categories(categories_json),
            teams=teams_task.result(),
            available=available_task.result(),
            stats={},
            last_season_stats=None,
            scoreboard=boards[0],
            next_scoreboard=boards[1] if len(boards) > 1 else None,
        )
        keys = [p.player_key for p in snapshot.pool]
        by_season = {}
        for season, found in lines.items():
            missing = [k for k in keys if k not in found]
            if missing:
                raise YahooParseError(
                    f"no {season} stats for {len(missing)} players, e.g. {missing[0]}"
                )
            by_season[season] = {k: found[k] for k in keys}
        return replace(
            snapshot,
            stats=by_season[game.season],
            last_season_stats=by_season[game.season - 1] if last_season else None,
        )

    async def _all_available(self, page: PageFetcher) -> tuple[Player, ...]:
        """The available players: ``self._available`` of them, or every one."""
        if self._available is not None:
            starts = range(0, self._available, PAGE_SIZE)
            pages = await _together(*(page(s, min(PAGE_SIZE, self._available - s)) for s in starts))
            return _dedupe(p for pg in pages for p in pg)[: self._available]
        players: list[Player] = []
        for wave in range(0, MAX_AVAILABLE, PAGE_WAVE * PAGE_SIZE):
            pages = await _together(
                *(page(wave + i * PAGE_SIZE, PAGE_SIZE) for i in range(PAGE_WAVE))
            )
            players.extend(p for pg in pages for p in pg)
            if any(len(pg) < PAGE_SIZE for pg in pages):
                return _dedupe(players)
        raise YahooParseError(f"more than {MAX_AVAILABLE} available players: stopped paging")

    async def _scoreboards(self, league_key: str, weeks: Sequence[int]) -> list[Scoreboard]:
        return await _together(*(self._scoreboard(league_key, w) for w in weeks))

    async def _available_page(self, league_key: str, start: int, count: int) -> tuple[Player, ...]:
        return parse.parse_league_players(
            await self._client.get(
                f"league/{league_key}/players;status=A;sort={self._sort};start={start};count={count}"
            )
        )

    async def _scoreboard(self, league_key: str, week: int) -> Scoreboard:
        board = parse.parse_scoreboard(
            await self._client.get(f"league/{league_key}/scoreboard;week={week}")
        )
        if board.week != week:
            raise YahooParseError(f"asked for the week {week} scoreboard, got week {board.week}")
        return board

    async def _season_stats(
        self, keys: Sequence[str], season: int, current: int, into: dict[str, StatLine]
    ) -> None:
        """One call's worth (<= 25 players) of season totals, added to ``into``."""
        suffix = "" if season == current else f";season={season}"
        content = await self._client.get(
            f"players;player_keys={','.join(keys)}/stats;type=season{suffix}"
        )
        into.update(parse.parse_player_stats(content, season))


def _dedupe(players: Iterable[Player]) -> tuple[Player, ...]:
    seen: dict[str, Player] = {}
    for player in players:
        seen.setdefault(player.player_key, player)
    return tuple(seen.values())
