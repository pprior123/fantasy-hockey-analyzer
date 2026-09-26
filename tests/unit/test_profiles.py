"""Team profiles, matchup comparison and need_score (SPEC §5)."""

import math
import statistics

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fha.domain.engine import Rating
from fha.domain.models import SKATER_CATEGORIES, Category
from fha.domain.profiles import (
    DEFAULT_PROFILE_CONFIG,
    Matchup,
    Member,
    ProfileConfig,
    TeamProfile,
    compare,
    need_score,
    profile_players,
    team_profile,
)

G, A, PPP = Category.G, Category.A, Category.PPP
CATS = SKATER_CATEGORIES


def rated(pid: str, ttltst: float, **norms: float) -> Rating:
    full = {c: norms.get(c.value, 0.0) for c in CATS}
    return Rating(pid, f"P{pid}", ttltst, 1, 50.0, None, full)


def unrated(pid: str) -> Rating:
    return Rating(pid, f"P{pid}", None, None, None, None, {})


def member(rating: Rating | None, ir: bool = False, pid: str | None = None) -> Member:
    return Member(pid or (rating.player_id if rating else "g"), ir, rating)


# ---------------------------------------------------------------- profile_players


def test_profile_players_are_rated_skaters_outside_ir_slots() -> None:
    a, b, c, d = rated("a", 1.0), rated("b", 0.5), unrated("c"), rated("d", 2.0)
    members = [member(a), member(b), member(c), member(d, ir=True), member(None, pid="goalie")]
    assert profile_players(members) == (a, b)


def test_a_player_listed_twice_is_an_error() -> None:
    a = rated("a", 1.0)
    with pytest.raises(ValueError, match="player a is listed twice"):
        profile_players([member(a), member(a, ir=True)])


# ---------------------------------------------------------------- team_profile


def test_mean_and_population_sd_per_category_and_team_ttltst() -> None:
    members = [
        member(rated("a", 1.0, G=1.0, A=0.2)),
        member(rated("b", 0.6, G=0.5, A=0.4)),
        member(rated("c", 0.8, G=0.0, A=0.9)),
    ]
    profile = team_profile(members)
    assert profile.size == 3
    assert profile.mean_norm[G] == pytest.approx(0.5)
    assert profile.sd_norm[G] == pytest.approx(statistics.pstdev([1.0, 0.5, 0.0]))
    assert profile.sd_norm[G] != pytest.approx(statistics.stdev([1.0, 0.5, 0.0]))  # population
    assert profile.mean_norm[A] == pytest.approx(0.5)
    assert profile.ttltst == pytest.approx(0.8)
    assert set(profile.mean_norm) == set(profile.sd_norm) == set(CATS)


def test_ir_players_and_unrated_players_are_left_out_of_the_profile() -> None:
    members = [
        member(rated("a", 1.0, G=1.0)),
        member(rated("b", 5.0, G=9.0), ir=True),
        member(unrated("c")),
    ]
    profile = team_profile(members)
    assert profile.players == (members[0].rating,)
    assert profile.mean_norm[G] == 1.0
    assert profile.ttltst == 1.0


def test_one_player_has_sd_zero() -> None:
    profile = team_profile([member(rated("a", 0.7, G=0.3))])
    assert profile.sd_norm[G] == 0.0
    assert profile.mean_norm[G] == 0.3


def test_no_profile_players_gives_none_everywhere() -> None:
    profile = team_profile([member(unrated("a")), member(rated("b", 1.0), ir=True)])
    assert profile.size == 0
    assert profile.ttltst is None
    assert all(v is None for v in profile.mean_norm.values())
    assert all(v is None for v in profile.sd_norm.values())
    assert set(profile.mean_norm) == set(CATS)


def test_the_profile_uses_the_configured_categories() -> None:
    config = ProfileConfig(categories=(G, A))
    profile = team_profile([member(rated("a", 1.0, G=1.0, A=2.0))], config)
    assert set(profile.mean_norm) == {G, A}


def test_a_rating_without_a_configured_category_is_an_error() -> None:
    odd = Rating("a", "Pa", 1.0, 1, 50.0, None, {G: 1.0})
    with pytest.raises(ValueError, match="rating for a has no A norm"):
        team_profile([member(odd)], ProfileConfig(categories=(G, A)))


# ---------------------------------------------------------------- compare (matchup)


def test_matchup_differences_are_me_minus_opponent() -> None:
    me = team_profile([member(rated("a", 1.0, G=0.9, A=0.3, PPP=0.5))])
    opp = team_profile([member(rated("b", 1.0, G=0.5, A=0.5, PPP=0.48))])
    result = compare(me, opp)
    assert result.diff[G] == pytest.approx(0.4)
    assert result.diff[A] == pytest.approx(-0.2)
    assert result.diff[PPP] == pytest.approx(0.02)
    # behind (A), close (PPP, 0.02 < 0.05) and level (the zero categories) are trailing
    assert result.trailing == frozenset(CATS) - {G}


