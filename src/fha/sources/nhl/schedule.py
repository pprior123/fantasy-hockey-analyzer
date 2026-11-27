"""Read a day's games from the NHL's public schedule."""

import asyncio
import logging
import re
from datetime import date, datetime
from typing import Protocol

import httpx

from fha.domain.names import canonical_team
from fha.domain.schedule import Game

SCHEDULE_TIMEOUT = 3.0
_SCHEDULE_URL = "https://api-web.nhle.com/v1/schedule/"
_TEAM_CODE = re.compile(r"[A-Z]{2,3}")
log = logging.getLogger(__name__)


class ScheduleError(Exception):
    """The schedule source could not supply the requested date."""


class ScheduleSource(Protocol):
    async def games_on(self, day: date) -> tuple[Game, ...]: ...


class NhlScheduleSource:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def games_on(self, day: date) -> tuple[Game, ...]:
        try:
            async with asyncio.timeout(SCHEDULE_TIMEOUT):
                response = await self._http.get(
                    _SCHEDULE_URL + day.isoformat(), follow_redirects=False
                )
                response.raise_for_status()
        except (TimeoutError, httpx.TimeoutException) as error:
            raise ScheduleError("NHL schedule request timed out") from error
        except httpx.HTTPError as error:
            raise ScheduleError("NHL schedule request failed") from error
        try:
            payload = response.json()
        except (ValueError, RecursionError) as error:
            raise ScheduleError("NHL schedule response is not valid JSON") from error
        return parse_schedule(payload, day)


def parse_schedule(payload: object, day: date) -> tuple[Game, ...]:
    """Read only the exact date. A missing date differs from an empty slate."""
    week = payload.get("gameWeek") if isinstance(payload, dict) else None
    if not isinstance(week, list):
        raise ScheduleError("NHL schedule response is missing a game week")
    requested = day.isoformat()
    for entry in week:
        if isinstance(entry, dict) and entry.get("date") == requested:
            games = entry.get("games")
            if not isinstance(games, list):
                raise ScheduleError("NHL schedule response is missing the day's games")
            return tuple(game for raw in games if (game := _game(raw)) is not None)
    raise ScheduleError(f"NHL schedule response has no entry for {requested}")


def _game(raw: object) -> Game | None:
    if not isinstance(raw, dict):
        return None
    away = _team(raw.get("awayTeam"))
    home = _team(raw.get("homeTeam"))
    game_type = raw.get("gameType")
    if away is None or home is None or type(game_type) is not int:
        return None
    return Game(away, home, _start_utc(raw.get("startTimeUTC")), game_type)


def _team(team: object) -> str | None:
    code = team.get("abbrev") if isinstance(team, dict) else None
    if not isinstance(code, str):
        return None
    canonical = canonical_team(code)
    if canonical is not None:
        return canonical
    if _TEAM_CODE.fullmatch(code):
        log.warning("Unrecognized NHL schedule team code %s", code)
        return code
    return None


def _start_utc(raw: object) -> float | None:
    if not isinstance(raw, str):
        return None
    try:
        start = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return start.timestamp() if start.tzinfo is not None else None
