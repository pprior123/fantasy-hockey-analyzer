"""The real Yahoo recording, replayed offline (SPEC §10 M4.5).

``scripts.record_yahoo`` recorded one real refresh into ``tests/fixtures/yahoo/``
(sanitized). These tests serve those responses to the real ``YahooClient`` and
``HttpYahooSource``, by exact request path, so a request the source makes that
the recording doesn't hold fails the test, and so does a recorded one it no
longer makes.
"""

import asyncio
import json
import math
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

import httpx

from fha.domain.engine import rate
from fha.domain.models import Category
from fha.domain.names import normalize_name
from fha.sources.yahoo.client import YahooClient
from fha.sources.yahoo.models import LeagueSnapshot
from fha.sources.yahoo.oauth import Credentials, Token
from fha.sources.yahoo.source import HttpYahooSource
from fha.sources.yahoo.stat_map import build_stat_map, to_player_season
from scripts.record_yahoo import MANIFEST, api_path

FIXTURES = Path(__file__).parents[2] / "fixtures" / "yahoo"
NOW = 2_000_000_000.0  # any time: the token below never expires against it
TOKEN = Token("access", "refresh", NOW + 3600)


class Store:
    async def load(self) -> Token | None:
        return TOKEN

    async def save(self, token: Token) -> None:  # pragma: no cover - no refresh happens
        raise AssertionError("the replay never refreshes the token")


@cache
def manifest() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / MANIFEST).read_text())
    return data


def body(file: str) -> Any:
    return json.loads((FIXTURES / file).read_text())


class Replay:
    """Answers each Fantasy API path with its recorded response."""

    def __init__(self) -> None:
        self.calls = {c["path"]: c for c in manifest()["calls"]}
        self.served: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = api_path(request.url)
        call = self.calls.get(path)
        if call is None:
            raise AssertionError(f"not in the recording: {path}")
        self.served.append(path)
        return httpx.Response(call["status"], json=body(call["file"]))


@cache
def replayed() -> tuple[LeagueSnapshot, tuple[str, ...]]:
    replay = Replay()

    async def fetch() -> LeagueSnapshot:
        async with httpx.AsyncClient(transport=httpx.MockTransport(replay)) as http:
            client = YahooClient(http, Credentials("id", "secret"), Store(), clock=lambda: NOW)
            source = HttpYahooSource(
                client,
                league_id=manifest()["params"]["league_id"],
                available=manifest()["params"]["available"],
                available_sort=manifest()["params"]["available_sort"],
            )
            return await source.fetch_snapshot()

    return asyncio.run(fetch()), tuple(replay.served)


def snapshot() -> LeagueSnapshot:
    return replayed()[0]


# ---------------------------------------------------------------- the replay itself


def test_the_refresh_makes_exactly_the_recorded_requests_once_each() -> None:
    served = replayed()[1]
    assert Counter(served) == Counter(c["path"] for c in manifest()["calls"])


def test_every_recorded_call_answered_200() -> None:
    assert {c["status"] for c in manifest()["calls"]} == {200}


# ---------------------------------------------------------------- what the snapshot holds


def test_the_game_key_resolves_to_this_season_s_nhl_game() -> None:
    snap = snapshot()
    assert (snap.game.game_key, snap.game.season) == ("477", 2026)
    assert snap.settings.league_key == "477.l.8076"


def test_the_stat_map_builds_from_the_real_categories() -> None:
    snap = snapshot()
    stat_map = build_stat_map(snap.settings.stat_categories, snap.game_stat_categories)
    assert stat_map.categories == {
        Category.G: ("1",),
        Category.A: ("2",),
        Category.PPP: ("8",),
        Category.PIM: ("5",),
        Category.HIT: ("31",),
        Category.SOG: ("14",),
        Category.BLK: ("32",),
    }
    assert stat_map.ppp_direct  # Yahoo supplies PPP itself (SPEC §11, question 2)
    assert (stat_map.skater_gp, stat_map.goalie_gp) == ("29", "30")
    assert stat_map.goalie == {"W": "19", "GAA": "23", "SV%": "26"}


def test_skater_gp_agrees_with_yahoo_s_general_games_played() -> None:
    """The map takes 29 ("F/D Games"); 0 ("Games Played") must say the same, or
    the tie-break would be hiding a real difference."""
    snap = snapshot()
    lines = [*snap.stats.values(), *(snap.last_season_stats or {}).values()]
    both = [line for line in lines if "29" in line.values]
    assert both
    assert all(line.values.get("0") == line.values["29"] for line in both)


