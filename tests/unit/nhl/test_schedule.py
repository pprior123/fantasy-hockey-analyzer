"""The public NHL schedule adapter, using a hand-built fixture and mocked HTTP."""

import asyncio
import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from fha.domain.schedule import Game, Mark, mark, tonight_games
from fha.sources.nhl.schedule import NhlScheduleSource, ScheduleError, parse_schedule

DAY = date(2026, 10, 6)
URL = "https://api-web.nhle.com/v1/schedule/2026-10-06"
FIXTURE = Path(__file__).parents[2] / "fixtures" / "nhl" / "schedule.json"
REGULAR_START = datetime(2026, 10, 6, 23, tzinfo=UTC).timestamp()
PRESEASON_START = datetime(2026, 10, 6, 23, 30, tzinfo=UTC).timestamp()
LATE_START = datetime(2026, 10, 7, 2, tzinfo=UTC).timestamp()


def slate(*games: object) -> dict[str, object]:
    return {"gameWeek": [{"date": DAY.isoformat(), "games": list(games)}]}


def test_parse_only_the_requested_nhl_date(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        games = parse_schedule(json.loads(FIXTURE.read_text()), DAY)
    regular = Game("TBL", "BOS", REGULAR_START, 2)
    unfamiliar_opponent = Game("ZZZ", "ANA", LATE_START, 2)
    assert games == (regular, Game("CHI", "DET", PRESEASON_START, 1), unfamiliar_opponent)
    assert "ZZZ" in caplog.text
    assert "X-4" not in caplog.text
    playing = tonight_games(games)
    assert playing == {
        "TBL": regular,
        "BOS": regular,
        "ZZZ": unfamiliar_opponent,
        "ANA": unfamiliar_opponent,
    }
    assert mark("ANA", playing["ANA"]) == Mark("vs", "ZZZ", LATE_START)


def test_an_explicitly_empty_date_is_an_off_day() -> None:
    assert parse_schedule(slate(), DAY) == ()


def test_a_nonempty_slate_with_no_usable_records_is_unavailable() -> None:
    malformed = {
        "awayTeam": {"abbreviation": "BOS"},
        "homeTeam": {"abbreviation": "TBL"},
        "gameType": 2,
    }
    with pytest.raises(ScheduleError, match="no usable games"):
        parse_schedule(slate(malformed, {"homeTeam": {"abbrev": "TOR"}}, None), DAY)


def test_a_valid_slate_with_only_excluded_types_is_usable_but_yields_no_tonight_teams() -> None:
    preseason = {"awayTeam": {"abbrev": "BOS"}, "homeTeam": {"abbrev": "TBL"}, "gameType": 1}
    parsed = parse_schedule(slate({}, preseason), DAY)
    assert parsed == (Game("BOS", "TBL", None, 1),)
    assert tonight_games(parsed) == {}


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"gameWeek": {}},
        {"gameWeek": []},
        {"gameWeek": [5]},
        {"gameWeek": [{"date": "2026-10-05", "games": []}]},
        {"gameWeek": [{"date": DAY.isoformat()}]},
        {"gameWeek": [{"date": DAY.isoformat(), "games": {}}]},
    ],
    ids=[
        "no-object",
        "missing-week",
        "not-a-week",
        "no-days",
        "not-a-day",
        "date-missing",
        "games-missing",
        "games-not-a-list",
    ],
)
def test_missing_or_malformed_schedule_raises_a_schedule_error(payload: object) -> None:
    with pytest.raises(ScheduleError):
        parse_schedule(payload, DAY)


def test_games_with_missing_or_malformed_teams_or_type_are_dropped() -> None:
    good = {"awayTeam": {"abbrev": "SJ"}, "homeTeam": {"abbrev": "LA"}, "gameType": 3}
    games = parse_schedule(
        slate(
            {"homeTeam": {"abbrev": "LA"}, "gameType": 2},
            {**good, "awayTeam": "SJ"},
            {**good, "awayTeam": {}},
            {**good, "homeTeam": {"abbrev": 13}},
            {**good, "homeTeam": {"abbrev": "xq"}},
            {**good, "gameType": True},
            {**good, "gameType": "2"},
            5,
            good,
        ),
        DAY,
    )
    assert games == (Game("SJS", "LAK", None, 3),)


@pytest.mark.parametrize(
    "start",
    ["2026-10-06T19:30:00", "2026-10-06T19:30:00+99:00", 123],
    ids=["naive", "bad-offset", "non-string"],
)
def test_invalid_start_keeps_the_game_without_a_time(start: object) -> None:
    payload = slate(
        {
            "awayTeam": {"abbrev": "MTL"},
            "homeTeam": {"abbrev": "TOR"},
            "gameType": 2,
            "startTimeUTC": start,
        }
    )
    (game,) = parse_schedule(payload, DAY)
    assert game == Game("MTL", "TOR", None, 2)
    assert mark("MTL", game) == Mark("at", "TOR", None)


async def test_http_source_reads_the_day_from_the_fixed_public_endpoint() -> None:
    with respx.mock as router:
        request = router.get(URL).respond(json=json.loads(FIXTURE.read_text()))
        async with httpx.AsyncClient() as http:
            games = await NhlScheduleSource(http).games_on(DAY)
    assert request.call_count == 1
    assert "Authorization" not in request.calls.last.request.headers
    assert games == parse_schedule(json.loads(FIXTURE.read_text()), DAY)


@pytest.mark.parametrize("failure", ["http-error", "timeout", "not-json"])
async def test_http_failures_become_schedule_errors(failure: str) -> None:
    with respx.mock as router:
        route = router.get(URL)
        if failure == "http-error":
            route.respond(500, text="server error")
        elif failure == "timeout":
            route.mock(side_effect=httpx.ReadTimeout("test timeout"))
        else:
            route.respond(200, text="not JSON")
        async with httpx.AsyncClient() as http:
            with pytest.raises(ScheduleError):
                await NhlScheduleSource(http).games_on(DAY)
    assert route.call_count == 1


async def test_an_http_request_cannot_outlive_the_three_second_budget() -> None:
    cancelled = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.sleep(3.2)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return httpx.Response(200, json=slate())

    with respx.mock as router:
        router.get(URL).mock(side_effect=slow)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ScheduleError, match="timed out"):
                await NhlScheduleSource(http).games_on(DAY)
    assert cancelled.is_set()
