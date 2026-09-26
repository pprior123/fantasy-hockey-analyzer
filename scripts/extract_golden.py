"""Extract the metric engine's golden fixtures from the owner's workbook (SPEC §5).

Reads ``private/2025_2026_stats.xlsx`` twice with openpyxl, once for cached
values and once for formulas, and writes three committed fixtures:

- ``golden_players.json``: per skater row of ``Fantasy Analysis``: id, name,
  team, position, GP, the 7 category totals, workbook TTLTST (``AD``),
  percentile (from ``Table2``) and cap hit (``S``, null when ``#N/A``);
- ``golden_divisors.json``: the category divisors in ``W6:AC6``;
- ``alias_seed.json``: the ``Name Aliases`` sheet.

Row-2 labels are wrong in places, so columns are chosen by what their
formulas pull, and every formula this relies on is checked before any value
is read. A layout change fails loudly instead of producing wrong fixtures.

Only the ranges above are read. The roster tables (``Table4`` and the seven
below it) and the manager comparison are never touched.

Usage: python scripts/extract_golden.py [workbook.xlsx] [out_dir]
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

DEFAULT_WORKBOOK = Path("private/2025_2026_stats.xlsx")
DEFAULT_OUT = Path("tests/fixtures")

ANALYSIS = "Fantasy Analysis"
STATS = "Stats Data Source"
ALIASES = "Name Aliases"
FIRST_ROW = 3
# Each Fantasy Analysis row pulls one Stats Data Source row, named in its A
# formula. Usually the row above, but not always (the workbook's last row pulls
# the source's last row, skipping the rows between), so it is read per row.
SOURCE_ROW = re.compile(r"=_xlfn\.CONCAT\('Stats Data Source'!C(\d+), ")

# Category -> Fantasy Analysis column, with the Stats Data Source column its
# formula must pull (PPP is M+N, i.e. ppGoals + ppAssists) and the divisor
# cell the TTLTST formula divides it by.
CATEGORIES: dict[str, tuple[str, str, str]] = {
    "G": ("F", "S", "W"),  # goals
    "A": ("G", "T", "X"),  # assists
    "PIM": ("J", "W", "Y"),  # pim
    "PPP": ("O", "M+N", "Z"),  # ppGoals + ppAssists
    "HIT": ("P", "AD", "AA"),  # labelled shortHandedGoals; pulls hits
    "BLK": ("Q", "AE", "AB"),  # labelled hits; pulls blocks
    "SOG": ("K", "X", "AC"),  # shots
}
# Other columns read, with the formula each must hold (``#`` = this row,
# ``@`` = the Stats Data Source row it pulls).
EXPECTED_FORMULAS = {
    "A": "=_xlfn.CONCAT('Stats Data Source'!C@, \" \",'Stats Data Source'!D@)",
    "C": (
        "=SUBSTITUTE(RIGHT(LEFT('Stats Data Source'!G@,"
        'SEARCH("</a",\'Stats Data Source\'!G@,1)-1),3), ">", "")'
    ),
    "D": "='Stats Data Source'!H@",
    "E": "='Stats Data Source'!R@",
    "M": "='Stats Data Source'!Z@",
    "N": "='Stats Data Source'!AA@",
    "O": "=M#+N#",
    "S": "=INDEX(Table_2[Column3], R#,1)",
    "AD": (
        "=IF(E#>0.02*MAX($E$3:$E$999), (F#/E#/$W$6+G#/E#/$X$6+J#/E#/$Y$6+O#/E#/$Z$6"
        "+P#/E#/$AA$6+Q#/E#/$AB$6+K#/E#/$AC$6)*82/7,0)"
    ),
}
for _cat, (_col, _src, _div) in CATEGORIES.items():
    if _col not in EXPECTED_FORMULAS:
        EXPECTED_FORMULAS[_col] = f"=_xlfn.NUMBERVALUE('Stats Data Source'!{_src}@)"

# The Stats Data Source headers of the columns pulled above (row 1).
STATS_HEADERS = {
    "A": "id",
    "C": "firstname",
    "D": "lastname",
    "G": "team",
    "H": "position",
    "R": "gp",
    "S": "goals",
    "T": "assists",
    "W": "pim",
    "X": "shots",
    "Z": "ppGoals",
    "AA": "ppAssists",
    "AD": "hits",
    "AE": "blocks",
}

# Divisors: mean of the top 20 totals over mean of the top 20 GP, times 82,
# each over one column from row 3 to at least the last player row.
DIVISOR_ROW = 6
TOP_20 = re.escape("{" + ",".join(str(k) for k in range(1, 21)) + "}")
DIVISOR_FORMULA = re.compile(
    rf"=AVERAGE\(LARGE\((?P<col>[A-Z]+)\$?3:(?P=col)\$?(?P<end>\d+),{TOP_20}\)\)"
    rf"/AVERAGE\(LARGE\(E\$3:E\$(?P<gp_end>\d+), ?{TOP_20}\)\)\*82"
)

# Table2 (the ranking): AE rank 1..n (literals, then =AE{prev}+1; checked by
# value), AF score (LARGE of AD), AG percentile.
RANKING_FORMULAS = {
    "AF": "=LARGE($AD$3:$AD$932,AE#)",
    "AG": "=(1-AE#/MAX(Table2[Ranking]))*100",
}
RANKING_TABLE = "Table2"
RANK_COL, SCORE_COL, PCT_COL = "AE", "AF", "AG"
NA = "#N/A"


class LayoutError(ValueError):
    """The workbook doesn't have the layout this script was written against."""


