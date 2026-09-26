"""Metric engine (SPEC §5): worked examples at every rule and boundary."""

import math
from collections.abc import Mapping

import pytest

from fha.domain.engine import (
    DEFAULT_CONFIG,
    DivisorMethod,
    EngineConfig,
    compute_divisors,
    display_order,
    eligible_skaters,
    gp_floor,
    per82,
    prefers_baseline,
    rate,
)
from fha.domain.models import SKATER_CATEGORIES, Category, PlayerSeason

G, A, PPP, PIM, HIT, SOG, BLK = (
    Category.G,
    Category.A,
    Category.PPP,
    Category.PIM,
    Category.HIT,
    Category.SOG,
    Category.BLK,
)
ONE_CAT = EngineConfig(categories=(G,), divisor_top_n=2)


def sk(
    pid: str,
    gp: int,
    stats: Mapping[Category, float] | None = None,
    *,
    name: str | None = None,
    aav: int | None = None,
    goalie: bool = False,
) -> PlayerSeason:
    full = dict.fromkeys(SKATER_CATEGORIES, 0.0)
    full.update(stats or {})
    return PlayerSeason(
        player_id=pid, name=name or pid, gp=gp, stats=full, aav=aav, is_goalie=goalie
    )


# ---------------------------------------------------------------- models


def test_categories_are_the_seven_skater_categories() -> None:
    assert [c.value for c in SKATER_CATEGORIES] == ["G", "A", "PPP", "PIM", "HIT", "SOG", "BLK"]


