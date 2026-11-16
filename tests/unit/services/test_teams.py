"""Team views: rosters, league table, matchup, Replace (SPEC §7.2-7.4)."""

from dataclasses import replace
from itertools import pairwise
from typing import Any

import pytest

from fha.domain.engine import DEFAULT_CONFIG, EngineConfig
from fha.domain.models import Category
from fha.domain.standings import EMPTY
from fha.services.league_sheet import RosteredSalary, TabReport
from fha.services.league_view import LeagueView, Salaries, Season, build_view
from fha.services.players_table import QueryError
from fha.services.teams import (
    LeagueQuery,
    LeagueSort,
    discrepancy_flags,
    free_agents_by_need,
    league_table,
    matchup_view,
    members,
    replace_view,
    summarize,
)
from fha.sources.league_sheet.models import ParsedTab
from fha.sources.yahoo.demo import demo_snapshot
from fha.sources.yahoo.models import LeagueSnapshot, Player, Scoreboard
from tests.unit.services.snapshots import synthetic_snapshot
from tests.unit.yahoo.fake_league import LK

MINE, THEIRS = f"{LK}.t.1", f"{LK}.t.2"
CAP = 10_000_000


@pytest.fixture
async def snap() -> LeagueSnapshot:
    return await synthetic_snapshot()


def view_of(snap: LeagueSnapshot, **salaries: object) -> LeagueView:
    base = {
        "rostered": {
            "1": RosteredSalary(4_000_000, True, "t"),
            "2": RosteredSalary(3_000_000, False, "t"),  # an IR row: not counted
        },
        "free_agents": {"101": 500_000, "102": 9_000_000, "103": 4_500_000},  # all have played
        "payrolls": {MINE: 9_000_000},
        "cap": CAP,
    }
    return build_view(snap, DEFAULT_CONFIG, Salaries(**{**base, **salaries}))  # type: ignore[arg-type]


def test_a_team_summary_excludes_ir_and_goalies_from_the_profile(snap: LeagueSnapshot) -> None:
    view = view_of(snap)
    mine = view.my_team
    assert mine is not None
    summary = summarize(view, mine)
    assert [p.player_id for p in summary.profile.players] == ["1"]  # 2 is in IR, 3 is a goalie
    assert (summary.payroll, summary.cap_room, summary.over_cap) == (9_000_000, 1_000_000, False)
    assert [m.player_id for m in members(view, MINE)] == ["1", "2", "3"]
    assert members(view, MINE)[2].rating is None


def test_over_the_cap_and_an_unbound_team(snap: LeagueSnapshot) -> None:
    view = view_of(snap, payrolls={MINE: 11_000_000})
    mine, theirs = view.team(MINE), view.team(THEIRS)
    assert mine is not None
    assert theirs is not None
    assert summarize(view, mine).over_cap
    unbound = summarize(view, theirs)
    assert (unbound.payroll, unbound.cap_room, unbound.over_cap) == (None, None, False)


def test_this_weeks_and_next_weeks_matchup(snap: LeagueSnapshot) -> None:
    view = view_of(snap)
    now = matchup_view(view)
    assert now is not None
    assert (now.week, now.me.team.team_key) == (1, MINE)
    assert now.opponent is not None
    assert now.opponent.team.team_key == THEIRS
    assert now.comparison is not None
    assert set(now.comparison.diff) == set(Category)
    later = matchup_view(view, next_week=True)
    assert later is not None
    assert later.week == 2


async def test_no_next_week_after_the_last_week_and_no_matchup_without_my_team() -> None:
    snap = await synthetic_snapshot(current_week=25)
    assert matchup_view(view_of(snap), next_week=True) is None
    orphan = replace(snap, teams=tuple(replace(t, is_mine=False) for t in snap.teams))
    assert matchup_view(view_of(orphan)) is None


