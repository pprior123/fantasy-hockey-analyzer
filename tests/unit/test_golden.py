"""Golden tests (SPEC §5): the engine reproduces the owner's 2025-26 workbook.

Fixtures come from ``scripts/extract_golden.py``.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from fha.domain.engine import DEFAULT_CONFIG, RatingResult, rate
from fha.domain.models import SKATER_CATEGORIES, Category, PlayerSeason

FIXTURES = Path(__file__).parents[1] / "fixtures"
TTLTST_TOLERANCE = 1e-3  # SPEC §5
PERCENTILE_TOLERANCE = 0.1  # SPEC §5


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


GOLDEN_PLAYERS: list[dict[str, Any]] = load("golden_players.json")
GOLDEN_DIVISORS = {Category(k): v for k, v in load("golden_divisors.json").items()}
POOL = [
    PlayerSeason(
        player_id=g["player_id"],
        name=g["name"],
        gp=g["gp"],
        stats={Category(k): float(v) for k, v in g["stats"].items()},
        aav=g["aav"],
    )
    for g in GOLDEN_PLAYERS
]


@pytest.fixture(scope="module")
def injected() -> RatingResult:
    return rate(POOL, DEFAULT_CONFIG, divisors=GOLDEN_DIVISORS)


@pytest.fixture(scope="module")
def computed() -> RatingResult:
    return rate(POOL, DEFAULT_CONFIG)


def test_fixtures_are_the_whole_workbook_pool() -> None:
    assert len(GOLDEN_PLAYERS) == 845
    assert set(GOLDEN_DIVISORS) == set(SKATER_CATEGORIES)
    assert {g["position"] for g in GOLDEN_PLAYERS} <= {"C", "LW", "RW", "D"}  # no goalies


def test_engine_rates_exactly_the_players_the_workbook_rates(injected: RatingResult) -> None:
    # The workbook's IF(...) gives 0 to players at or below the GP floor.
    rated = {pid for pid, r in injected.ratings.items() if r.rated}
    workbook_rated = {g["player_id"] for g in GOLDEN_PLAYERS if g["ttltst"] > 0}
    assert rated == workbook_rated
    assert injected.eligible_count == 827
    assert injected.pool_size == 845


def test_parity_ttltst_and_percentile_with_injected_divisors(injected: RatingResult) -> None:
    rated = 0
    for g in GOLDEN_PLAYERS:
        rating = injected.ratings[g["player_id"]]
        if not rating.rated:
            assert g["ttltst"] == 0
            continue
        rated += 1
        assert rating.ttltst == pytest.approx(g["ttltst"], abs=TTLTST_TOLERANCE), g["name"]
        assert rating.percentile == pytest.approx(g["percentile"], abs=PERCENTILE_TOLERANCE), g[
            "name"
        ]
    assert rated == 827


def test_workbook_ties_share_the_first_rank(injected: RatingResult) -> None:
    # Two players tie on TTLTST in the workbook; both get the first rank's percentile.
    by_score: dict[float, list[str]] = {}
    for g in GOLDEN_PLAYERS:
        if g["ttltst"] > 0:
            by_score.setdefault(g["ttltst"], []).append(g["player_id"])
    ties = [ids for ids in by_score.values() if len(ids) > 1]
    assert len(ties) == 1
    ranks = {injected.ratings[pid].rank for pid in ties[0]}
    assert len(ranks) == 1


def test_divisor_recompute_reproduces_the_workbook(computed: RatingResult) -> None:
    # W6:AC6 are live formulas, not stale statics: recomputing them from the
    # same rows with the workbook's method gives the same numbers. The
    # comparison with SPEC's original method is in docs/DECISIONS.md.
    for cat in SKATER_CATEGORIES:
        assert computed.divisors[cat] == pytest.approx(GOLDEN_DIVISORS[cat], rel=1e-12), cat


def test_parity_holds_with_computed_divisors(computed: RatingResult) -> None:
    for g in GOLDEN_PLAYERS:
        rating = computed.ratings[g["player_id"]]
        if rating.rated:
            assert rating.ttltst == pytest.approx(g["ttltst"], abs=TTLTST_TOLERANCE)
            assert rating.percentile == pytest.approx(g["percentile"], abs=PERCENTILE_TOLERANCE)


def test_value_uses_the_workbook_cap_hit(injected: RatingResult) -> None:
    mcdavid = next(g for g in GOLDEN_PLAYERS if g["name"] == "Connor McDavid")
    rating = injected.ratings[mcdavid["player_id"]]
    assert rating.ttltst is not None
    assert rating.rank == 1
    assert rating.value == pytest.approx(15.2869877274356)  # workbook Table2 $/TTSTLT, rank 1