@pytest.mark.parametrize(
    ("gp", "stats", "message"),
    [
        (-1, {}, "gp"),
        (10, {G: -1.0}, "negative"),
        (10, {G: math.nan}, "finite"),
        (10, {G: math.inf}, "finite"),
    ],
)
def test_player_season_rejects_bad_numbers(
    gp: int, stats: Mapping[Category, float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        sk("p", gp, stats)


def test_player_season_rejects_negative_aav() -> None:
    with pytest.raises(ValueError, match="aav"):
        sk("p", 10, aav=-1)


def test_player_season_accepts_zero_gp_and_zero_aav() -> None:
    p = sk("p", 0, aav=0)
    assert (p.gp, p.aav) == (0, 0)


# ---------------------------------------------------------------- config


def test_default_config_follows_the_workbook() -> None:
    assert DEFAULT_CONFIG.categories == SKATER_CATEGORIES
    assert DEFAULT_CONFIG.gp_floor_fraction == 0.02
    assert DEFAULT_CONFIG.divisor_method is DivisorMethod.WORKBOOK
    assert DEFAULT_CONFIG.top_n == 20


@pytest.mark.parametrize(
    ("method", "top_n"), [(DivisorMethod.WORKBOOK, 20), (DivisorMethod.TOP_PER82, 10)]
)
def test_top_n_defaults_per_method(method: DivisorMethod, top_n: int) -> None:
    assert EngineConfig(divisor_method=method).top_n == top_n
    assert EngineConfig(divisor_method=method, divisor_top_n=3).top_n == 3


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"categories": ()}, "categories"),
        ({"categories": (G, G)}, "categories"),
        ({"gp_floor_fraction": -0.01}, "gp_floor_fraction"),
        ({"gp_floor_fraction": 1.01}, "gp_floor_fraction"),
        ({"gp_floor_fraction": math.nan}, "gp_floor_fraction"),
        ({"divisor_top_n": 0}, "divisor_top_n"),
    ],
)
def test_config_rejects_bad_values(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        EngineConfig(**kwargs)  # type: ignore[arg-type]


def test_config_coerces_strings_from_json_or_env() -> None:
    config = EngineConfig(categories=("G", "A"), divisor_method="top_per82")  # type: ignore[arg-type]
    assert config.divisor_method is DivisorMethod.TOP_PER82
    assert config.categories == (G, A)
    assert all(type(c) is Category for c in config.categories)
    players = [sk("a", 80, {G: 20}), sk("b", 60, {G: 30}), sk("c", 40, {G: 10})]
    one = EngineConfig(categories=("G",), divisor_method="top_per82", divisor_top_n=2)  # type: ignore[arg-type]
    assert compute_divisors(players, one) == {G: pytest.approx(30.75)}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"divisor_method": "bogus"}, "bogus"),
        ({"categories": ("G", "GAA")}, "GAA"),
        ({"categories": "GA"}, "sequence"),
        ({"divisor_top_n": 2.5}, "divisor_top_n"),
        ({"divisor_top_n": 20.0}, "divisor_top_n"),
        ({"divisor_top_n": True}, "divisor_top_n"),
        ({"gp_floor_fraction": False}, "^gp_floor_fraction must be a number, got a bool$"),
    ],
)
def test_config_rejects_unknown_names(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        EngineConfig(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("fraction", [0.0, 1.0])
def test_config_accepts_fraction_bounds(fraction: float) -> None:
    assert EngineConfig(gp_floor_fraction=fraction).gp_floor_fraction == fraction


def test_config_accepts_top_n_of_one() -> None:
    assert EngineConfig(divisor_top_n=1).top_n == 1


# ---------------------------------------------------------------- from_mapping (stored settings)


def test_from_mapping_of_nothing_is_the_default() -> None:
    assert EngineConfig.from_mapping({}) == DEFAULT_CONFIG


def test_from_mapping_reads_strings_as_stored_or_typed() -> None:
    stored = {
        "divisor_method": "top_per82",
        "divisor_top_n": " 12 ",
        "gp_floor_fraction": "0.05",
        "categories": "G, A,PPP",
    }
    config = EngineConfig.from_mapping(stored)
    assert config == EngineConfig(
        categories=(G, A, Category.PPP),
        gp_floor_fraction=0.05,
        divisor_method=DivisorMethod.TOP_PER82,
        divisor_top_n=12,
    )
    assert type(config.gp_floor_fraction) is float


def test_from_mapping_accepts_native_values() -> None:
    native = {"divisor_top_n": 20, "gp_floor_fraction": 0, "categories": ["G"]}
    config = EngineConfig.from_mapping(native)
    assert (config.divisor_top_n, config.gp_floor_fraction, config.categories) == (20, 0.0, (G,))


@pytest.mark.parametrize("unset", [None, "", "  "])
def test_from_mapping_blank_top_n_means_the_methods_default(unset: object) -> None:
    config = EngineConfig.from_mapping({"divisor_method": "top_per82", "divisor_top_n": unset})
    assert config.divisor_top_n is None
    assert config.top_n == 10


@pytest.mark.parametrize(
    ("stored", "message"),
    [
        ({"divisor_top_n": "2.5"}, "^divisor_top_n must be a whole number, got '2.5'$"),
        ({"divisor_top_n": "twenty"}, "^divisor_top_n must be a whole number, got 'twenty'$"),
        ({"divisor_top_n": "0"}, "divisor_top_n must be >= 1"),
        ({"divisor_top_n": True}, "divisor_top_n must be an integer"),
        ({"gp_floor_fraction": True}, "^gp_floor_fraction must be a number, got a bool$"),
        ({"gp_floor_fraction": "2%"}, "^gp_floor_fraction must be a number, got '2%'$"),
        ({"gp_floor_fraction": ""}, "^gp_floor_fraction must be a number, got ''$"),
        ({"gp_floor_fraction": "nan"}, "gp_floor_fraction must be in"),
        ({"gp_floor_fraction": "1.5"}, "gp_floor_fraction must be in"),
        ({"gp_floor_fraction": [0.1]}, "^gp_floor_fraction must be a number, got \\[0.1\\]$"),
        ({"divisor_method": "Workbook"}, "Workbook"),
        ({"divisor_method": 3}, "3"),
        ({"categories": "G,,A"}, "^categories: empty name in 'G,,A'$"),
        ({"categories": 7}, "^categories must be names or a comma-separated string, got 7$"),
        ({"divisor_count": 20}, "^unknown rating settings: divisor_count$"),
        ({"zeta": 1, "alpha": 2}, "^unknown rating settings: alpha, zeta$"),
    ],
)
def test_from_mapping_rejects_bad_settings(stored: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        EngineConfig.from_mapping(stored)


def test_from_mapping_round_trips_to_mapping() -> None:
    config = EngineConfig(categories=(G, A), gp_floor_fraction=0.1, divisor_top_n=5)
    assert config.to_mapping() == {
        "categories": ["G", "A"],
        "gp_floor_fraction": 0.1,
        "divisor_method": "workbook",
        "divisor_top_n": 5,
    }
    assert EngineConfig.from_mapping(config.to_mapping()) == config
    assert EngineConfig.from_mapping(DEFAULT_CONFIG.to_mapping()) == DEFAULT_CONFIG


# ---------------------------------------------------------------- eligibility


def test_gp_floor_is_fraction_of_max_skater_gp_ignoring_goalies() -> None:
    players = [sk("a", 50), sk("b", 20), sk("g", 82, goalie=True)]
    assert gp_floor(players, DEFAULT_CONFIG) == pytest.approx(1.0)


def test_gp_floor_of_empty_or_goalie_only_pool_is_zero() -> None:
    assert gp_floor([], DEFAULT_CONFIG) == 0
    assert gp_floor([sk("g", 60, goalie=True)], DEFAULT_CONFIG) == 0


def test_eligibility_is_strictly_above_the_floor() -> None:
    # Max 50, floor 0.02 * 50 = 1.0: GP 1 sits on the floor and is out (workbook: E > floor).
    players = [sk("max", 50), sk("on", 1), sk("above", 2), sk("zero", 0)]
    assert [p.player_id for p in eligible_skaters(players, DEFAULT_CONFIG)] == ["max", "above"]


def test_zero_gp_is_never_eligible_even_with_a_zero_floor() -> None:
    config = EngineConfig(gp_floor_fraction=0.0)
    players = [sk("a", 3), sk("zero", 0)]
    assert [p.player_id for p in eligible_skaters(players, config)] == ["a"]


def test_goalies_are_never_eligible() -> None:
    players = [sk("a", 10), sk("g", 10, goalie=True)]
    assert [p.player_id for p in eligible_skaters(players, DEFAULT_CONFIG)] == ["a"]


def test_fraction_one_leaves_only_players_above_max_so_nobody() -> None:
    config = EngineConfig(gp_floor_fraction=1.0)
    assert eligible_skaters([sk("a", 10), sk("b", 5)], config) == []


# ---------------------------------------------------------------- per82 and divisors


def test_per82_scales_totals_to_82_games() -> None:
    assert per82(sk("a", 41, {G: 10}), G) == pytest.approx(20.0)
    assert per82(sk("a", 82, {HIT: 7}), HIT) == pytest.approx(7.0)


def test_per82_of_zero_gp_is_an_error() -> None:
    with pytest.raises(ValueError, match="gp"):
        per82(sk("a", 0), G)


def test_workbook_divisor_is_mean_top_totals_over_mean_top_gp_times_82() -> None:
    # Top-2 goal totals (30, 20) and top-2 GP (80, 60) are taken independently.
    players = [sk("a", 80, {G: 20}), sk("b", 60, {G: 30}), sk("c", 40, {G: 10})]
    config = EngineConfig(categories=(G,), divisor_top_n=2)
    assert compute_divisors(players, config) == {G: pytest.approx(25 / 70 * 82)}


def test_per82_divisor_is_mean_of_top_rates() -> None:
    # Rates: a 20.5, b 41, c 20.5 -> top 2 mean = 30.75.
    players = [sk("a", 80, {G: 20}), sk("b", 60, {G: 30}), sk("c", 40, {G: 10})]
    config = EngineConfig(categories=(G,), divisor_method=DivisorMethod.TOP_PER82, divisor_top_n=2)
    assert compute_divisors(players, config) == {G: pytest.approx((41 + 20.5) / 2)}


@pytest.mark.parametrize("method", list(DivisorMethod))
def test_divisor_uses_all_eligible_when_fewer_than_top_n(method: DivisorMethod) -> None:
    players = [sk("a", 82, {G: 10}), sk("b", 82, {G: 30})]
    config = EngineConfig(categories=(G,), divisor_method=method, divisor_top_n=5)
    assert compute_divisors(players, config) == {G: pytest.approx(20.0)}


@pytest.mark.parametrize("method", list(DivisorMethod))
def test_divisor_ignores_ineligible_and_goalies(method: DivisorMethod) -> None:
    players = [
        sk("a", 82, {G: 10}),
        sk("low", 1, {G: 50}),  # floor 1.64: out
        sk("g", 82, {G: 90}, goalie=True),
    ]
    config = EngineConfig(categories=(G,), divisor_method=method, divisor_top_n=3)
    assert compute_divisors(players, config) == {G: pytest.approx(10.0)}


def test_divisor_is_zero_when_nobody_recorded_the_stat() -> None:
    assert compute_divisors([sk("a", 82), sk("b", 70)], DEFAULT_CONFIG) == dict.fromkeys(
        SKATER_CATEGORIES, 0.0
    )


@pytest.mark.parametrize("method", list(DivisorMethod))
def test_divisors_with_no_eligible_players_are_zero(method: DivisorMethod) -> None:
    config = EngineConfig(divisor_method=method)
    assert compute_divisors([sk("z", 0)], config) == dict.fromkeys(SKATER_CATEGORIES, 0.0)


def test_divisors_cover_only_configured_categories() -> None:
    assert set(compute_divisors([sk("a", 82, {G: 5})], ONE_CAT)) == {G}


def test_missing_category_is_an_error() -> None:
    p = PlayerSeason(player_id="p", name="p", gp=10, stats={A: 1.0})
    with pytest.raises(ValueError, match="G"):
        compute_divisors([p], ONE_CAT)


# ---------------------------------------------------------------- TTLTST


def test_ttltst_is_the_mean_of_per82_over_divisor() -> None:
    # One category per player so the arithmetic is visible.
    config = EngineConfig(categories=(G, A), divisor_top_n=1)
    players = [sk("a", 82, {G: 40, A: 10}), sk("b", 41, {G: 10, A: 20})]
    result = rate(players, config, divisors={G: 40.0, A: 40.0})
    a, b = result.ratings["a"], result.ratings["b"]
    assert a.norms == {G: pytest.approx(1.0), A: pytest.approx(0.25)}
    assert a.ttltst == pytest.approx(0.625)
    assert b.norms == {G: pytest.approx(0.5), A: pytest.approx(1.0)}
    assert b.ttltst == pytest.approx(0.75)


def test_zero_divisor_gives_norm_zero_but_stays_in_the_mean() -> None:
    config = EngineConfig(categories=(G, A))
    result = rate([sk("a", 82, {G: 10})], config)
    assert result.divisors == {G: pytest.approx(10.0), A: 0.0}
    assert result.ratings["a"].norms == {G: pytest.approx(1.0), A: 0.0}
    assert result.ratings["a"].ttltst == pytest.approx(0.5)


def test_computed_divisors_are_used_when_none_injected() -> None:
    players = [sk("a", 82, {G: 10}), sk("b", 82, {G: 30})]
    result = rate(players, ONE_CAT)
    assert result.divisors == {G: pytest.approx(20.0)}
    assert result.ratings["b"].ttltst == pytest.approx(1.5)


def test_injected_divisors_override_computed_ones() -> None:
    result = rate([sk("a", 82, {G: 10})], ONE_CAT, divisors={G: 5.0})
    assert result.divisors == {G: 5.0}
    assert result.ratings["a"].ttltst == pytest.approx(2.0)


@pytest.mark.parametrize("divisor", [0.0, 0.5])
def test_injected_divisors_may_be_zero_or_below_one(divisor: float) -> None:
    result = rate([sk("a", 82, {G: 10})], ONE_CAT, divisors={G: divisor})
    assert result.ratings["a"].ttltst == pytest.approx(20.0 if divisor else 0.0)


def test_injected_divisors_may_carry_extra_categories() -> None:
    result = rate([sk("a", 82, {G: 10})], ONE_CAT, divisors={G: 5.0, A: 1.0})
    assert result.divisors == {G: 5.0}


@pytest.mark.parametrize(
    ("divisors", "message"),
    [
        ({}, "G"),
        ({G: -1.0}, "divisor"),
        ({G: math.nan}, "divisor"),
        ({G: math.inf}, "divisor"),
    ],
)
def test_injected_divisors_are_validated(divisors: dict[Category, float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        rate([sk("a", 82, {G: 10})], ONE_CAT, divisors=divisors)


@pytest.mark.parametrize("goalie", [False, True])
def test_duplicate_player_ids_are_an_error(goalie: bool) -> None:
    with pytest.raises(ValueError, match=r"^duplicate player_id in the pool$"):
        rate([sk("a", 82), sk("a", 70, goalie=goalie)])


# ---------------------------------------------------------------- unrated players


def test_ineligible_players_are_unrated_but_listed() -> None:
    players = [sk("a", 82, {G: 10}, aav=1_000_000), sk("low", 1, {G: 5}, aav=1_000_000)]
    result = rate(players, ONE_CAT)
    low = result.ratings["low"]
    assert (low.ttltst, low.rank, low.percentile, low.value) == (None, None, None, None)
    assert low.norms == {}
    assert low.name == "low"
    assert not low.rated
    assert result.ratings["a"].rated


def test_goalies_are_not_in_the_result() -> None:
    result = rate([sk("a", 82, {G: 1}), sk("g", 82, goalie=True)], ONE_CAT)
    assert set(result.ratings) == {"a"}
    assert result.pool_size == 1


def test_no_eligible_players_is_an_empty_ranking_not_an_error() -> None:
    result = rate([sk("a", 0), sk("b", 0)])
    assert result.eligible_count == 0
    assert result.pool_size == 0
    assert all(not r.rated for r in result.ratings.values())
    assert result.divisors == dict.fromkeys(SKATER_CATEGORIES, 0.0)


def test_empty_pool() -> None:
    result = rate([])
    assert (result.ratings, result.pool_size, result.eligible_count) == ({}, 0, 0)


# ---------------------------------------------------------------- rank and percentile


def test_competition_ranking_shares_ranks_on_ties() -> None:
    players = [
        sk("a", 82, {G: 40}),
        sk("b", 82, {G: 30}),
        sk("c", 82, {G: 30}),
        sk("d", 82, {G: 10}),
    ]
    result = rate(players, ONE_CAT, divisors={G: 10.0})
    assert {pid: r.rank for pid, r in result.ratings.items()} == {"a": 1, "b": 2, "c": 2, "d": 4}


def test_percentile_divides_by_skaters_who_played() -> None:
    # N counts the unrated GP-1 skater too (the workbook ranks all its rows);
    # GP-0 skaters and goalies are not counted.
    players = [
        sk("a", 82, {G: 40}),
        sk("b", 82, {G: 30}),
        sk("c", 82, {G: 20}),
        sk("low", 1),
        sk("zero", 0),
        sk("g", 82, goalie=True),
    ]
    result = rate(players, ONE_CAT, divisors={G: 10.0})
    assert result.pool_size == 4
    assert result.eligible_count == 3
    pct = {pid: r.percentile for pid, r in result.ratings.items()}
    assert pct == {"a": pytest.approx(75.0), "b": pytest.approx(50.0), "c": pytest.approx(25.0)} | {
        "low": None,
        "zero": None,
    }


def test_eligible_player_with_zero_stats_is_rated_zero_and_ranked() -> None:
    result = rate([sk("a", 82, {G: 10}), sk("b", 82)], ONE_CAT)
    b = result.ratings["b"]
    assert (b.ttltst, b.rank, b.percentile) == (0.0, 2, 0.0)


# ---------------------------------------------------------------- value


def test_value_is_aav_millions_per_ttltst() -> None:
    result = rate([sk("a", 82, {G: 10}, aav=5_000_000)], ONE_CAT, divisors={G: 20.0})
    assert result.ratings["a"].value == pytest.approx(10.0, rel=1e-12)


def test_value_of_zero_aav_is_zero() -> None:
    result = rate([sk("a", 82, {G: 10}, aav=0)], ONE_CAT)
    assert result.ratings["a"].value == 0.0


def test_value_is_none_without_aav_or_with_zero_ttltst() -> None:
    players = [sk("noaav", 82, {G: 10}), sk("zero", 82, aav=1_000_000)]
    result = rate(players, ONE_CAT)
    assert result.ratings["noaav"].value is None
    assert result.ratings["zero"].value is None


# ---------------------------------------------------------------- display order


def test_display_order_is_rank_then_name_then_id_with_unrated_last() -> None:
    players = [
        sk("u2", 1, name="Aaron"),
        sk("t2", 82, {G: 10}, name="Bob"),
        sk("t1", 82, {G: 10}, name="Bob"),
        sk("top", 82, {G: 20}, name="Zed"),
        sk("t0", 82, {G: 10}, name="Adam"),
        sk("u1", 0, name="Aaron"),
    ]
    result = rate(players, ONE_CAT)
    assert [r.player_id for r in display_order(result)] == ["top", "t0", "t1", "t2", "u1", "u2"]


def test_ratings_carry_names() -> None:
    result = rate([sk("a", 82, {G: 1}, name="Connor McDavid")], ONE_CAT)
    assert result.ratings["a"].name == "Connor McDavid"


# ---------------------------------------------------------------- baseline season


@pytest.mark.parametrize(("max_gp", "expected"), [(0, True), (9, True), (10, False), (60, False)])
def test_prefers_baseline_below_min_gp(max_gp: int, expected: bool) -> None:
    pool = [sk("a", max_gp), sk("b", 0), sk("g", 82, goalie=True)]
    assert prefers_baseline(pool, baseline_min_gp=10) is expected


@pytest.mark.parametrize("min_gp", [1, 10])
def test_prefers_baseline_for_an_empty_pool(min_gp: int) -> None:
    assert prefers_baseline([], baseline_min_gp=min_gp) is True
