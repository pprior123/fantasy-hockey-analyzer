"""Divisor recompute report (SPEC §5, golden test 2) as a Markdown table.

Compares the workbook's divisors (``golden_divisors.json``) with divisors
recomputed from ``golden_players.json`` by each ``DivisorMethod``, and counts
how many of each method's contributors have GP below 20% of the pool's max.
The output goes into docs/DECISIONS.md.

Usage: python scripts/divisor_report.py [fixtures_dir]
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fha.domain.engine import (
    DivisorMethod,
    EngineConfig,
    RatingResult,
    eligible_skaters,
    per82,
    rate,
)
from fha.domain.models import SKATER_CATEGORIES, Category, PlayerSeason

LOW_GP_FRACTION = 0.2
WORKBOOK = EngineConfig(divisor_method=DivisorMethod.WORKBOOK)  # top 20
SPEC = EngineConfig(divisor_method=DivisorMethod.TOP_PER82)  # top 10


def load(fixtures: Path) -> tuple[list[PlayerSeason], dict[Category, float]]:
    players: list[dict[str, Any]] = json.loads(
        (fixtures / "golden_players.json").read_text(encoding="utf-8")
    )
    divisors = json.loads((fixtures / "golden_divisors.json").read_text(encoding="utf-8"))
    pool = [
        PlayerSeason(
            player_id=p["player_id"],
            name=p["name"],
            gp=p["gp"],
            stats={Category(k): float(v) for k, v in p["stats"].items()},
            aav=p["aav"],
        )
        for p in players
    ]
    return pool, {Category(k): float(v) for k, v in divisors.items()}


def low_gp_contributors(
    pool: Sequence[PlayerSeason], category: Category, config: EngineConfig
) -> int:
    """Of the players whose totals (WORKBOOK) or rates (TOP_PER82) set the divisor,
    how many have GP below 20% of the pool's max GP."""
    eligible = eligible_skaters(pool, config)
    cutoff = LOW_GP_FRACTION * max(p.gp for p in pool)
    if config.divisor_method is DivisorMethod.WORKBOOK:
        key = [(p.stats[category], p.gp) for p in eligible]
    else:
        key = [(per82(p, category), p.gp) for p in eligible]
    top = sorted(key, reverse=True)[: config.top_n]
    return sum(1 for _value, gp in top if gp < cutoff)


def ranks(result: RatingResult) -> dict[str, int]:
    return {pid: r.rank for pid, r in result.ratings.items() if r.rank is not None}


def rank_shift(a: RatingResult, b: RatingResult, top: int = 50) -> tuple[float, int]:
    """Mean absolute rank change, and how many of a's top ``top`` stay in b's."""
    ra, rb = ranks(a), ranks(b)
    shifts = [abs(ra[pid] - rb[pid]) for pid in ra]
    kept = {pid for pid, r in ra.items() if r <= top} & {pid for pid, r in rb.items() if r <= top}
    return sum(shifts) / len(shifts), len(kept)


def report(pool: Sequence[PlayerSeason], static: dict[Category, float]) -> str:
    workbook, spec = rate(pool, WORKBOOK), rate(pool, SPEC)
    lines = [
        "| Category | Workbook `W6:AC6` | Recomputed, workbook method (top 20) "
        "| Spec method (top-10 per-82) | Spec / workbook "
        "| Low-GP contributors: workbook / spec |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for cat in SKATER_CATEGORIES:
        lines.append(
            f"| {cat} | {static[cat]:.4f} | {workbook.divisors[cat]:.4f} "
            f"| {spec.divisors[cat]:.4f} | {spec.divisors[cat] / static[cat]:.3f} "
            f"| {low_gp_contributors(pool, cat, WORKBOOK)} of 20 "
            f"/ {low_gp_contributors(pool, cat, SPEC)} of 10 |"
        )
    mean_shift, kept = rank_shift(workbook, spec)
    lines += [
        "",
        f"Low GP = below {LOW_GP_FRACTION:.0%} of the pool's max GP "
        f"({max(p.gp for p in pool)}). Switching to the spec method moves a rated "
        f"player {mean_shift:.1f} ranks on average; {kept} of the workbook's top 50 "
        "stay in the top 50.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    fixtures = Path(args[0]) if args else Path("tests/fixtures")
    sys.stdout.write(report(*load(fixtures)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