@dataclass(frozen=True)
class Golden:
    players: list[dict[str, Any]]
    divisors: dict[str, float]
    aliases: list[dict[str, str]]
    unrated_source_players: int  # scraped but never pulled into Fantasy Analysis


def formula_text(value: object) -> str:
    """A cell's formula as text; array formulas carry theirs in ``.text``."""
    text = getattr(value, "text", value)
    return "" if text is None else str(text)


def expected(template: str, row: int, source_row: int) -> str:
    return template.replace("#", str(row)).replace("@", str(source_row))


def source_rows(formulas: Worksheet, rows: range) -> list[int]:
    """The Stats Data Source row each Fantasy Analysis row pulls; each at most once."""
    found = []
    for row in rows:
        match = SOURCE_ROW.match(formula_text(formulas[f"A{row}"].value))
        if not match:
            raise LayoutError(f"{ANALYSIS}!A{row}: can't tell which source row it pulls")
        found.append(int(match[1]))
    if len(set(found)) != len(found):
        raise LayoutError(f"{ANALYSIS}: two rows pull the same source row")
    return found


def data_rows(values: Worksheet) -> range:
    """Rows 3.. up to the first row whose name (column A) is blank."""
    row = FIRST_ROW
    while str(values[f"A{row}"].value or "").strip():
        row += 1
    if row == FIRST_ROW:
        raise LayoutError(f"{ANALYSIS}: no player rows from A{FIRST_ROW}")
    return range(FIRST_ROW, row)


def check_formulas(formulas: Worksheet, rows: range, sources: Sequence[int]) -> None:
    problems: list[str] = []
    for row, source in zip(rows, sources, strict=True):
        for col, template in EXPECTED_FORMULAS.items():
            got = formula_text(formulas[f"{col}{row}"].value)
            if got != expected(template, row, source):
                problems.append(f"{col}{row}: {got!r}")
    for cat, (col, _src, div) in CATEGORIES.items():
        got = formula_text(formulas[f"{div}{DIVISOR_ROW}"].value)
        match = DIVISOR_FORMULA.fullmatch(got)
        last = rows.stop - 1
        if (
            not match
            or match["col"] != col
            or int(match["end"]) < last
            or int(match["gp_end"]) < last
        ):
            problems.append(f"{div}{DIVISOR_ROW} ({cat}): {got!r}")
    for row in rows:
        for col, template in RANKING_FORMULAS.items():
            got = formula_text(formulas[f"{col}{row}"].value)
            if got != expected(template, row, row):
                problems.append(f"{col}{row}: {got!r}")
    if problems:
        raise LayoutError(f"{ANALYSIS}: unexpected formulas: " + "; ".join(problems[:10]))


def number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise LayoutError(f"{where}: expected a number, got {value!r}")
    return value


