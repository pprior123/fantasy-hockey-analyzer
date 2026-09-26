"""The TTLTST metric engine (SPEC §5). Pure: no I/O, no clock.

For one season's pool of players::

    skaters      = players who are not goalies
    floor        = gp_floor_fraction * max(gp over skaters)
    eligible(p)  = p is a skater and p.gp > floor   (so GP 0 is never eligible)
    per82(c, p)  = p.stats[c] / p.gp * 82
    divisor(c)   = see DivisorMethod, over eligible players
    norm(c, p)   = per82(c, p) / divisor(c), or 0 if divisor(c) == 0
    TTLTST(p)    = mean of norm over the categories
    rank(p)      = 1 + #{eligible q : TTLTST(q) > TTLTST(p)}   (ties share a rank)
    percentile   = (1 - rank / N) * 100, N = number of skaters in the pool
    value(p)     = aav / TTLTST / 1e6, None without an AAV or when TTLTST == 0

Ineligible skaters are unrated (all of the above None) but still returned.
Goalies take no part at all and are not returned.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from fha.domain.models import SKATER_CATEGORIES, Category, PlayerSeason

GAMES = 82


class DivisorMethod(StrEnum):
    """How a category's divisor is computed from the eligible players.

    ``WORKBOOK``: mean of the top-N season totals divided by the mean of the
    top-N GP (each ranked independently), times 82. This is what the owner's
    workbook computes (``W6:AC6``, N = 20). Robust to small samples: a player
    with 3 GP never enters the GP mean.

    ``TOP_PER82``: mean of the top-N per-82 rates (SPEC's original wording,
    N = 10). Small samples inflate it (a 2-GP player with 2 goals rates 82).
    """

    WORKBOOK = "workbook"
    TOP_PER82 = "top_per82"


@dataclass(frozen=True)
class EngineConfig:
    categories: tuple[Category, ...] = SKATER_CATEGORIES
    gp_floor_fraction: float = 0.02
    divisor_method: DivisorMethod = DivisorMethod.WORKBOOK
    divisor_top_n: int = 20

    def __post_init__(self) -> None:
        if not self.categories or len(set(self.categories)) != len(self.categories):
            raise ValueError(f"categories must be non-empty and unique, got {self.categories}")
        if not 0 <= self.gp_floor_fraction <= 1:  # also rejects NaN
            raise ValueError(f"gp_floor_fraction must be in [0, 1], got {self.gp_floor_fraction}")
        if self.divisor_top_n < 1:
            raise ValueError(f"divisor_top_n must be >= 1, got {self.divisor_top_n}")


DEFAULT_CONFIG = EngineConfig()


@dataclass(frozen=True)
class Rating:
    """A skater's rating. Unrated (ineligible) skaters have None and no norms."""

    player_id: str
    name: str
    ttltst: float | None
    rank: int | None
    percentile: float | None
    value: float | None
    norms: Mapping[Category, float] = field(default_factory=dict)

    @property
    def rated(self) -> bool:
        return self.ttltst is not None


@dataclass(frozen=True)
class RatingResult:
    divisors: Mapping[Category, float]
    ratings: Mapping[str, Rating]  # every skater in the pool, by player_id
    pool_size: int  # N: skaters in the pool, rated or not
    eligible_count: int


def _skaters(players: Iterable[PlayerSeason]) -> list[PlayerSeason]:
    return [p for p in players if not p.is_goalie]


def gp_floor(players: Iterable[PlayerSeason], config: EngineConfig) -> float:
    """GP a skater must exceed to be rated: a fraction of the pool's max skater GP."""
    return config.gp_floor_fraction * max((p.gp for p in _skaters(players)), default=0)


def eligible_skaters(players: Iterable[PlayerSeason], config: EngineConfig) -> list[PlayerSeason]:
    skaters = _skaters(players)
    floor = gp_floor(skaters, config)  # >= 0, so gp > floor also excludes GP 0
    return [p for p in skaters if p.gp > floor]