def test_eight_teams_one_of_them_mine() -> None:
    snap = snapshot()
    assert len(snap.teams) == snap.settings.num_teams == 8
    assert [t.team_key for t in snap.teams if t.is_mine] == ["477.l.8076.t.6"]


def test_rosters_fit_the_league_s_slots() -> None:
    snap = snapshot()
    slots = {s.position: s.count for s in snap.settings.roster_slots}
    for team in snap.teams:
        used = Counter(e.selected_position for e in team.roster)
        assert set(used) <= set(slots), team.team_key
        assert all(used[s] <= slots[s] for s in used), team.team_key
        assert all(e.player.eligible_positions for e in team.roster), team.team_key


def test_the_pool_is_rostered_plus_available_with_no_player_twice() -> None:
    snap = snapshot()
    keys = [p.player_key for p in snap.pool]
    rostered = sum(len(t.roster) for t in snap.teams)
    assert len(keys) == len(set(keys)) == rostered + len(snap.available)
    assert all(snap.owner_of(p.player_key) is None for p in snap.available)


def test_both_seasons_have_stats_for_the_pool() -> None:
    snap = snapshot()
    pool = {p.player_key for p in snap.pool}
    assert snap.last_season_stats is not None
    assert set(snap.stats) == pool  # every pool player, in both seasons (PR #19, Codex)
    assert set(snap.last_season_stats) == pool
    assert {line.season for line in snap.stats.values()} == {2026}
    assert {line.season for line in snap.last_season_stats.values()} == {2025}


def raw_stat_values(season: int) -> dict[str, dict[str, str]]:
    """Every player's stat values for ``season``, straight from the recorded JSON."""
    suffix = ";season=2025" if season == 2025 else ";type=season"
    out: dict[str, dict[str, str]] = {}
    for call in manifest()["calls"]:
        if "/stats;" not in call["path"] or not call["path"].endswith(suffix):
            continue
        players = body(call["file"])["fantasy_content"]["players"]
        for i in range(players["count"]):
            fragments, stats = players[str(i)]["player"]
            key = next(
                f["player_key"] for f in fragments if isinstance(f, dict) and "player_key" in f
            )
            out[key] = {
                str(s["stat"]["stat_id"]): str(s["stat"]["value"])
                for s in stats["player_stats"]["stats"]
            }
    return out


def test_parsed_stat_values_are_the_recorded_ones() -> None:
    """Checked against the raw JSON, independent of the parser: every value of
    every player, in both seasons."""
    snap = snapshot()
    for season, lines in ((2026, snap.stats), (2025, snap.last_season_stats or {})):
        raw = raw_stat_values(season)
        assert set(raw) == set(lines)
        assert all(dict(lines[k].values) == raw[k] for k in raw), season


def test_this_week_and_next_week_pair_every_team_once() -> None:
    snap = snapshot()
    teams = {t.team_key for t in snap.teams}
    assert snap.next_scoreboard is not None
    for board, week in (
        (snap.scoreboard, snap.settings.current_week),
        (snap.next_scoreboard, snap.settings.current_week + 1),
    ):
        assert board.week == week
        paired = [k for m in board.matchups for k in m.team_keys]
        assert sorted(paired) == sorted(teams)
        assert board.opponent("477.l.8076.t.6") in teams - {"477.l.8076.t.6"}


# ---------------------------------------------------------------- through the engine


def test_the_engine_rates_the_real_pool() -> None:
    snap = snapshot()
    stat_map = build_stat_map(snap.settings.stat_categories, snap.game_stat_categories)
    for stats in (snap.stats, snap.last_season_stats or {}):
        seasons = [to_player_season(p, stats.get(p.player_key), stat_map) for p in snap.pool]
        result = rate(seasons)
        rated = [r for r in result.ratings.values() if r.rated]
        assert len(rated) > 300
        assert all(math.isfinite(r.ttltst) for r in rated if r.ttltst is not None)
        assert all(d > 0 for d in result.divisors.values())


def test_every_real_name_normalizes() -> None:
    names = [p.name for p in snapshot().pool]
    assert all(normalize_name(n) for n in names)
