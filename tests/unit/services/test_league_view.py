"""The rated league the screens show: seasons, salaries, rows (SPEC §4, §5, §7)."""

from dataclasses import replace

import pytest

from fha.domain.engine import DEFAULT_CONFIG, EngineConfig
from fha.domain.matcher import NO_ALIASES
from fha.services.clock import Clock
from fha.services.free_agents import import_free_agent_salaries
from fha.services.league_sheet import RosteredSalary, bind_tab, read_league_sheet
from fha.services.league_view import (
    AavSource,
    Salaries,
    Season,
    ViewError,
    build_view,
    load_salaries,
)
from fha.sources.league_sheet.parse import parse_sheet
from fha.sources.league_sheet.source import FakeLeagueSheet
from fha.sources.puckpedia import SalaryRow
from fha.sources.yahoo.models import LeagueSnapshot
from fha.storage.memory import InMemoryRepository
from tests.unit.league_sheet import sheets as s
from tests.unit.services.snapshots import synthetic_snapshot
from tests.unit.yahoo.fake_league import LK


@pytest.fixture
async def snap() -> LeagueSnapshot:
    return await synthetic_snapshot()


def test_rows_cover_the_pool_rated_skaters_first_then_unrated_then_goalies(
    snap: LeagueSnapshot,
) -> None:
    view = build_view(snap, DEFAULT_CONFIG, Salaries())
    assert {r.player_id for r in view.rows} == {p.player_id for p in snap.pool}
    kinds = [
        "goalie" if r.is_goalie else "rated" if r.ttltst is not None else "unrated"
        for r in view.rows
    ]
    assert kinds == sorted(kinds, key=["rated", "unrated", "goalie"].index)
    ranks = [r.rating.rank for r in view.rows if r.rating and r.rating.rank]
    assert ranks == sorted(ranks)
    goalie = next(r for r in view.rows if r.is_goalie)
    assert goalie.rating is None
    assert set(goalie.goalie) == {"W", "GAA", "SV%"}


def test_owners_slots_and_free_agents(snap: LeagueSnapshot) -> None:
    view = build_view(snap, DEFAULT_CONFIG, Salaries())
    by_id = view.by_id
    assert (by_id["1"].owner_key, by_id["1"].slot) == (f"{LK}.t.1", "C")
    assert by_id["2"].in_ir_slot
    assert by_id["100"].owner_key is None
    assert len(view.free_agents()) == 60
    assert [r.player_id for r in view.roster(f"{LK}.t.1")] == ["1", "2", "3"]
    assert view.roster("nobody") == []
    assert view.my_team is not None
    assert view.my_team.team_key == f"{LK}.t.1"


def test_the_sheet_wins_over_the_csv_and_unknown_is_none(snap: LeagueSnapshot) -> None:
    salaries = Salaries(
        rostered={
            "1": RosteredSalary(5_000_000, True, "tab"),
            "2": RosteredSalary(9, False, "tab"),
        },
        free_agents={"1": 1, "100": 800_000},
        payrolls={f"{LK}.t.1": 5_000_009},
        cap=119_600_000,
    )
    view = build_view(snap, DEFAULT_CONFIG, salaries)
    by_id = view.by_id
    assert (by_id["1"].aav, by_id["1"].aav_source, by_id["1"].counts) == (
        5_000_000,
        AavSource.SHEET,
        True,
    )
    assert (by_id["2"].counts, by_id["2"].aav) == (False, 9)
    assert (by_id["100"].aav, by_id["100"].aav_source, by_id["100"].counts) == (
        800_000,
        AavSource.CSV,
        None,
    )
    assert (by_id["101"].aav, by_id["101"].aav_source) == (None, None)
    ttltst = by_id["1"].ttltst
    assert ttltst is not None  # player 1 has the most games: rated
    assert by_id["1"].value == pytest.approx(5.0 / ttltst)  # $M per TTLTST
    assert view.cap == 119_600_000
    team = view.team(f"{LK}.t.1")
    assert team is not None
    assert team.payroll == 5_000_009
    assert view.team(f"{LK}.t.2") is not None
    assert view.team(f"{LK}.t.2").payroll is None  # type: ignore[union-attr]


