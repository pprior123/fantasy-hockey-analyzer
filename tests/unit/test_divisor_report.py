"""scripts/divisor_report.py on a small synthetic pool."""

import json
from pathlib import Path

import pytest

from fha.domain.engine import DivisorMethod, EngineConfig
from fha.domain.models import SKATER_CATEGORIES, Category, PlayerSeason
from scripts import divisor_report as dr


def sk(pid: str, gp: int, pim: float) -> PlayerSeason:
    stats = dict.fromkeys(SKATER_CATEGORIES, 1.0) | {Category.PIM: pim}
    return PlayerSeason(pid, pid, gp, stats)


# Max GP 80, so low GP is < 16. "brief" has a huge PIM rate on 5 GP.
POOL = [sk("a", 80, 40), sk("b", 70, 60), sk("brief", 5, 20), sk("c", 30, 10)]


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        # By totals: 60, 40 lead; "brief" (20) is third.
        (DivisorMethod.WORKBOOK, 0),
        # By rate: brief 328/82 leads.
        (DivisorMethod.TOP_PER82, 1),
    ],
)
def test_low_gp_contributors(method: DivisorMethod, expected: int) -> None:
    config = EngineConfig(divisor_method=method, divisor_top_n=2)
    assert dr.low_gp_contributors(POOL, Category.PIM, config) == expected


def test_low_gp_contributors_counts_a_brief_player_among_top_totals() -> None:
    config = EngineConfig(divisor_top_n=3)
    assert dr.low_gp_contributors(POOL, Category.PIM, config) == 1


def test_report_table(tmp_path: Path) -> None:
    players = [
        {
            "player_id": p.player_id,
            "name": p.name,
            "gp": p.gp,
            "stats": {c.value: v for c, v in p.stats.items()},
            "aav": None,
        }
        for p in POOL
    ]
    (tmp_path / "golden_players.json").write_text(json.dumps(players), encoding="utf-8")
    static = dict.fromkeys([c.value for c in SKATER_CATEGORIES], 2.0)
    (tmp_path / "golden_divisors.json").write_text(json.dumps(static), encoding="utf-8")

    text = dr.report(*dr.load(tmp_path))
    rows = [line for line in text.splitlines() if line.startswith("| ")][1:]
    assert [row.split(" | ")[0] for row in rows] == [f"| {c}" for c in SKATER_CATEGORIES]
    pim = next(row for row in rows if row.startswith("| PIM"))
    # Workbook method over 4 eligible: mean totals 32.5 / mean GP 46.25 * 82.
    assert f"{32.5 / 46.25 * 82:.4f}" in pim
    assert "| 2.0000 |" in pim
    assert "max GP (80)" in text


def test_rank_shift_of_identical_results_is_zero() -> None:
    wb = dr.rate(POOL, dr.WORKBOOK)
    assert dr.rank_shift(wb, wb, top=2) == (0.0, 2)


def result_in_order(order: list[str]) -> dr.RatingResult:
    # One category; descending G totals set the order.
    base = dict.fromkeys(SKATER_CATEGORIES, 0.0)
    pool = [
        PlayerSeason(pid, pid, 82, base | {Category.G: 10.0 - i}) for i, pid in enumerate(order)
    ]
    return dr.rate(pool, EngineConfig(categories=(Category.G,)))


def test_rank_shift_counts_moves_and_top_overlap() -> None:
    # a,b,c,d -> c,a,b,d: moves 1+1+2+0 = 4 over 4 players; top 2 {a,b} vs {c,a}.
    before, after = result_in_order(["a", "b", "c", "d"]), result_in_order(["c", "a", "b", "d"])
    assert dr.rank_shift(before, after, top=2) == (1.0, 1)


def test_main_prints_the_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fixtures = Path(__file__).parents[1] / "fixtures"
    assert dr.main([str(fixtures)]) == 0
    out = capsys.readouterr().out
    assert "| PIM | 110.9247 | 110.9247 |" in out
