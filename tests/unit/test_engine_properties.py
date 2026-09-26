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
from fha.domain.models import SKATER_CATEGORIES, PlayerSeason

# Small value sets mixed in so ties and GP exactly on the floor come up often.
TOTALS = st.one_of(st.sampled_from([0.0, 1.0, 2.0]), st.integers(0, 400).map(float))
GPS = st.one_of(st.sampled_from([0, 1, 2, 4, 50, 82]), st.integers(0, 82))
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
    prefix = "g" if goalies else "p"
    pool = [
        player(f"{prefix}{i}", draw(GPS), draw(st.lists(TOTALS, min_size=7, max_size=7)), goalies)
        for i in range(size)
    ]
    # Clones under new ids: exact ties in every category.
    for i in draw(st.lists(st.integers(0, size - 1), max_size=3)) if size else []:
        p = pool[i]
        pool.append(player(f"{prefix}c{len(pool)}", p.gp, list(p.stats.values()), goalies))
    return pool


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


@given(pools(), st.integers(min_value=1, max_value=41), st.sampled_from([0.25, 0.5]))
def test_gp_exactly_on_the_floor_is_unrated(
    pool: list[PlayerSeason], half_max: int, fraction: float
) -> None:
    # Max GP 4 * half_max makes the floor a whole number for both fractions.
    top_gp = 4 * half_max
    on_floor = int(fraction * top_gp)
    config = EngineConfig(gp_floor_fraction=fraction)
    capped = [
        PlayerSeason(p.player_id, p.name, min(p.gp, top_gp), p.stats, aav=p.aav) for p in pool
    ]
    extra = [player("max", top_gp, [1.0] * 7, False), player("on", on_floor, [9.0] * 7, False)]
    extra.append(player("above", on_floor + 1, [9.0] * 7, False))
    result = rate(capped + extra, config)
    assert gp_floor(capped + extra, config) == on_floor
    assert not result.ratings["on"].rated
    assert result.ratings["above"].rated


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
    assert result.pool_size == sum(1 for p in pool if p.gp > 0)
    for rating in result.ratings.values():
        if rating.ttltst is None:
            continue
        assert rating.rank == 1 + sum(1 for s in scores if s > rating.ttltst)
        assert rating.percentile == pytest.approx((1 - rating.rank / result.pool_size) * 100)
        assert 0 <= rating.percentile < 100


@given(pools(), st.integers(min_value=1, max_value=5), CONFIGS)
def test_zero_gp_skaters_change_no_skater_output(
    pool: list[PlayerSeason], extra: int, config: EngineConfig
) -> None:
    # IR players, prospects and pre-season rows can't shift anyone's percentile.
    zeros = [player(f"z{i}", 0, [5.0] * 7, False) for i in range(extra)]
    base, padded = rate(pool, config), rate(pool + zeros, config)
    assert base.divisors == padded.divisors
    assert base.pool_size == padded.pool_size
    assert snapshot(base) == snapshot(padded, list(base.ratings))
    assert all(not padded.ratings[z.player_id].rated for z in zeros)


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