def test_the_default_season_follows_the_baseline_rule(snap: LeagueSnapshot) -> None:
    # The synthetic league's most-played skater has 10 GP this season.
    assert build_view(snap, DEFAULT_CONFIG, Salaries()).season is Season.CURRENT
    early = build_view(snap, DEFAULT_CONFIG, Salaries(), baseline_min_gp=11)
    assert (early.season, early.default_season, early.season_year) == (
        Season.LAST,
        Season.LAST,
        2025,
    )
    forced = build_view(snap, DEFAULT_CONFIG, Salaries(), season=Season.CURRENT, baseline_min_gp=11)
    assert (forced.season, forced.default_season, forced.season_year) == (
        Season.CURRENT,
        Season.LAST,
        2026,
    )


async def test_without_last_season_the_default_is_this_season() -> None:
    snap = await synthetic_snapshot(last_season=False)
    view = build_view(snap, DEFAULT_CONFIG, Salaries(), baseline_min_gp=99)
    assert (view.season, view.last_season_available) == (Season.CURRENT, False)
    with pytest.raises(ViewError, match="last season's stats weren't fetched"):
        build_view(snap, DEFAULT_CONFIG, Salaries(), season=Season.LAST)


def test_last_season_rates_last_seasons_stats(snap: LeagueSnapshot) -> None:
    view = build_view(snap, DEFAULT_CONFIG, Salaries(), season=Season.LAST)
    assert view.by_id["1"].gp == 82
    assert view.by_id["100"].gp == 0


def test_a_settings_change_re_rates_without_a_refresh(snap: LeagueSnapshot) -> None:
    loose = build_view(snap, DEFAULT_CONFIG, Salaries())
    strict = build_view(snap, EngineConfig(gp_floor_fraction=0.5), Salaries())
    assert loose.result.eligible_count > strict.result.eligible_count
    assert strict.config.gp_floor_fraction == 0.5


def test_a_league_missing_a_stat_is_a_view_error(snap: LeagueSnapshot) -> None:
    broken = replace(
        snap, game_stat_categories=(), settings=replace(snap.settings, stat_categories=())
    )
    with pytest.raises(ViewError, match="no Yahoo stat"):
        build_view(broken, DEFAULT_CONFIG, Salaries())


class FixedClock:
    def now(self) -> float:
        return 1_800_000_000.0


async def test_load_salaries_reads_the_sheet_bindings_and_the_csv(snap: LeagueSnapshot) -> None:
    repo = InMemoryRepository()
    clock: Clock = FixedClock()
    mine = s.TeamTab(
        "Mine", [s.Player("Player 1", "C", "TBL", 5_000_000), s.Player("Player 2", "C", "TBL", 1)]
    )
    await read_league_sheet(repo, FakeLeagueSheet(parse_sheet(s.grid(mine))), clock)
    await bind_tab(repo, "Mine", f"{LK}.t.1")
    await import_free_agent_salaries(
        repo, [SalaryRow("Player 100", "C", None, 750_000, 2)], snap.pool, NO_ALIASES
    )
    salaries, reports = await load_salaries(repo, snap)
    assert salaries.rostered["1"] == RosteredSalary(5_000_000, True, "Mine")
    assert salaries.free_agents == {"100": 750_000}
    assert salaries.payrolls == {f"{LK}.t.1": 5_000_001}
    assert salaries.cap == s.CAP
    assert [r.tab.name for r in reports] == ["Mine"]


async def test_load_salaries_without_a_sheet_uses_the_cap_override(snap: LeagueSnapshot) -> None:
    salaries, reports = await load_salaries(InMemoryRepository(), snap, cap_override=120_000_000)
    assert (salaries.cap, salaries.rostered, salaries.payrolls, reports) == (
        120_000_000,
        {},
        {},
        [],
    )
