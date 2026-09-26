"""A full refresh through HttpYahooSource against a mocked Yahoo (SPEC §10 M2)."""

from typing import Any

import httpx
import pytest

from fha.sources.yahoo import source as source_module
from fha.sources.yahoo.client import YahooClient
from fha.sources.yahoo.models import LeagueSnapshot
from fha.sources.yahoo.oauth import Credentials, Token
from fha.sources.yahoo.parse import YahooParseError
from fha.sources.yahoo.source import HttpYahooSource
from fha.sources.yahoo.stat_map import build_stat_map, to_player_season
from tests.unit.yahoo import builders as b
from tests.unit.yahoo.fake_league import FREE, LK, MINE, THEIRS, League


class Store:
    async def load(self) -> Token:
        return Token("access", "refresh", 10_000.0)

    async def save(self, token: Token) -> None:
        raise AssertionError("no refresh expected")


def source(league: League, max_concurrency: int = 8, **kw: Any) -> HttpYahooSource:
    http = httpx.AsyncClient(transport=httpx.MockTransport(league))
    client = YahooClient(
        http, Credentials("c", "s"), Store(), clock=lambda: 0.0, max_concurrency=max_concurrency
    )
    return HttpYahooSource(client, **{"available": 60, **kw})


async def refresh(league: League | None = None, **kw: Any) -> tuple[LeagueSnapshot, League]:
    league = league or League()
    return await source(league, **kw).fetch_snapshot(), league


async def test_full_refresh_issues_calls_concurrently_within_the_bound() -> None:
    snap, league = await refresh(max_concurrency=4)
    # 1 game + 2 settings/categories + 1 rosters + 3 pages + 2 boards
    # + stats per season: 1 rostered batch + 1 per page
    assert len(league.paths) == 17
    assert len(set(league.paths)) == 17
    assert league.max_in_flight == 4
    assert snap.settings.league_key == LK


async def test_default_bound_is_eight() -> None:
    _, league = await refresh(available=200)  # 8 pages + rosters + 2 boards in one stage
    assert league.max_in_flight == 8


async def test_game_key_is_resolved_at_runtime() -> None:
    snap, league = await refresh()
    assert (snap.game.game_key, snap.game.season) == ("465", 2026)
    assert league.paths[0] == "games;game_codes=nhl"
    assert all(LK in p or not p.startswith("league/") for p in league.paths)


async def test_rosters_with_slots_and_my_team() -> None:
    snap, _ = await refresh()
    mine = snap.my_team
    assert mine is not None
    assert mine.team_key == f"{LK}.t.1"
    assert [(e.player.player_id, e.selected_position, e.in_ir_slot) for e in mine.roster] == [
        ("1", "C", False),
        ("2", "IR", True),
        ("3", "G", False),
    ]
    assert snap.owner_of("465.p.6") == snap.teams[1]
    assert snap.owner_of("465.p.100") is None


async def test_pool_is_rostered_then_available() -> None:
    snap, _ = await refresh()
    ids = [p.player_id for p in snap.pool]
    assert ids[:6] == ["1", "2", "3", "4", "5", "6"]
    assert ids[6:] == [str(100 + i) for i in range(60)]
    assert set(snap.stats) == {p.player_key for p in snap.pool}


async def test_available_count_limits_pages_and_pool() -> None:
    snap, league = await refresh(available=30)
    pages = [p for p in league.paths if ";status=A;" in p]
    assert sorted(pages) == [
        f"league/{LK}/players;status=A;sort=AR;start=0;count=25",
        f"league/{LK}/players;status=A;sort=AR;start=25;count=5",
    ]
    assert len(snap.available) == 30


async def test_available_sort_is_configurable() -> None:
    league = League()
    league.overrides[f"league/{LK}/players;status=A;sort=OR;start=0;count=25"] = b.league_players(
        FREE[:25]
    )
    snap, league = await refresh(league, available=25, available_sort="OR")
    assert len(snap.available) == 25


async def test_available_players_already_rostered_or_repeated_are_dropped() -> None:
    league = League()
    rostered = MINE.roster[0][0]
    page = [rostered, *FREE[:24]]
    league.overrides[f"league/{LK}/players;status=A;sort=AR;start=0;count=25"] = b.league_players(
        page
    )
    league.overrides[f"league/{LK}/players;status=A;sort=AR;start=25;count=25"] = b.league_players(
        FREE[20:45]
    )  # overlaps the first page (ranks shifted mid-refresh)
    snap, _ = await refresh(league, available=50)
    keys = [p.player_key for p in snap.pool]
    assert len(keys) == len(set(keys))
    assert keys.count(rostered.key) == 1


async def test_no_available_players() -> None:
    snap, league = await refresh(available=0)
    assert snap.available == ()
    assert not any(";status=A;" in p for p in league.paths)


def test_available_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="available"):
        source(League(), available=-1)


