"""What the parser keeps from the league sheet (SPEC §4a).

Only cells from each tab's resolved payroll range, its header row, its IR
rows and its CAP / PAYROLL cells reach these objects. Contact details and
every other cell stay in the grid, which is discarded. The fields a writer
needs (SPEC §4a "Writing") are column letters, row numbers and flags, never
another cell's contents.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["ok", "unrecognized"]


class LeagueSheetError(Exception):
    """The sheet could not be read at all (a tab that can't be parsed is not this)."""


IRLabel = Literal["IR", "IR+"]


@dataclass(frozen=True)
class SheetRow:
    """One player row: counted (in the PAYROLL range) or an IR row below it."""

    row: int  # the sheet row number, for the owner to find it
    name: str
    position: str
    team: str
    salary: int | None  # None: no numeric salary in the cell (e.g. "???")
    counted: bool
    ir: IRLabel | None = None
    has_formula: bool = False  # the name, position, team or salary cell holds one
    negative_salary: bool = False  # SUM adds a number that ``salary`` reads as None


@dataclass(frozen=True)
class ParsedTab:
    name: str
    status: Status
    reason: str | None  # why the tab is unrecognized; None when ok
    payroll: int | None  # the tab's own PAYROLL value: the official payroll
    cap: int | None  # the tab's CAP value (it references the summary tab's cap)
    salary_column: str | None  # e.g. "F"
    rows: tuple[SheetRow, ...] = ()
    payroll_formula: str | None = None  # the PAYROLL cell's formula, as written
    payroll_range: str | None = None  # the resolved range, e.g. "F7:F33"
    # For the writer (SPEC §4a "Writing"); None / () in a sheet stored before them.
    sheet_id: int | None = None  # Google's numeric tab ID (None from an .xlsx)
    header_row: int | None = None
    name_column: str | None = None
    position_column: str | None = None  # None: the tab has no position header
    team_column: str | None = None  # None: the tab has no NHL team header
    blank_rows: tuple[int, ...] = ()  # in the range, all four cells blank, no formula
    empty_ir_rows: tuple[tuple[int, IRLabel], ...] = ()  # IR rows likewise blank
    # The IR block: the run of IR-labelled rows from the first one below the range
    # (first, last row). The writer may touch IR rows only inside it.
    ir_block: tuple[int, int] | None = None

    @property
    def counted(self) -> tuple[SheetRow, ...]:
        return tuple(r for r in self.rows if r.counted)

    @property
    def ir_rows(self) -> tuple[SheetRow, ...]:
        return tuple(r for r in self.rows if not r.counted)

    @property
    def counted_total(self) -> int:
        """The sum of the counted rows' numeric salaries (what SUM adds up)."""
        return sum(r.salary for r in self.counted if r.salary is not None)


@dataclass(frozen=True)
class ParsedSheet:
    cap: int | None  # the summary tab's cap cell
    cap_source: str | None  # where the cap came from, e.g. "'Summary'!B3"
    tabs: tuple[ParsedTab, ...]  # the team tabs (those with a PAYROLL label)
    other_tabs: tuple[str, ...] = ()  # tabs with no PAYROLL: summary, notes, ...
    # Tabs (any kind) the reader may edit: none protected against it as a whole.
    # None: unknown (an .xlsx). Not stored: the writer checks a fresh read.
    editable_tabs: tuple[str, ...] | None = None

    def tab(self, name: str) -> ParsedTab | None:
        return next((t for t in self.tabs if t.name == name), None)

    @property
    def cap_mismatches(self) -> tuple[ParsedTab, ...]:
        """Team tabs whose CAP value differs from the summary tab's cap (SPEC §4a)."""
        if self.cap is None:
            return ()
        return tuple(t for t in self.tabs if t.cap is not None and t.cap != self.cap)