def test_a_bye_week_has_no_opponent(snap: LeagueSnapshot) -> None:
    bye = replace(snap, scoreboard=Scoreboard(1, None, None, ()))
    now = matchup_view(view_of(bye))
    assert now is not None
    assert (now.opponent, now.comparison) == (None, None)
    assert free_agents_by_need(view_of(bye), now).rows == ()


def test_free_agents_by_need_are_sorted_and_the_cap_filter_counts_unknowns(
    snap: LeagueSnapshot,
) -> None:
    view = view_of(snap)
    now = matchup_view(view)
    assert now is not None
    assert now.comparison is not None
    assert now.comparison.trailing  # the synthetic opponent leads somewhere
    ranked = free_agents_by_need(view, now)
    needs = [n.need for n in ranked.rows]
    assert needs == sorted(needs, reverse=True)
    assert ranked.excluded_unknown == 0
    fitting = free_agents_by_need(view, now, fits_my_cap=True)
    fit_ids = {n.player.player_id for n in fitting.rows}
    assert "101" in fit_ids  # $0.5M <= $1M room
    assert "102" not in fit_ids  # $9M doesn't fit
    assert all(n.fits is True for n in fitting.rows)
    unknown = sum(n.fits is None for n in ranked.rows)
    assert fitting.excluded_unknown == unknown > 0  # FAs with no AAV


def test_replace_lists_free_agents_at_the_players_positions(snap: LeagueSnapshot) -> None:
    view = view_of(snap)
    swap = replace_view(view, "1")
    assert swap is not None
    assert swap.drop.player_id == "1"
    assert all("C" in r.player.eligible_positions for r in swap.rows)
    ttl = [r.player.ttltst for r in swap.rows if r.player.ttltst is not None]
    assert ttl == sorted(ttl, reverse=True)
    by_id = {r.player.player_id: r for r in swap.rows}
    # Room 1M + Knight's counted 4M - the add.
    assert (by_id["101"].room_after, by_id["101"].swap_ok) == (4_500_000, True)
    assert (by_id["102"].room_after, by_id["102"].swap_ok) == (-4_000_000, False)
    assert (by_id["103"].room_after, by_id["103"].swap_ok) == (500_000, True)
    assert (by_id["104"].room_after, by_id["104"].swap_ok) == (None, None)  # no AAV
    drop_ttl = swap.drop.ttltst
    assert drop_ttl is not None
    rated = next(r for r in swap.rows if r.player.ttltst is not None)
    assert rated.delta_ttltst == pytest.approx(rated.player.ttltst - drop_ttl)  # type: ignore[operator]
    assert set(rated.delta_norms) == set(Category)
    for cat in Category:  # the free agent's norm minus the dropped player's, not the reverse
        assert rated.delta_norms[cat] == pytest.approx(
            rated.player.norms[cat] - swap.drop.norms[cat]
        )
    assert any(v != 0 for v in rated.delta_norms.values())


def test_the_swap_ok_toggle_keeps_only_legal_swaps_and_counts_unknowns(
    snap: LeagueSnapshot,
) -> None:
    view = view_of(snap)
    everyone = replace_view(view, "1")
    legal = replace_view(view, "1", swap_ok_only=True)
    assert everyone is not None
    assert legal is not None
    assert {r.player.player_id for r in legal.rows} == {"101", "103"}
    assert legal.excluded_unknown == sum(r.swap_ok is None for r in everyone.rows)


def test_an_ir_drop_frees_nothing_and_unrated_players_have_no_deltas(
    snap: LeagueSnapshot,
) -> None:
    swap = replace_view(view_of(snap), "2")  # Player 2 is on an IR row
    assert swap is not None
    by_id = {r.player.player_id: r for r in swap.rows}
    assert by_id["101"].room_after == 500_000  # 1M room, nothing freed, 0.5M added
    unrated = next(r for r in swap.rows if r.player.ttltst is None)
    assert (unrated.delta_ttltst, unrated.delta_norms) == (None, {})