def integer(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LayoutError(f"{where}: expected an integer, got {value!r}")
    return value


def percentiles(values: Worksheet, rows: range) -> dict[float, float]:
    """Workbook percentile per TTLTST score: that of the score's first ranking row.

    ``Table2`` ranks by position (``LARGE(AD, k)``), so tied scores get
    consecutive ranks and its name lookup repeats the first tied player.
    A score's first row is the rank every player with that score shares; for
    a tied player the name lookup doesn't show, that is the extractor's
    reading, not a value the workbook displays.
    """
    by_score: dict[float, float] = {}
    for row in rows:
        score = number(values[f"{SCORE_COL}{row}"].value, f"{SCORE_COL}{row}")
        pct = number(values[f"{PCT_COL}{row}"].value, f"{PCT_COL}{row}")
        by_score.setdefault(score, pct)
    return by_score


def player_ids(stats: Worksheet, sources: Sequence[int]) -> list[str]:
    for col, header in STATS_HEADERS.items():
        if stats[f"{col}1"].value != header:
            raise LayoutError(
                f"{STATS}!{col}1: expected {header!r}, got {stats[f'{col}1'].value!r}"
            )
    ids = [str(stats[f"A{row}"].value) for row in sources]
    if len(set(ids)) != len(ids):
        raise LayoutError(f"{STATS}: duplicate player ids")
    return ids


def unrated_source_rows(stats: Worksheet, sources: Sequence[int]) -> list[int]:
    """Source rows holding a player that no Fantasy Analysis row pulls."""
    pulled = set(sources)
    return [
        row
        for row in range(2, stats.max_row + 1)
        if stats[f"A{row}"].value is not None and row not in pulled
    ]


def extract_players(values: Workbook, formulas: Workbook) -> tuple[list[dict[str, Any]], int]:
    """The rated rows, and how many source players the workbook never rates."""
    ws = values[ANALYSIS]
    rows = data_rows(ws)
    sources = source_rows(formulas[ANALYSIS], rows)
    check_formulas(formulas[ANALYSIS], rows, sources)
    # Percentile divides by MAX(Table2[Ranking]): the table must end at the last player.
    tables = formulas[ANALYSIS].tables
    want = f"{RANK_COL}{FIRST_ROW - 1}:AL{rows.stop - 1}"
    if RANKING_TABLE not in tables or tables[RANKING_TABLE].ref != want:
        raise LayoutError(f"{ANALYSIS}: {RANKING_TABLE} must span {want}")
    ranks = [ws[f"{RANK_COL}{row}"].value for row in rows]
    if ranks != list(range(1, len(rows) + 1)):
        raise LayoutError(f"{ANALYSIS}: {RANKING_TABLE} must rank 1..{len(rows)}")
    pct_by_score = percentiles(ws, rows)
    players = []
    for pid, row in zip(player_ids(values[STATS], sources), rows, strict=True):
        ttltst = number(ws[f"AD{row}"].value, f"AD{row}")
        aav = ws[f"S{row}"].value
        players.append(
            {
                "player_id": pid,
                "name": str(ws[f"A{row}"].value),
                "team": str(ws[f"C{row}"].value),
                "position": str(ws[f"D{row}"].value),
                "gp": integer(ws[f"E{row}"].value, f"E{row}"),
                "stats": {
                    cat: integer(ws[f"{col}{row}"].value, f"{col}{row}")
                    for cat, (col, _src, _div) in CATEGORIES.items()
                },
                "ttltst": ttltst,
                "percentile": pct_by_score[ttltst],
                "aav": None if aav == NA else integer(aav, f"S{row}"),
            }
        )
    return players, len(unrated_source_rows(values[STATS], sources))


def extract_divisors(values: Workbook) -> dict[str, float]:
    ws = values[ANALYSIS]
    return {
        cat: number(ws[f"{div}{DIVISOR_ROW}"].value, f"{div}{DIVISOR_ROW}")
        for cat, (_col, _src, div) in CATEGORIES.items()
    }


def extract_aliases(values: Workbook) -> list[dict[str, str]]:
    """``Name Aliases``: column A (stats-feed name) -> column B (salary-sheet name)."""
    ws = values[ALIASES]
    if (ws["A1"].value, ws["B1"].value) != ("Name1", "Name2"):
        raise LayoutError(f"{ALIASES}: unexpected header {ws['A1'].value!r}, {ws['B1'].value!r}")
    aliases = []
    for row in range(2, ws.max_row + 1):
        stats_name, salary_name = ws[f"A{row}"].value, ws[f"B{row}"].value
        if stats_name is None and salary_name is None:
            continue
        if not isinstance(stats_name, str) or not isinstance(salary_name, str):
            raise LayoutError(f"{ALIASES}!A{row}: incomplete alias")
        aliases.append({"stats_name": stats_name.strip(), "salary_name": salary_name.strip()})
    return aliases


def extract(values: Workbook, formulas: Workbook) -> Golden:
    players, unrated = extract_players(values, formulas)
    return Golden(players, extract_divisors(values), extract_aliases(values), unrated)


def write(golden: Golden, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, data in [
        ("golden_players.json", golden.players),
        ("golden_divisors.json", golden.divisors),
        ("alias_seed.json", golden.aliases),
    ]:
        path = out_dir / name
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    return written


def load(path: Path) -> Golden:
    values = openpyxl.load_workbook(path, data_only=True, read_only=False)
    formulas = openpyxl.load_workbook(path, data_only=False, read_only=False)
    return extract(values, formulas)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    workbook = Path(args[0]) if args else DEFAULT_WORKBOOK
    out_dir = Path(args[1]) if len(args) > 1 else DEFAULT_OUT
    try:
        golden = load(workbook)
    except (LayoutError, FileNotFoundError, KeyError) as exc:
        sys.stderr.write(f"extract_golden: {exc}\n")
        return 1
    for path in write(golden, out_dir):
        sys.stdout.write(f"wrote {path}\n")
    sys.stdout.write(
        f"{len(golden.players)} players, {len(golden.divisors)} divisors, "
        f"{len(golden.aliases)} aliases; {golden.unrated_source_players} "
        f"{STATS} players are never pulled into {ANALYSIS}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
