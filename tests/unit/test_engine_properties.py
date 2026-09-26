"""Property tests for the metric engine (SPEC §9)."""

import random
from collections.abc import Sequence

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fha.domain.engine import (
    DEFAULT_CONFIG,
    DivisorMethod,
    EngineConfig,
    RatingResult,
    eligible_skaters,
    gp_floor,
    per82,
    rate,
)
from fha.domain.models import SKATER_CATEGORIES, Category, PlayerSeason

TOTALS = st.integers(min_value=0, max_value=400).map(float)
METHODS = st.sampled_from(list(DivisorMethod))
CONFIGS = st.builds(
    EngineConfig,
    divisor_method=METHODS,
    divisor_top_n=st.integers(min_value=1, max_value=12),
    gp_floor_fraction=st.sampled_from([0.0, 0.02, 0.1, 0.5]),
)


def player(pid: str, gp: int, totals: Sequence[float], goalie: bool) -> PlayerSeason:
    return PlayerSeason(
        player_id=pid,
        name=f"n{pid}",
        gp=gp,
        stats=dict(zip(SKATER_CATEGORIES, totals, strict=True)),
        is_goalie=goalie,
        aav=1_000_000,
    )


@st.composite
def pools(draw: st.DrawFn, goalies: bool = False, max_size: int = 25) -> list[PlayerSeason]:
    size = draw(st.integers(min_value=0, max_value=max_size))
    return [
        player(
            f"{'g' if goalies else 'p'}{i}",
            draw(st.integers(min_value=0, max_value=82)),
            draw(st.lists(TOTALS, min_size=7, max_size=7)),
            goalies,
        )
        for i in range(size)
    ]


def snapshot(result: RatingResult, ids: Sequence[str] | None = None) -> dict[str, object]:
    keep = result.ratings if ids is None else {i: result.ratings[i] for i in ids}
    return {
        pid: (r.ttltst, r.rank, r.percentile, r.value, dict(r.norms)) for pid, r in keep.items()
    }


@given(pools(), CONFIGS)
def test_zero_gp_and_below_floor_players_are_unrated(
    pool: list[PlayerSeason], config: EngineConfig
) -> None:
    result = rate(pool, config)
    floor = gp_floor(pool, config)
    for p in pool:
        rating = result.ratings[p.player_id]
        if p.gp == 0 or p.gp <= floor:
            assert not rating.rated
            assert rating.ttltst is None
        else:
            assert rating.rated


@given(pools(), CONFIGS, st.randoms(use_true_random=False))
def test_input_order_does_not_change_output(
    pool: list[PlayerSeason], config: EngineConfig, rnd: random.Random
) -> None:
    shuffled = list(pool)
    rnd.shuffle(shuffled)
    first, second = rate(pool, config), rate(shuffled, config)
    assert first.divisors == second.divisors
    assert snapshot(first) == snapshot(second)


@given(pools(), pools(goalies=True), CONFIGS)
def test_goalies_change_no_skater_output(
    skaters: list[PlayerSeason], goalies: list[PlayerSeason], config: EngineConfig
) -> None:
    alone, mixed = rate(skaters, config), rate(skaters + goalies, config)
    assert alone.divisors == mixed.divisors
    assert alone.pool_size == mixed.pool_size
    assert snapshot(alone) == snapshot(mixed)


@given(pools(), CONFIGS)
def test_ttltst_is_the_mean_of_norms_and_norms_are_rate_over_divisor(
    pool: list[PlayerSeason], config: EngineConfig
) -> None:
    result = rate(pool, config)
    for p in eligible_skaters(pool, config):
        rating = result.ratings[p.player_id]
        for cat in config.categories:
            d = result.divisors[cat]
            assert rating.norms[cat] == pytest.approx(per82(p, cat) / d if d else 0.0)
        assert rating.ttltst == pytest.approx(sum(rating.norms.values()) / len(config.categories))