def test_replace_needs_a_player_on_my_roster(snap: LeagueSnapshot) -> None:
    view = view_of(snap)
    assert replace_view(view, "4") is None  # theirs
    assert replace_view(view, "nobody") is None
    orphan = replace(snap, teams=tuple(replace(t, is_mine=False) for t in snap.teams))
    assert replace_view(view_of(orphan), "1") is None


def test_a_drop_missing_from_the_sheet_frees_nothing(snap: LeagueSnapshot) -> None:
    view = view_of(snap, rostered={}, payrolls={MINE: 9_000_000})
    swap = replace_view(view, "1")
    assert swap is not None
    assert {r.player.player_id: r.room_after for r in swap.rows}["101"] == 500_000


def test_discrepancy_flags_come_from_bound_tab_reports() -> None:
    tab = ParsedTab("T", "ok", None, 1, None, "F")
    reports = [
        TabReport(tab, MINE, (), (), (), 0),
        TabReport(replace(tab, cap=5), THEIRS, (), (), (), 0, sheet_cap=6),
        TabReport(tab, None, (), (), (), 0),
    ]
    assert discrepancy_flags(reports) == {MINE: True, THEIRS: True}


def test_replacing_a_goalie_offers_no_skaters(snap: LeagueSnapshot) -> None:
    swap = replace_view(view_of(snap), "3")  # the only free agents are centres
    assert swap is not None
    assert swap.rows == ()


def with_positions(snap: LeagueSnapshot, positions: dict[str, tuple[str, ...]]) -> LeagueSnapshot:
    """The snapshot with some players' eligible positions changed (by player_id)."""

    def moved(p: Player) -> Player:
        eligible = positions.get(p.player_id)
        return (
            p
            if eligible is None
            else replace(p, eligible_positions=eligible, display_position=",".join(eligible))
        )

    teams = tuple(
        replace(t, roster=tuple(replace(e, player=moved(e.player)) for e in t.roster))
        for t in snap.teams
    )
    return replace(snap, teams=teams, available=tuple(moved(p) for p in snap.available))


def test_replace_takes_free_agents_eligible_at_any_of_the_drops_positions(
    snap: LeagueSnapshot,
) -> None:
    """M4R1B-5: a LW drop gets the C,LW and LW free agents but not the C-only or RW ones;
    a C,LW drop gets both kinds. Bench and IR slots never count as positions."""
    moved = with_positions(
        snap,
        {
            "1": ("LW",),
            "101": ("C", "LW"),
            "102": ("LW", "IR"),
            "103": ("RW",),
            "104": ("C", "BN", "Util"),
        },
    )
    swap = replace_view(view_of(moved), "1")
    assert swap is not None
    got = {r.player.player_id for r in swap.rows}
    assert {"101", "102"} <= got
    assert not got & {"103", "104"}
    assert all("LW" in r.player.eligible_positions for r in swap.rows)
    util = replace_view(view_of(with_positions(moved, {"1": ("LW", "Util")})), "1")
    assert util is not None
    assert not {r.player.player_id for r in util.rows} & {"103", "104"}  # Util isn't a position
    both = replace_view(view_of(with_positions(moved, {"1": ("C", "LW")})), "1")
    assert both is not None
    assert {"101", "102", "104"} <= {r.player.player_id for r in both.rows}
    assert "103" not in {r.player.player_id for r in both.rows}


def test_fits_my_cap_includes_a_free_agent_whose_cap_hit_is_exactly_the_room(
    snap: LeagueSnapshot,
) -> None:
    """M4R1B-5: room is $1M; an AAV of exactly $1M fits (<=), a dollar more doesn't."""
    view = view_of(snap, free_agents={"101": 1_000_000, "102": 1_000_001})
    now = matchup_view(view)
    assert now is not None
    assert now.me.cap_room == 1_000_000
    fitting = free_agents_by_need(view, now, fits_my_cap=True)
    ids = {n.player.player_id for n in fitting.rows}
    assert "101" in ids
    assert "102" not in ids
    everyone = {n.player.player_id: n.fits for n in free_agents_by_need(view, now).rows}
    assert (everyone["101"], everyone["102"]) == (True, False)
    assert fitting.excluded_unknown == sum(f is None for f in everyone.values())