def test_trailing_is_strictly_below_the_margin() -> None:
    me = team_profile([member(rated("a", 1.0, G=0.55))])
    opp = team_profile([member(rated("b", 1.0, G=0.5))])
    exactly = compare(me, opp, ProfileConfig(close_margin=0.05))
    assert exactly.diff[G] == pytest.approx(0.05)
    margin = exactly.diff[G]
    assert G not in compare(me, opp, ProfileConfig(close_margin=margin)).trailing
    assert G in compare(me, opp, ProfileConfig(close_margin=math.nextafter(margin, 1))).trailing


def test_a_zero_margin_trails_only_when_behind() -> None:
    me = team_profile([member(rated("a", 1.0, G=0.5, A=0.4))])
    opp = team_profile([member(rated("b", 1.0, G=0.5, A=0.5))])
    assert compare(me, opp, ProfileConfig(close_margin=0.0)).trailing == {A}


@pytest.mark.parametrize("empty_side", ["me", "opp", "both"])
def test_an_empty_team_has_no_differences_and_nothing_trailing(empty_side: str) -> None:
    full = team_profile([member(rated("a", 1.0, G=1.0))])
    empty = team_profile([])
    me = empty if empty_side in ("me", "both") else full
    opp = empty if empty_side in ("opp", "both") else full
    result = compare(me, opp)
    assert result == Matchup(dict.fromkeys(CATS), frozenset())


def test_teams_profiled_on_other_categories_cant_be_compared() -> None:
    me = team_profile([member(rated("a", 1.0))], ProfileConfig(categories=(G,)))
    opp = team_profile([member(rated("b", 1.0))])
    with pytest.raises(ValueError, match="profiled on different categories"):
        compare(me, opp)


# ---------------------------------------------------------------- need_score


def test_need_score_sums_norms_over_trailing_categories() -> None:
    player = rated("x", 1.0, G=0.2, A=0.7, PPP=0.4)
    assert need_score(player, {A, PPP}) == pytest.approx(1.1)
    assert need_score(player, []) == 0.0


def test_need_score_of_an_unrated_player_is_none() -> None:
    assert need_score(unrated("x"), {A}) is None


def test_need_score_needs_the_trailing_categories_norms() -> None:
    odd = Rating("x", "Px", 1.0, 1, 50.0, None, {G: 1.0})
    with pytest.raises(ValueError, match="rating for x has no A norm"):
        need_score(odd, {A})


# ---------------------------------------------------------------- config


@pytest.mark.parametrize("margin", [-0.01, math.nan, math.inf])
def test_close_margin_must_be_finite_and_non_negative(margin: float) -> None:
    with pytest.raises(ValueError, match="close_margin"):
        ProfileConfig(close_margin=margin)


def test_close_margin_rejects_a_bool() -> None:
    with pytest.raises(ValueError, match="close_margin must be a number"):
        ProfileConfig(close_margin=True)


@pytest.mark.parametrize("categories", [(), (G, G)])
def test_categories_must_be_non_empty_and_unique(categories: tuple[Category, ...]) -> None:
    with pytest.raises(ValueError, match="categories must be non-empty and unique"):
        ProfileConfig(categories=categories)


def test_default_config() -> None:
    assert ProfileConfig() == DEFAULT_PROFILE_CONFIG
    assert (ProfileConfig().close_margin, ProfileConfig().categories) == (0.05, CATS)


# ---------------------------------------------------------------- properties

norm = st.floats(min_value=0.0, max_value=5.0, allow_nan=False)
teams = st.lists(st.lists(norm, min_size=len(CATS), max_size=len(CATS)), min_size=1, max_size=8)


def _team(rows: list[list[float]], prefix: str) -> TeamProfile:
    members = []
    for i, values in enumerate(rows):
        norms = dict(zip(CATS, values, strict=True))
        members.append(
            Member(f"{prefix}{i}", False, Rating(f"{prefix}{i}", "n", 1.0, 1, 1.0, None, norms))
        )
    return team_profile(members)


@given(teams, teams)
def test_trailing_is_exactly_the_categories_below_the_margin(
    mine: list[list[float]], theirs: list[list[float]]
) -> None:
    me, opp = _team(mine, "m"), _team(theirs, "o")
    result = compare(me, opp)
    for cat in CATS:
        diff = result.diff[cat]
        assert diff is not None
        assert diff == pytest.approx(
            statistics.fmean(r[CATS.index(cat)] for r in mine)
            - statistics.fmean(r[CATS.index(cat)] for r in theirs)
        )
        assert (cat in result.trailing) is (diff < DEFAULT_PROFILE_CONFIG.close_margin)


@given(teams)
def test_member_order_does_not_change_the_profile(rows: list[list[float]]) -> None:
    forward = _team(rows, "p")
    backward = _team(list(reversed(rows)), "p")
    for cat in CATS:
        assert forward.mean_norm[cat] == pytest.approx(backward.mean_norm[cat])
        assert forward.sd_norm[cat] == pytest.approx(backward.sd_norm[cat], abs=1e-9)


@given(teams)
def test_a_team_against_itself_trails_everywhere(rows: list[list[float]]) -> None:
    team = _team(rows, "s")
    result = compare(team, team)
    assert all(d == 0.0 for d in result.diff.values())
    assert result.trailing == frozenset(CATS)  # level counts as "behind or close"