@given(pools(), st.integers(min_value=1, max_value=12))
def test_top_per82_players_have_mean_norm_one(pool: list[PlayerSeason], top_n: int) -> None:
    config = EngineConfig(divisor_method=DivisorMethod.TOP_PER82, divisor_top_n=top_n)
    result = rate(pool, config)
    rated = [r for r in result.ratings.values() if r.rated]
    for cat in config.categories:
        if result.divisors[cat] > 0:
            top = sorted((r.norms[cat] for r in rated), reverse=True)[: min(top_n, len(rated))]
            assert sum(top) / len(top) == pytest.approx(1.0)


@given(pools(), st.integers(min_value=1, max_value=82), st.integers(min_value=1, max_value=12))
def test_workbook_divisor_matches_top_rates_when_gp_is_equal(
    pool: list[PlayerSeason], gp: int, top_n: int
) -> None:
    # With every eligible player on the same GP the two methods coincide,
    # so the top players again average a norm of 1.
    same_gp = [
        PlayerSeason(p.player_id, p.name, gp, p.stats, is_goalie=False, aav=p.aav) for p in pool
    ]
    workbook = rate(same_gp, EngineConfig(divisor_top_n=top_n))
    per_82 = rate(
        same_gp, EngineConfig(divisor_top_n=top_n, divisor_method=DivisorMethod.TOP_PER82)
    )
    for cat in SKATER_CATEGORIES:
        assert workbook.divisors[cat] == pytest.approx(per_82.divisors[cat])


@given(pools(), CONFIGS, st.floats(min_value=0.01, max_value=100.0))
def test_ttltst_is_invariant_to_scaling_all_stats(
    pool: list[PlayerSeason], config: EngineConfig, factor: float
) -> None:
    scaled = [
        PlayerSeason(
            p.player_id,
            p.name,
            p.gp,
            {c: v * factor for c, v in p.stats.items()},
            is_goalie=p.is_goalie,
            aav=p.aav,
        )
        for p in pool
    ]
    base, big = rate(pool, config), rate(scaled, config)
    for pid, rating in base.ratings.items():
        if rating.ttltst is None:
            assert big.ratings[pid].ttltst is None
        else:
            assert big.ratings[pid].ttltst == pytest.approx(rating.ttltst)


@given(pools(), CONFIGS)
def test_ranks_are_competition_ranks_and_percentiles_follow(
    pool: list[PlayerSeason], config: EngineConfig
) -> None:
    result = rate(pool, config)
    scores = [r.ttltst for r in result.ratings.values() if r.ttltst is not None]
    assert result.eligible_count == len(scores)
    assert result.pool_size == len(pool)
    for rating in result.ratings.values():
        if rating.ttltst is None:
            continue
        assert rating.rank == 1 + sum(1 for s in scores if s > rating.ttltst)
        assert rating.percentile == pytest.approx((1 - rating.rank / len(pool)) * 100)
        assert 0 <= rating.percentile < 100


@given(pools(), st.integers(min_value=1, max_value=5), CONFIGS)
def test_zero_gp_skaters_change_no_rating_or_rank(
    pool: list[PlayerSeason], extra: int, config: EngineConfig
) -> None:
    # They do count in N (percentile), as in the workbook.
    zeros = [player(f"z{i}", 0, [5.0] * 7, False) for i in range(extra)]
    base, padded = rate(pool, config), rate(pool + zeros, config)
    assert base.divisors == padded.divisors
    for pid, rating in base.ratings.items():
        assert (padded.ratings[pid].ttltst, padded.ratings[pid].rank) == (
            rating.ttltst,
            rating.rank,
        )
    assert padded.pool_size == base.pool_size + extra


@given(pools(), CONFIGS)
def test_divisors_are_non_negative_and_cover_the_categories(
    pool: list[PlayerSeason], config: EngineConfig
) -> None:
    divisors = rate(pool, config).divisors
    assert set(divisors) == set(config.categories)
    assert all(d >= 0 for d in divisors.values())


def test_default_config_is_used_when_none_given() -> None:
    pool = [player("a", 82, [10.0] * 7, False), player("b", 1, [1.0] * 7, False)]
    assert snapshot(rate(pool)) == snapshot(rate(pool, DEFAULT_CONFIG))


def test_categories_type() -> None:
    assert all(isinstance(c, Category) for c in SKATER_CATEGORIES)