def per82(player: PlayerSeason, category: Category) -> float:
    if player.gp <= 0:
        raise ValueError(f"{player.player_id}: per82 needs gp > 0")
    return _total(player, category) / player.gp * GAMES


def _total(player: PlayerSeason, category: Category) -> float:
    try:
        return player.stats[category]
    except KeyError:
        raise ValueError(f"{player.player_id}: no {category} total") from None


def _mean_top(values: Iterable[float], n: int) -> float:
    top = sorted(values, reverse=True)[:n]
    return math.fsum(top) / len(top)


def _divisor(eligible: Sequence[PlayerSeason], category: Category, config: EngineConfig) -> float:
    if not eligible:
        return 0.0
    n = config.divisor_top_n
    if config.divisor_method is DivisorMethod.TOP_PER82:
        return _mean_top((per82(p, category) for p in eligible), n)
    totals = _mean_top((_total(p, category) for p in eligible), n)
    return totals / _mean_top((p.gp for p in eligible), n) * GAMES


def compute_divisors(
    players: Iterable[PlayerSeason], config: EngineConfig = DEFAULT_CONFIG
) -> dict[Category, float]:
    eligible = eligible_skaters(players, config)
    return {cat: _divisor(eligible, cat, config) for cat in config.categories}


def _checked_divisors(
    divisors: Mapping[Category, float], config: EngineConfig
) -> dict[Category, float]:
    checked = {}
    for cat in config.categories:
        if cat not in divisors:
            raise ValueError(f"no divisor for {cat}")
        value = divisors[cat]
        if not (math.isfinite(value) and value >= 0):
            raise ValueError(f"divisor for {cat} must be finite and >= 0, got {value}")
        checked[cat] = value
    return checked


def _unrated(player: PlayerSeason) -> Rating:
    return Rating(player.player_id, player.name, None, None, None, None)


def rate(
    players: Iterable[PlayerSeason],
    config: EngineConfig = DEFAULT_CONFIG,
    divisors: Mapping[Category, float] | None = None,
) -> RatingResult:
    """Rate one season's pool. ``divisors`` overrides the computed ones (golden tests)."""
    skaters = _skaters(players)
    ids = [p.player_id for p in skaters]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate player_id in the pool")
    eligible = eligible_skaters(skaters, config)
    used = (
        compute_divisors(skaters, config)
        if divisors is None
        else _checked_divisors(divisors, config)
    )

    norms = {
        p.player_id: {
            cat: per82(p, cat) / used[cat] if used[cat] else 0.0 for cat in config.categories
        }
        for p in eligible
    }
    scores = {pid: math.fsum(n.values()) / len(config.categories) for pid, n in norms.items()}
    # Competition ranking: a score's rank is its first (1-based) position, descending.
    first_rank: dict[float, int] = {}
    for position, score in enumerate(sorted(scores.values(), reverse=True), start=1):
        first_rank.setdefault(score, position)
    pool_size = len(skaters)

    ratings = {}
    for p in skaters:
        if p.player_id not in scores:
            ratings[p.player_id] = _unrated(p)
            continue
        score = scores[p.player_id]
        rank = first_rank[score]
        ratings[p.player_id] = Rating(
            player_id=p.player_id,
            name=p.name,
            ttltst=score,
            rank=rank,
            percentile=(1 - rank / pool_size) * 100,
            value=None if p.aav is None or score == 0 else p.aav / score / 1_000_000,
            norms=norms[p.player_id],
        )
    return RatingResult(used, ratings, pool_size, len(eligible))


def display_order(result: RatingResult) -> list[Rating]:
    """Rated players by rank, then name, then player_id; unrated players after, by name."""
    return sorted(
        result.ratings.values(),
        key=lambda r: (math.inf if r.rank is None else r.rank, r.name, r.player_id),
    )


def prefers_baseline(current: Iterable[PlayerSeason], baseline_min_gp: int) -> bool:
    """Whether the default view should use last season (SPEC §5, baseline season).

    True while the current season's max skater GP is below ``baseline_min_gp``.
    """
    return max((p.gp for p in _skaters(current)), default=0) < baseline_min_gp
