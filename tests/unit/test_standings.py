"""League standings from team profiles (plans/league-standings.md §1): ranks, Pctl on
the players' scale, roto points, gaps to tie, heat bands."""

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fha.domain.engine import Rating, rate
from fha.domain.models import SKATER_CATEGORIES, Category, PlayerSeason
from fha.domain.profiles import TeamProfile
from fha.domain.standings import Cell, standings
from tests.unit.test_engine_properties import pools

G, A, HIT = Category.G, Category.A, Category.HIT
CATS = (G, A)  # a subset: nothing assumes the 7
DIVISORS = {G: 30.0, A: 40.0, HIT: 100.0}


def team(ttltst: float | None, cats: tuple[Category, ...] = CATS, **means: float) -> TeamProfile:
    values = {c: means.get(c.value) for c in cats}
    return TeamProfile((), values, dict.fromkeys(cats), ttltst, cats)


def player(pid: str, ttltst: float | None, **norms: float) -> Rating:
    full = {c: norms.get(c.value, 0.0) for c in SKATER_CATEGORIES} if ttltst is not None else {}
    return Rating(pid, f"P{pid}", ttltst, None, None, None, full)


# ---------------------------------------------------------------- ranks and roto points


def test_ranks_are_competition_ranks_with_ties_sharing_the_best() -> None:
    teams = {
        "a": team(1.0, G=3.0, A=1.0),
        "b": team(1.0, G=2.0, A=1.0),
        "c": team(1.0, G=2.0, A=1.0),
        "d": team(1.0, G=1.0, A=1.0),
    }
    table = standings(teams, [], 0, DIVISORS)
    assert [table[k].categories[G].rank for k in "abcd"] == [1, 2, 2, 4]
    assert [table[k].categories[G].value for k in "abcd"] == [3.0, 2.0, 2.0, 1.0]
    assert table["a"].ttltst.value == 1.0
    assert [table[k].categories[A].rank for k in "abcd"] == [1, 1, 1, 1]  # all tied
    assert [table[k].ttltst.rank for k in "abcd"] == [1, 1, 1, 1]


def test_roto_points_share_the_places_tied_teams_span() -> None:
    teams = {
        "a": team(1.0, G=3.0, A=0.5),
        "b": team(1.0, G=2.0, A=0.9),
        "c": team(1.0, G=2.0, A=0.1),
        "d": team(1.0, G=1.0, A=0.1),
    }
    table = standings(teams, [], 0, DIVISORS)
    assert [table[k].categories[G].points for k in "abcd"] == [4, 2.5, 2.5, 1]
    assert [table[k].categories[A].points for k in "abcd"] == [3, 4, 1.5, 1.5]
    assert [table[k].roto for k in "abcd"] == [7, 6.5, 4, 2.5]
    assert table["a"].ttltst.points is None  # TTLTST is not a roto category


def test_a_team_with_no_value_has_no_rank_points_or_roto_and_is_not_counted() -> None:
    teams = {"a": team(2.0, G=3.0, A=1.0), "b": team(1.0, G=2.0, A=2.0), "x": team(None)}
    table = standings(teams, [], 0, DIVISORS)
    empty = table["x"]
    assert empty.categories[G] == Cell(None, None, None, None, None, None, (), None)
    assert empty.ttltst == Cell(None, None, None, None, None, None, (), None)
    assert empty.roto is None
    assert [table[k].categories[G].points for k in "ab"] == [2, 1]  # n = 2, not 3


def test_roto_is_none_when_any_category_has_no_value() -> None:
    teams = {"a": team(1.0, G=3.0), "b": team(1.0, G=2.0, A=1.0)}  # a has no A
    table = standings(teams, [], 0, DIVISORS)
    assert table["a"].roto is None
    assert table["a"].categories[G].points == 2
    assert table["b"].roto == 1 + 1  # last in G of 2, alone (first) in A of 1


def test_the_categories_come_from_the_profiles() -> None:
    teams = {"a": team(1.0, (HIT,), HIT=2.0), "b": team(0.5, (HIT,), HIT=1.0)}
    table = standings(teams, [], 0, DIVISORS)
    assert list(table["a"].categories) == [HIT]
    assert table["a"].roto == 2
    assert table["b"].categories[HIT].gap_per82 == pytest.approx(100.0)


def test_profiles_with_different_categories_are_refused() -> None:
    with pytest.raises(ValueError, match=r"^every team profile must have the same categories$"):
        standings({"a": team(1.0, (G,)), "b": team(1.0, (A,))}, [], 0, DIVISORS)


def test_no_teams_is_no_standings() -> None:
    assert standings({}, [], 0, DIVISORS) == {}


# ---------------------------------------------------------------- gaps


def test_the_gap_is_to_tie_the_next_distinct_value_up() -> None:
    """10, 8, 8, 5: both 8s are 2 short of the 10; the 5 is 3 short of the 8s (both
    named); the leader has no gap."""
    teams = {
        "lead": team(10.0, G=10.0, A=1.0),
        "b": team(8.0, G=8.0, A=1.0),
        "c": team(8.0, G=8.0, A=1.0),
        "d": team(5.0, G=5.0, A=1.0),
    }
    table = standings(teams, [], 0, DIVISORS)
    cells = {k: table[k].categories[G] for k in teams}
    assert cells["lead"].gap is None
    assert cells["lead"].above == ()
    assert (cells["b"].gap, cells["b"].above) == (2.0, ("lead",))
    assert (cells["c"].gap, cells["c"].above) == (2.0, ("lead",))
    assert (cells["d"].gap, cells["d"].above) == (3.0, ("b", "c"))
    assert cells["d"].gap_per82 == pytest.approx(3.0 * DIVISORS[G])
    assert table["d"].ttltst.gap == 3.0
    assert table["d"].ttltst.gap_per82 is None  # TTLTST stays in TTLTST units
    assert table["b"].categories[A].gap is None  # tied leaders: nobody above


