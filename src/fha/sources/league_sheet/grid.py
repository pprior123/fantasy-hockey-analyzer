"""The grid both sheet readers produce: per tab, each cell's value and formula.

A grid holds *every* cell of every tab, contact details included, because the
PAYROLL cell's position is only known after reading (SPEC §4a). So it never
prints its cells: ``repr`` shows titles and sizes only. The parser keeps what
it may and the grid is then discarded.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

Value = str | int | float | bool | None

A1 = re.compile(r"\$?([A-Za-z]{1,3})\$?(\d+)")


@dataclass(frozen=True)
class Cell:
    """A cell's value (what the sheet shows) and its formula, if it has one."""

    value: Value = None
    formula: str | None = None

    @property
    def text(self) -> str:
        """The value as trimmed text ("" for no value); numbers are not text."""
        return self.value.strip() if isinstance(self.value, str) else ""

    @property
    def number(self) -> float | None:
        """The value if it is a finite number (bools, inf and nan are not)."""
        v = self.value
        if not isinstance(v, int | float) or isinstance(v, bool):
            return None
        number = float(v)
        return number if math.isfinite(number) else None

    @property
    def is_blank(self) -> bool:
        return self.value is None or (isinstance(self.value, str) and not self.value.strip())


EMPTY = Cell()


def column_number(letters: str) -> int:
    """``A`` → 1, ``Z`` → 26, ``AA`` → 27."""
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - ord("A") + 1
    return n


def column_letters(number: int) -> str:
    """1 → ``A``, 27 → ``AA``."""
    if number < 1:
        raise ValueError(f"column numbers start at 1, got {number}")
    out = ""
    while number:
        number, rem = divmod(number - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def parse_a1(ref: str) -> tuple[int, int]:
    """``F7`` (or ``$F$7``) → (row 7, column 6)."""
    m = A1.fullmatch(ref.strip())
    if not m:
        raise ValueError(f"not an A1 cell reference: {ref!r}")
    return int(m[2]), column_number(m[1])


def a1(row: int, col: int) -> str:
    return f"{column_letters(col)}{row}"


@dataclass(frozen=True)
class Tab:
    """One tab: cells by (row, column), both 1-based. Absent cells are empty."""

    title: str
    cells: Mapping[tuple[int, int], Cell] = field(default_factory=dict, repr=False)

    def cell(self, row: int, col: int) -> Cell:
        return self.cells.get((row, col), EMPTY)

    def at(self, ref: str) -> Cell:
        return self.cell(*parse_a1(ref))

    @property
    def max_row(self) -> int:
        return max((r for r, _ in self.cells), default=0)

    @property
    def max_col(self) -> int:
        return max((c for _, c in self.cells), default=0)

    def __repr__(self) -> str:
        return f"Tab({self.title!r}, {len(self.cells)} cells)"

    @classmethod
    def from_rows(cls, title: str, rows: Sequence[Sequence[Any]], start_row: int = 1) -> Tab:
        """A tab from row lists, for tests and readers. An item is a value, a
        ``Cell``, or a string starting with ``=`` (a formula with no value), or
        a ``(value, formula)`` pair. ``None`` and "" leave the cell empty."""
        cells: dict[tuple[int, int], Cell] = {}
        for r, row in enumerate(rows, start=start_row):
            for c, item in enumerate(row, start=1):
                cell = _cell(item)
                if cell != EMPTY:
                    cells[(r, c)] = cell
        return cls(title, cells)


def _cell(item: Any) -> Cell:
    if isinstance(item, Cell):
        return item
    if isinstance(item, tuple):
        value, formula = item
        return Cell(value, formula)
    if isinstance(item, str) and item.startswith("="):
        return Cell(None, item)
    if item == "":
        return EMPTY
    return Cell(item)


@dataclass(frozen=True)
class Grid:
    tabs: tuple[Tab, ...]

    def tab(self, title: str) -> Tab | None:
        return next((t for t in self.tabs if t.title == title), None)

    def __iter__(self) -> Iterator[Tab]:
        return iter(self.tabs)