async def test_current_and_next_week_opponents() -> None:
    snap, _ = await refresh()
    assert snap.scoreboard.week == 1
    assert snap.next_scoreboard is not None
    assert snap.next_scoreboard.week == 2
    assert snap.scoreboard.opponent(f"{LK}.t.1") == f"{LK}.t.2"
    assert snap.next_scoreboard.opponent(f"{LK}.t.1") == f"{LK}.t.2"


async def test_last_week_has_no_next_scoreboard() -> None:
    snap, league = await refresh(League(current_week=25))
    assert snap.scoreboard.week == 25
    assert snap.next_scoreboard is None
    assert not any("scoreboard;week=26" in p for p in league.paths)


async def test_last_season_stats_for_the_same_pool() -> None:
    snap, league = await refresh()
    assert snap.last_season_stats is not None
    assert set(snap.last_season_stats) == set(snap.stats)
    assert snap.last_season_stats["465.p.1"].season == 2025
    assert snap.last_season_stats["465.p.1"].values == {"0": "82"}
    assert sum(";season=2025" in p for p in league.paths) == 4


async def test_last_season_can_be_skipped() -> None:
    league = League()
    snap = await source(league).fetch_snapshot(last_season=False)
    assert snap.last_season_stats is None
    assert not any(";season=" in p for p in league.paths)


async def test_snapshot_feeds_the_engine_input() -> None:
    snap, _ = await refresh()
    stat_map = build_stat_map(snap.settings.stat_categories, snap.game_stat_categories)
    assert stat_map.ppp_direct
    first = snap.pool[0]
    season = to_player_season(first, snap.stats[first.player_key], stat_map)
    assert (season.player_id, season.gp) == ("1", 10)


async def test_settings_for_another_league_is_an_error() -> None:
    league = League(league_key="465.l.9999")
    with pytest.raises(YahooParseError, match=r"asked for league 465\.l\.8076"):
        await refresh(league)


async def test_settings_for_another_season_is_an_error() -> None:
    league = League(season="2025")
    with pytest.raises(YahooParseError, match=r"\(2026\), got 465\.l\.8076 \(2025\)"):
        await refresh(league)


async def test_scoreboard_for_the_wrong_week_is_an_error() -> None:
    league = League()
    league.overrides[f"league/{LK}/scoreboard;week=2"] = b.scoreboard(1, [])
    with pytest.raises(YahooParseError, match="week 2 scoreboard, got week 1"):
        await refresh(league)


async def test_missing_stats_for_a_pool_player_is_an_error() -> None:
    league = League(current_week=25)
    keys = ",".join(p.key for p, _ in [*MINE.roster, *THEIRS.roster])
    league.overrides[f"players;player_keys={keys}/stats;type=season"] = b.player_stats(
        [p for p, _ in MINE.roster]
    )
    with pytest.raises(YahooParseError, match=r"no 2026 stats for 3 players, e\.g\. 465\.p\.4"):
        await refresh(league, available=0)


# ---------------------------------------------------------------- every available player


async def test_by_default_every_available_player_is_read() -> None:
    league = League()
    client = source(league)._client
    snap = await HttpYahooSource(client).fetch_snapshot()  # no `available`: all of them
    assert len(snap.available) == len(FREE) == 200
    assert len(snap.pool) == 206
    assert set(snap.stats) == {p.player_key for p in snap.pool}
    pages = sorted(
        int(p.split("start=")[1].split(";")[0]) for p in league.paths if ";status=A;" in p
    )
    # Wave 1 (0-175) is all full pages, so wave 2 (200-375) is read and comes back empty.
    assert pages == list(range(0, 400, 25))


async def test_paging_stops_at_the_first_short_page() -> None:
    league = League()
    league.overrides.update(
        {
            f"league/{LK}/players;status=A;sort=AR;start={s};count=25": b.league_players(
                FREE[s : s + 25] if s < 150 else FREE[150:160] if s == 150 else []
            )
            for s in range(0, 200, 25)
        }
    )
    snap = await source(league, available=None).fetch_snapshot()
    assert len(snap.available) == 160
    assert (
        max(int(p.split("start=")[1].split(";")[0]) for p in league.paths if "status=A" in p) == 175
    )


async def test_paging_gives_up_past_the_safety_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_module, "MAX_AVAILABLE", 200)  # FREE fills the first wave exactly
    with pytest.raises(YahooParseError, match="more than 200 available players"):
        await source(League(), available=None).fetch_snapshot()


async def test_stats_are_requested_while_paging_continues() -> None:
    league = League()
    await source(league, available=None).fetch_snapshot()
    first_stats = next(i for i, p in enumerate(league.paths) if p.startswith("players;"))
    last_page = max(i for i, p in enumerate(league.paths) if ";status=A;" in p)
    assert first_stats < last_page


async def test_a_failure_in_one_request_is_raised_as_itself() -> None:
    league = League()
    league.overrides[f"league/{LK}/players;status=A;sort=AR;start=25;count=25"] = {"league": []}
    with pytest.raises(YahooParseError, match="missing 'players'"):
        await refresh(league)