def test_a_tiny_positive_gap_stays_positive() -> None:
    teams = {"a": team(1.0, G=0.5 + 1e-12, A=1.0), "b": team(1.0, G=0.5, A=1.0)}
    gap = standings(teams, [], 0, DIVISORS)["b"].categories[G].gap
    assert gap is not None
    assert gap > 0


# ---------------------------------------------------------------- pctl


def test_pctl_places_the_team_value_among_the_rated_players() -> None:
    players = [
        player("1", 5.0, G=5.0),
        player("2", 4.0, G=4.0),
        player("3", 3.0, G=3.0),
        player("4", 2.0, G=2.0),
        player("u", None),  # unrated: not ranked against
    ]
    teams = {"a": team(3.5, G=2.5, A=0.0)}
    cell = standings(teams, players, 10, DIVISORS)["a"]
    assert cell.ttltst.pctl == pytest.approx((1 - 3 / 10) * 100)  # 2 players above
    assert cell.categories[G].pctl == pytest.approx((1 - 4 / 10) * 100)  # 3 above
    assert cell.categories[A].pctl == pytest.approx((1 - 1 / 10) * 100)  # nobody above 0


def test_pctl_is_none_without_a_pool() -> None:
    cell = standings({"a": team(1.0, G=1.0, A=1.0)}, [], 0, DIVISORS)["a"]
    assert cell.ttltst.pctl is None
    assert cell.categories[G].pctl is None
    assert cell.ttltst.rank == 1


def test_pctl_is_never_below_zero() -> None:
    """A team mean can't sit below every rated player (they are its players), but a
    float mean may land an ulp under its smallest member."""
    players = [player("1", 2.0), player("2", 1.0)]
    cell = standings({"a": team(0.5, G=1.0, A=1.0)}, players, 2, DIVISORS)["a"]
    assert cell.ttltst.pctl == 0.0


@given(pools())
def test_a_team_valued_as_a_player_gets_that_players_engine_percentile(
    pool: list[PlayerSeason],
) -> None:
    result = rate(pool)
    players = list(result.ratings.values())
    for r in players:
        if r.ttltst is None:
            continue
        table = standings({"t": team(r.ttltst, G=0.0, A=0.0)}, players, result.pool_size, {})
        assert table["t"].ttltst.pctl == r.percentile


# ---------------------------------------------------------------- heat bands


def test_heat_bands_run_from_the_best_rank_to_the_worst_in_five_steps() -> None:
    values = [9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0]
    teams = {str(i): team(v, G=v, A=1.0) for i, v in enumerate(values)}
    table = standings(teams, [], 0, DIVISORS)
    # (rank - 1) / 7 over 5 bands: 0, .14, .29, .43, .57, .71, .86, 1
    assert [table[str(i)].categories[G].heat for i in range(8)] == [0, 0, 1, 2, 2, 3, 4, 4]
    assert {table[k].categories[A].heat for k in teams} == {0}  # all tied for first


def test_heat_is_neutral_for_one_team_or_no_value() -> None:
    table = standings({"a": team(1.0, G=1.0, A=1.0), "x": team(None)}, [], 0, DIVISORS)
    assert table["a"].categories[G].heat is None  # n = 1
    assert table["x"].categories[G].heat is None


def test_two_teams_are_the_two_ends() -> None:
    table = standings({"a": team(2.0, G=2.0, A=1.0), "b": team(1.0, G=1.0, A=1.0)}, [], 0, {})
    assert [table[k].categories[G].heat for k in "ab"] == [0, 4]


# ---------------------------------------------------------------- properties

VALUE = st.one_of(st.none(), st.sampled_from([0.0, 0.25, 0.5, 1.0, 1.5]))  # ties likely


@st.composite
def leagues(draw: st.DrawFn) -> dict[str, TeamProfile]:
    n = draw(st.integers(1, 10))
    out = {}
    for i in range(n):
        g, a = draw(VALUE), draw(VALUE)
        means = {k: v for k, v in (("G", g), ("A", a)) if v is not None}
        out[f"t{i}"] = team(draw(VALUE), **means)
    return out


@given(leagues())
def test_each_categorys_points_sum_to_n_n_plus_1_over_2(teams: dict[str, TeamProfile]) -> None:
    table = standings(teams, [], 0, DIVISORS)
    for cat in CATS:
        points = [table[k].categories[cat].points for k in teams]
        valued = [p for p in points if p is not None]
        n = len(valued)
        assert math.fsum(valued) == pytest.approx(n * (n + 1) / 2)
        ranks = [table[k].categories[cat].rank for k in teams]
        assert all(1 <= r <= n for r in ranks if r is not None)
        assert sum(r is not None for r in ranks) == n


@given(
    st.lists(st.floats(-3, 3, allow_nan=False), max_size=20),
    st.integers(0, 30),
    st.floats(-4, 4, allow_nan=False),
    st.floats(-4, 4, allow_nan=False),
)
def test_pctl_is_in_range_and_never_falls_as_the_value_rises(
    scores: list[float], extra: int, low: float, high: float
) -> None:
    players = [player(str(i), s) for i, s in enumerate(scores)]
    pool = len(players) + extra
    low, high = min(low, high), max(low, high)
    lo = standings({"t": team(low, G=0.0, A=0.0)}, players, pool, {})["t"].ttltst.pctl
    hi = standings({"t": team(high, G=0.0, A=0.0)}, players, pool, {})["t"].ttltst.pctl
    if pool == 0:
        assert lo is None
        assert hi is None
        return
    assert lo is not None
    assert hi is not None
    assert 0 <= lo <= hi < 100
