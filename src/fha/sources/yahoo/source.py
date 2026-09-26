"""``YahooSource``: one full read of the league from Yahoo (SPEC §3, §4).

A refresh runs in four dependent stages. Each stage issues its requests
together, and the client caps how many are in flight (SPEC §2):

1. the current NHL game (``game_key`` changes every season);
2. league settings + the game's stat categories;
3. rosters (all teams, one call), the top available players (pages of 25,
   Yahoo's maximum), and the scoreboards for the current and next week;
4. season stats for the whole pool, 25 players per call, for the current
   season and (optionally) last season.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any, Protocol

from fha.sources.yahoo import parse
from fha.sources.yahoo.models import LeagueSnapshot, Player, Scoreboard, StatLine
from fha.sources.yahoo.parse import YahooParseError

DEFAULT_LEAGUE_ID = 8076
PAGE_SIZE = 25  # Yahoo returns at most 25 players per request
DEFAULT_AVAILABLE = 300  # SPEC §4: rostered players plus the top ~300 available
DEFAULT_AVAILABLE_SORT = "AR"  # Yahoo's actual (current) rank


class YahooSource(Protocol):
    async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
        """Read everything a refresh needs; last season's stats only if asked."""
        ...


class ApiClient(Protocol):
    async def get(self, path: str) -> dict[str, Any]: ...


def _chunks(keys: Sequence[str], size: int) -> list[list[str]]:
    return [list(keys[i : i + size]) for i in range(0, len(keys), size)]


class HttpYahooSource:
    def __init__(
        self,
        client: ApiClient,
        *,
        league_id: int = DEFAULT_LEAGUE_ID,
        available: int = DEFAULT_AVAILABLE,
        available_sort: str = DEFAULT_AVAILABLE_SORT,
    ) -> None:
        if available < 0:
            raise ValueError(f"available must be >= 0, got {available}")
        self._client = client
        self._league_id = league_id
        self._available = available
        self._sort = available_sort

    async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
        get = self._client.get
        game = parse.parse_games(await get("games;game_codes=nhl"))
        league_key = f"{game.game_key}.l.{self._league_id}"

        settings_json, categories_json = await asyncio.gather(
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

        starts = range(0, self._available, PAGE_SIZE)
        rosters_json, pages, boards = await asyncio.gather(
            get(f"league/{league_key}/teams/roster"),
            asyncio.gather(*(self._available_page(league_key, s) for s in starts)),
            asyncio.gather(*(self._scoreboard(league_key, w) for w in weeks)),
        )
        teams = parse.parse_teams_rosters(rosters_json)
        available = _dedupe(p for page in pages for p in page)[: self._available]

        snapshot = LeagueSnapshot(
            game=game,
            settings=settings,
            game_stat_categories=parse.parse_game_stat_categories(categories_json),
            teams=teams,
            available=available,
            stats={},
            last_season_stats=None,
            scoreboard=boards[0],
            next_scoreboard=boards[1] if len(boards) > 1 else None,
        )
        keys = [p.player_key for p in snapshot.pool]
        seasons = [game.season, game.season - 1] if last_season else [game.season]
        stats = await asyncio.gather(*(self._season_stats(keys, s, game.season) for s in seasons))
        return replace(
            snapshot, stats=stats[0], last_season_stats=stats[1] if last_season else None
        )

    async def _available_page(self, league_key: str, start: int) -> tuple[Player, ...]:
        count = min(PAGE_SIZE, self._available - start)
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
        self, keys: Sequence[str], season: int, current: int
    ) -> dict[str, StatLine]:
        suffix = "" if season == current else f";season={season}"
        batches = await asyncio.gather(
            *(
                self._client.get(f"players;player_keys={','.join(batch)}/stats;type=season{suffix}")
                for batch in _chunks(keys, PAGE_SIZE)
            )
        )
        lines: dict[str, StatLine] = {}
        for batch in batches:
            lines.update(parse.parse_player_stats(batch, season))
        missing = [k for k in keys if k not in lines]
        if missing:
            raise YahooParseError(
                f"no {season} stats for {len(missing)} players, e.g. {missing[0]}"
            )
        return lines


def _dedupe(players: Iterable[Player]) -> tuple[Player, ...]:
    seen: dict[str, Player] = {}
    for player in players:
        seen.setdefault(player.player_key, player)
    return tuple(seen.values())