def test_the_swap_ok_toggle_includes_a_swap_that_leaves_exactly_zero_room(
    snap: LeagueSnapshot,
) -> None:
    """Room $1M + the drop's counted $4M = $5M: an add of exactly $5M is a legal swap."""
    view = view_of(snap, free_agents={"101": 5_000_000, "102": 5_000_001})
    legal = replace_view(view, "1", swap_ok_only=True)
    assert legal is not None
    by_id = {r.player.player_id: r for r in legal.rows}
    assert by_id["101"].room_after == 0
    assert "102" not in by_id


def test_free_agents_by_need_order_is_need_then_name_then_id(snap: LeagueSnapshot) -> None:
    view = view_of(snap)
    now = matchup_view(view)
    assert now is not None
    rows = free_agents_by_need(view, now).rows
    keys = [(-n.need, n.player.name, n.player.player_id) for n in rows]
    assert keys == sorted(keys)
    assert len({n.need for n in rows}) > 1


def test_equal_scores_are_ordered_by_name_then_id(snap: LeagueSnapshot) -> None:
    """Free agents 101 and 136 have identical stats (the fake league's stats repeat
    every 35), so they tie on need and on TTLTST. Renaming 136 so its name sorts
    first shows that ties go by name, not by id."""
    renamed = replace(
        snap,
        available=tuple(
            replace(p, name="Aaron Tie") if p.player_id == "136" else p for p in snap.available
        ),
    )
    view = view_of(renamed)
    assert view.by_id["101"].ttltst == view.by_id["136"].ttltst is not None
    now = matchup_view(view)
    assert now is not None
    need = [n.player.player_id for n in free_agents_by_need(view, now).rows]
    assert need.index("136") == need.index("101") - 1
    swap = replace_view(view, "1")
    assert swap is not None
    order = [r.player.player_id for r in swap.rows]
    assert order.index("136") == order.index("101") - 1


def test_an_ir_plus_slot_is_outside_the_profile_too(snap: LeagueSnapshot) -> None:
    """SPEC §5: IR and IR+ slots are both excluded (not only "IR")."""
    moved = replace(
        snap,
        teams=tuple(
            replace(
                t,
                roster=tuple(
                    replace(e, selected_position="IR+") if e.player.player_id == "5" else e
                    for e in t.roster
                ),
            )
            for t in snap.teams
        ),
    )
    view = view_of(moved)
    theirs = view.team(THEIRS)
    assert theirs is not None
    rating = view.by_id["5"].rating
    assert rating is not None
    assert rating.rated
    assert [p.player_id for p in summarize(view, theirs).profile.players] == ["4"]


def test_exactly_no_room_is_not_over_the_cap(snap: LeagueSnapshot) -> None:
    view = view_of(snap, payrolls={MINE: CAP})
    mine = view.team(MINE)
    assert mine is not None
    summary = summarize(view, mine)
    assert (summary.cap_room, summary.over_cap) == (0, False)


# ---------------------------------------------------------------- league table (standings)


def demo_view(
    season: Season, config: EngineConfig = DEFAULT_CONFIG, **salaries: object
) -> LeagueView:
    snap = demo_snapshot()
    return build_view(snap, config, Salaries(**salaries), season=season)  # type: ignore[arg-type]


def test_the_league_table_flags_discrepancies_and_does_not_pin_my_team(
    snap: LeagueSnapshot,
) -> None:
    table = league_table(view_of(snap), discrepancies={THEIRS: True})
    flags = {r.team.team_key: r.summary.has_discrepancies for r in table.rows}
    assert flags == {MINE: False, THEIRS: True}
    # Roto: THEIRS leads every category it differs in, so it is first, mine second.
    assert [r.team.team_key for r in table.rows] == [THEIRS, MINE]
    assert table.mine is not None
    assert table.mine.team.team_key == MINE


