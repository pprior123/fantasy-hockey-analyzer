"""Grid → ``ParsedSheet``, keyed off each tab's own PAYROLL formula (SPEC §4a).

For each tab with a ``PAYROLL`` label in its first rows:

1. The PAYROLL cell's formula, followed through at most two cell references
   (``=SUM(C36)`` → ``C36 = SUM(F6:F31)``; ``=C40`` → ``sum(F7:F34)``), must
   name one single-column range. That column holds the salaries, and the
   range's rows are the counted players. Anything else makes the tab
   *unrecognized*, with the reason. The parser never guesses.
2. The header row is the range's first row, or one of the three rows above
   it: the first of those with a name header (``NAME`` / ``Player``). The
   position and team columns come from the same row.
3. Rows below the range whose column A is exactly ``IR`` or ``IR+`` and that
   have a name are IR rows. Column A is the only cell read in any other row
   below the range: contact details sit there and must never be read into a
   result (SPEC §4a).
4. The cap is the summary-tab cell that the team tabs' ``CAP`` formulas
   reference.

Error reasons name cell addresses and the PAYROLL formula itself, never
another cell's contents.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from fha.sources.league_sheet.grid import Cell, Grid, Tab, a1, column_letters, parse_a1
from fha.sources.league_sheet.models import IRLabel, ParsedSheet, ParsedTab, SheetRow

LABEL_ROWS = 10  # the PAYROLL and CAP labels sit in a tab's first rows
LABEL_REACH = 3  # the formula is at most this many columns right of its label
MAX_REFERENCES = 2  # PAYROLL may point at another cell, which may point at one more
HEADER_ROWS_ABOVE = 3

REF = r"\$?[A-Za-z]{1,3}\$?\d+"
SUM_OF_RANGE = re.compile(rf"=\s*SUM\(\s*({REF})\s*:\s*({REF})\s*\)\s*", re.IGNORECASE)
ONE_REFERENCE = re.compile(rf"=\s*(?:SUM\(\s*({REF})\s*\)|({REF}))\s*", re.IGNORECASE)
# 'Quoted title' (a quote inside is doubled, as Sheets writes it) or a bare title.
OTHER_TAB_REFERENCE = re.compile(rf"=\s*(?:'((?:[^']|'')+)'|([^'!=\s]+))!\s*({REF})\s*")

NAME_HEADERS = frozenset({"name", "names", "player", "players", "player name"})
POSITION_HEADERS = frozenset({"position", "positions", "pos"})
TEAM_HEADERS = frozenset({"nhl team", "team", "nhl"})
IR_LABELS: dict[str, IRLabel] = {"IR": "IR", "IR+": "IR+"}


class UnrecognizedTabError(Exception):
    """This tab can't be read without guessing (the message is the reason)."""


@dataclass(frozen=True)
class _Columns:
    header_row: int
    name: int
    position: int | None
    team: int | None


def parse_sheet(grid: Grid) -> ParsedSheet:
    tabs: list[ParsedTab] = []
    others: list[str] = []
    cap_refs: Counter[tuple[str, str]] = Counter()
    for tab in grid:
        payroll_label = _label(tab, "payroll")
        if payroll_label is None:
            others.append(tab.title)
            continue
        cap_cell = _formula_cell(tab, _label(tab, "cap"))
        if cap_cell is not None and cap_cell.formula:
            m = OTHER_TAB_REFERENCE.fullmatch(cap_cell.formula)
            if m:
                title = m[1].replace("''", "'") if m[1] else m[2]
                cap_refs[(title, m[3].replace("$", "").upper())] += 1
        tabs.append(_parse_tab(tab, payroll_label, _whole(cap_cell)))
    cap, source = _summary_cap(grid, cap_refs)
    return ParsedSheet(cap=cap, cap_source=source, tabs=tuple(tabs), other_tabs=tuple(others))


def _parse_tab(tab: Tab, payroll_label: tuple[int, int], cap: int | None) -> ParsedTab:
    cell = _formula_cell(tab, payroll_label)
    formula = cell.formula if cell is not None else None
    try:
        if cell is None or not formula:
            raise UnrecognizedTabError(
                f"the PAYROLL label at {a1(*payroll_label)} has no formula beside it"
            )
        start, end = _resolve(tab, formula)
        (r1, c1), (r2, c2) = sorted((parse_a1(start), parse_a1(end)))
        if c1 != c2:
            raise UnrecognizedTabError(
                f"PAYROLL sums {start}:{end}, which spans more than one column"
            )
        payroll = _whole(cell)
        if payroll is None:
            raise UnrecognizedTabError("PAYROLL has no computed value (was the file recalculated?)")
        columns = _header(tab, r1, r2)
        first = columns.header_row + 1 if columns.header_row == r1 else r1
        rows = [*_counted_rows(tab, columns, c1, first, r2), *_ir_rows(tab, columns, c1, r2)]
    except UnrecognizedTabError as e:
        return ParsedTab(
            tab.title, "unrecognized", str(e), None, cap, None, payroll_formula=formula
        )
    return ParsedTab(
        name=tab.title,
        status="ok",
        reason=None,
        payroll=payroll,
        cap=cap,
        salary_column=column_letters(c1),
        rows=tuple(rows),
        payroll_formula=formula,
        payroll_range=f"{a1(r1, c1)}:{a1(r2, c2)}",
    )


