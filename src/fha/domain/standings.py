"""League standings from the team profiles (SPEC §5, §7.3). Pure.

For each team and each column (the profiles' categories, and TTLTST):
- the **rank** among the teams with a value: competition ranking, higher is
  better, ties share the best rank (1, 2, 2, 4);
- the **Pctl** on the Players scale: where the team's value would sit among the
  league's rated players, by the engine's own formula for a player
  (``1 + #players above``, over the pool's skaters with GP > 0);
- **roto points** (categories only): n teams with a value, rank 1 scores n,
  tied teams share the points of the places they span; the roto total is the
  sum, None if any category has no value;
- the **gap to tie** the next distinct value up, and the teams there; for a
  category also in per-82 units (the gap times the category's divisor);
- a **heat band**, 0 (best) to 4 (worst), from the rank's position among the
  teams with a value; None with no value or a single team.

These are rates per 82 games, a guide, not the league's real standings.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from fha.domain.engine import Rating
from fha.domain.models import Category
from fha.domain.profiles import TeamProfile

HEAT_BANDS = 5


@dataclass(frozen=True)
class Cell:
    value: float | None
    rank: int | None
    pctl: float | None
    points: float | None  # roto points; None for TTLTST
    gap: float | None  # to tie the next distinct value up; None for the best value
    gap_per82: float | None  # a category's gap as a per-82 rate; None for TTLTST
    above: tuple[str, ...]  # the team keys at that value, sorted
    heat: int | None  # 0 (best) .. HEAT_BANDS - 1 (worst)


EMPTY = Cell(None, None, None, None, None, None, (), None)


@dataclass(frozen=True)
class TeamStanding:
    categories: Mapping[Category, Cell]
    ttltst: Cell
    roto: float | None


def standings(
    teams: Mapping[str, TeamProfile],
    players: Sequence[Rating],
    pool_size: int,
    divisors: Mapping[Category, float],
) -> dict[str, TeamStanding]:
    """Every team's standing, by team key. ``players``: the season's ratings (unrated
    ones are skipped); ``pool_size``: the engine's (skaters with GP > 0)."""
    if not teams:
        return {}
    kinds = {p.categories for p in teams.values()}
    if len(kinds) != 1:
        raise ValueError("every team profile must have the same categories")
    (categories,) = kinds
    rated = [p for p in players if p.ttltst is not None]
    by_cat = {
        cat: _column(
            {k: p.mean_norm.get(cat) for k, p in teams.items()},
            [n for p in rated if (n := p.norms.get(cat)) is not None],
            pool_size,
            divisor=divisors.get(cat),
            roto=True,
        )
        for cat in categories
    }
    by_ttltst = _column(
        {k: p.ttltst for k, p in teams.items()},
        [p.ttltst for p in rated if p.ttltst is not None],
        pool_size,
        divisor=None,
        roto=False,
    )
    out = {}
    for key in teams:
        cells = {cat: by_cat[cat][key] for cat in categories}
        points = [c.points for c in cells.values()]
        roto = None if None in points else sum(p for p in points if p is not None)
        out[key] = TeamStanding(cells, by_ttltst[key], roto)
    return out


def _column(
    values: Mapping[str, float | None],
    scores: Iterable[float],
    pool_size: int,
    *,
    divisor: float | None,
    roto: bool,
) -> dict[str, Cell]:
    valued = {k: v for k, v in values.items() if v is not None}
    n = len(valued)
    distinct = sorted(set(valued.values()), reverse=True)
    rank_of = _competition_ranks(valued.values())
    pctl = _pctl(list(scores), pool_size)
    out: dict[str, Cell] = {}
    for key, value in values.items():
        if value is None:
            out[key] = EMPTY
            continue
        rank = rank_of[value]
        ties = sum(1 for v in valued.values() if v == value)
        position = distinct.index(value)
        above_value = distinct[position - 1] if position > 0 else None
        gap = None if above_value is None else above_value - value
        out[key] = Cell(
            value=value,
            rank=rank,
            pctl=pctl(value),
            points=(n - rank + 1 - (ties - 1) / 2) if roto else None,
            gap=gap,
            gap_per82=None if gap is None or divisor is None else gap * divisor,
            above=tuple(sorted(k for k, v in valued.items() if v == above_value)),
            heat=None if n == 1 else min((rank - 1) * HEAT_BANDS // (n - 1), HEAT_BANDS - 1),
        )
    return out


def _competition_ranks(values: Iterable[float]) -> dict[float, int]:
    """A value's rank is its first (1-based) position, highest first."""
    first: dict[float, int] = {}
    for position, value in enumerate(sorted(values, reverse=True), start=1):
        first.setdefault(value, position)
    return first


def _pctl(scores: Sequence[float], pool_size: int) -> Callable[[float], float | None]:
    """The engine's percentile for a value among the players' scores (SPEC §5)."""

    def pctl(value: float) -> float | None:
        if pool_size <= 0:
            return None
        above = sum(1 for s in scores if s > value)
        # A team's mean can't sit below all its own players, but a float mean can
        # land an ulp under the smallest: never below 0.
        return max(0.0, (1 - (1 + above) / pool_size) * 100)

    return pctl