def test_without_my_team_there_is_no_mine(snap: LeagueSnapshot) -> None:
    no_mine = replace(snap, teams=tuple(replace(t, is_mine=False) for t in snap.teams))
    assert league_table(view_of(no_mine)).mine is None


# Each sort's value, read off the row directly (not through the code under test).
LEAGUE_COLUMN: dict[LeagueSort | Category, Any] = {
    LeagueSort.TEAM: lambda r: r.team.name.casefold(),
    LeagueSort.ROTO: lambda r: r.standing.roto,
    LeagueSort.TTLTST: lambda r: r.summary.profile.ttltst,
    LeagueSort.PAYROLL: lambda r: r.summary.payroll,
    LeagueSort.ROOM: lambda r: r.summary.cap_room,
    **{c: (lambda r, c=c: r.summary.profile.mean_norm[c]) for c in Category},
}


@pytest.mark.parametrize("descending", [True, False])
@pytest.mark.parametrize("key", list(LEAGUE_COLUMN))
def test_every_league_column_sorts_both_ways_with_blanks_last(
    key: LeagueSort | Category, descending: bool
) -> None:
    snap = demo_snapshot()
    keys = [t.team_key for t in snap.teams]
    payrolls = {k: 80_000_000 + i * 1_000_000 for i, k in enumerate(keys[:5])}  # 3 unknown
    view = demo_view(Season.LAST, payrolls=payrolls, cap=84_000_000)
    rows = league_table(view, LeagueQuery(key, descending)).rows
    values = [LEAGUE_COLUMN[key](r) for r in rows]
    known = [v for v in values if v is not None]
    assert values[: len(known)] == sorted(known, reverse=descending)
    assert all(v is None for v in values[len(known) :])
    for a, b in pairwise(rows):  # ties (and blanks) by team name
        if LEAGUE_COLUMN[key](a) == LEAGUE_COLUMN[key](b):
            assert a.team.name < b.team.name
    if key in (LeagueSort.PAYROLL, LeagueSort.ROOM):
        assert len(known) == 5


def test_league_params_parse_with_defaults() -> None:
    assert LeagueQuery.from_params({}) == LeagueQuery(LeagueSort.ROTO, True)
    assert LeagueQuery.from_params({"sort": "team"}) == LeagueQuery(LeagueSort.TEAM, False)
    assert LeagueQuery.from_params({"sort": "HIT"}) == LeagueQuery(Category.HIT, True)
    assert LeagueQuery.from_params({"sort": "room", "dir": "asc"}) == LeagueQuery(
        LeagueSort.ROOM, False
    )
    assert LeagueQuery.from_params({"sort": " ", "dir": ""}) == LeagueQuery()


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"sort": "points"}, "unknown sort 'points'"),
        ({"sort": "x" * 13}, "unknown sort 'xxxxxxxxxxxx…'"),
        ({"dir": "up"}, "dir must be asc or desc, got 'up'"),
        ({"dir": "y" * 13}, "unknown dir 'yyyyyyyyyyyy…'"),
    ],
)
def test_bad_league_params_are_refused(params: dict[str, str], message: str) -> None:
    with pytest.raises(QueryError) as e:
        LeagueQuery.from_params(params)
    assert str(e.value) == message


def test_a_category_outside_the_league_config_is_refused() -> None:
    config = replace(DEFAULT_CONFIG, categories=(Category.G, Category.A))
    view = demo_view(Season.LAST, config)
    with pytest.raises(QueryError, match="HIT isn't one of this league's categories"):
        league_table(view, LeagueQuery(Category.HIT))
    table = league_table(view, LeagueQuery(Category.G))
    assert table.categories == (Category.G, Category.A)
    assert all(list(r.standing.categories) == [Category.G, Category.A] for r in table.rows)