def _resolve(tab: Tab, formula: str, references: int = 0) -> tuple[str, str]:
    """The (start, end) of the one range the formula sums, following references."""
    if m := SUM_OF_RANGE.fullmatch(formula):
        return m[1].replace("$", "").upper(), m[2].replace("$", "").upper()
    if m := ONE_REFERENCE.fullmatch(formula):
        ref = (m[1] or m[2]).replace("$", "").upper()
        if references == MAX_REFERENCES:
            raise UnrecognizedTabError(
                f"PAYROLL follows more than {MAX_REFERENCES} cell references"
            )
        target = tab.at(ref).formula
        if not target:
            raise UnrecognizedTabError(f"PAYROLL leads to {ref}, which has no formula")
        return _resolve(tab, target, references + 1)
    if references:
        raise UnrecognizedTabError("PAYROLL leads to a formula that is not a SUM of one range")
    # The formula as written, quoted with "" (not repr: its \' escapes would defeat
    # the check script's redaction of quoted tab titles).
    raise UnrecognizedTabError(f'PAYROLL formula "{formula}" is not a SUM of one range')


def _header(tab: Tab, r1: int, r2: int) -> _Columns:
    for row in (r1, *range(r1 - 1, r1 - 1 - HEADER_ROWS_ABOVE, -1)):
        if row < 1:
            break
        found: dict[str, int] = {}
        for col in range(1, tab.max_col + 1):
            label = _header_text(tab.cell(row, col))
            for kind, names in (
                ("name", NAME_HEADERS),
                ("position", POSITION_HEADERS),
                ("team", TEAM_HEADERS),
            ):
                if label in names:
                    found.setdefault(kind, col)
        if "name" in found:
            return _Columns(row, found["name"], found.get("position"), found.get("team"))
    top = max(r1 - HEADER_ROWS_ABOVE, 1)
    raise UnrecognizedTabError(f"no NAME header in rows {top}-{r1} above the payroll range")


def _counted_rows(
    tab: Tab, cols: _Columns, salary_col: int, first: int, last: int
) -> list[SheetRow]:
    rows = []
    for r in range(first, min(last, tab.max_row) + 1):  # past the last cell, all is blank
        name = _text(tab.cell(r, cols.name))
        salary = tab.cell(r, salary_col)
        if not name and salary.is_blank:
            continue  # a blank row inside the range
        rows.append(_row(tab, cols, r, name, salary, counted=True, ir=None))
    return rows


def _ir_rows(tab: Tab, cols: _Columns, salary_col: int, last: int) -> list[SheetRow]:
    rows = []
    for r in range(last + 1, tab.max_row + 1):
        label = IR_LABELS.get(tab.cell(r, 1).text)
        if label is None:
            continue  # not an IR row: nothing else in it is read
        name = _text(tab.cell(r, cols.name))
        if not name:
            continue  # an empty IR slot
        rows.append(_row(tab, cols, r, name, tab.cell(r, salary_col), counted=False, ir=label))
    return rows


def _row(
    tab: Tab, cols: _Columns, r: int, name: str, salary: Cell, *, counted: bool, ir: IRLabel | None
) -> SheetRow:
    return SheetRow(
        row=r,
        name=name,
        position=_text(tab.cell(r, cols.position)) if cols.position else "",
        team=_text(tab.cell(r, cols.team)) if cols.team else "",
        salary=_whole(salary),
        counted=counted,
        ir=ir,
    )


def _summary_cap(grid: Grid, refs: Counter[tuple[str, str]]) -> tuple[int | None, str | None]:
    if not refs:
        return None, None
    ranked = refs.most_common(2)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None, None  # tabs disagree evenly on where the cap is: don't pick one
    (title, ref), _ = ranked[0]
    summary = grid.tab(title)
    if summary is None:
        return None, _reference(title, ref)
    return _whole(summary.at(ref)), _reference(title, ref)


def _reference(title: str, ref: str) -> str:
    """``'Title'!B3``, with a quote in the title doubled as Sheets writes it."""
    escaped = title.replace("'", "''")
    return f"'{escaped}'!{ref}"


def _label(tab: Tab, word: str) -> tuple[int, int] | None:
    """Where the tab's ``PAYROLL`` / ``CAP`` label is, in its first rows."""
    for row in range(1, LABEL_ROWS + 1):
        for col in range(1, tab.max_col + 1):
            if _header_text(tab.cell(row, col)) == word:
                return row, col
    return None


def _formula_cell(tab: Tab, label: tuple[int, int] | None) -> Cell | None:
    """The first non-empty cell right of a label (the label's formula or value)."""
    if label is None:
        return None
    row, col = label
    for c in range(col + 1, col + 1 + LABEL_REACH):
        cell = tab.cell(row, c)
        if cell.formula or not cell.is_blank:
            return cell
    return None


def _header_text(cell: Cell) -> str:
    return " ".join(cell.text.casefold().rstrip(".:").split())


def _text(cell: Cell) -> str:
    """A name / position / team cell as text (a number becomes its digits)."""
    number = cell.number
    if number is not None:
        return str(int(number)) if number.is_integer() else str(number)
    return " ".join(cell.text.split())


def _whole(cell: Cell | None) -> int | None:
    number = cell.number if cell is not None else None
    return round(number) if number is not None else None