def pctl_by_hand(view: LeagueView, value: float) -> float:
    """The players' scale, counted off the rows: 1 + rated skaters above, over the
    skaters with GP > 0 (rated or not)."""
    skaters = [r for r in view.rows if not r.is_goalie]
    above = sum(1 for r in skaters if r.ttltst is not None and r.ttltst > value)
    pool = sum(1 for r in skaters if r.gp > 0)
    return (1 - (1 + above) / pool) * 100


@pytest.mark.parametrize("season", [Season.LAST, Season.CURRENT])
def test_the_standings_use_the_season_shown(season: Season) -> None:
    """Ranks, Pctl, roto points and per-82 gaps from that season's ratings, pool and
    divisors (the demo's team order and divisors differ between the seasons)."""
    view = demo_view(season)
    rows = league_table(view, LeagueQuery(LeagueSort.TTLTST)).rows
    ttl = [r.summary.profile.ttltst for r in rows]
    assert [r.standing.ttltst.rank for r in rows] == list(range(1, len(rows) + 1))
    assert ttl == sorted(ttl, reverse=True)  # type: ignore[type-var]
    for r in rows:
        assert r.standing.ttltst.value == r.summary.profile.ttltst
        assert r.standing.ttltst.pctl == pytest.approx(pctl_by_hand(view, r.standing.ttltst.value))
        g = r.standing.categories[Category.G]
        if g.gap is not None:
            assert g.gap_per82 == pytest.approx(g.gap * view.result.divisors[Category.G])
    n = len(rows)
    for cat in view.config.categories:
        assert sum(r.standing.categories[cat].points or 0 for r in rows) == n * (n + 1) / 2


def test_the_demo_seasons_differ_so_the_wiring_test_can_tell_them_apart() -> None:
    last, current = demo_view(Season.LAST), demo_view(Season.CURRENT)
    order = [
        [r.team.team_key for r in league_table(v, LeagueQuery(LeagueSort.TTLTST)).rows]
        for v in (last, current)
    ]
    assert order[0] != order[1]
    assert last.result.divisors[Category.G] != current.result.divisors[Category.G]


def test_the_pctl_pool_counts_unrated_skaters_with_games_not_goalies_or_zero_gp() -> None:
    view = demo_view(Season.LAST, replace(DEFAULT_CONFIG, gp_floor_fraction=0.3))  # floor 24.6 GP
    skaters = [r for r in view.rows if not r.is_goalie]
    assert any(r.gp > 0 and r.ttltst is None for r in skaters)  # below the floor
    assert any(r.gp == 0 for r in skaters)
    assert any(r.is_goalie for r in view.rows)
    for r in league_table(view).rows:
        value = r.standing.ttltst.value
        assert value is not None
        assert r.standing.ttltst.pctl == pytest.approx(pctl_by_hand(view, value))


@pytest.mark.parametrize("case", ["no games", "nobody eligible"])
def test_a_season_with_nobody_rated_has_no_standings_and_no_error(case: str) -> None:
    snap = demo_snapshot()
    config = DEFAULT_CONFIG
    if case == "no games":
        zero = {k: replace(v, values={**v.values, "0": "0"}) for k, v in snap.stats.items()}
        snap = replace(snap, stats=zero)
    else:
        config = replace(DEFAULT_CONFIG, gp_floor_fraction=1.0)  # nobody above the max GP
    view = build_view(snap, config, Salaries(), season=Season.CURRENT)
    table = league_table(view)
    assert len(table.rows) == len(snap.teams)
    for r in table.rows:
        assert r.standing.roto is None
        assert r.standing.ttltst == EMPTY
        assert all(c == EMPTY for c in r.standing.categories.values())
    assert [r.team.name for r in table.rows] == sorted(r.team.name for r in table.rows)


def test_the_teams_a_gap_is_to_are_named_in_order() -> None:
    view = demo_view(Season.LAST)
    table = league_table(view)
    last = table.rows[-1]
    cell = last.standing.categories[Category.G]
    assert cell.above
    assert table.above(cell) == ", ".join(sorted(table.names[k] for k in cell.above))
    assert table.names == {t.team_key: t.name for t in view.teams}
