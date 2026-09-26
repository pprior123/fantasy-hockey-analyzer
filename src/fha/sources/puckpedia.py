"""Free-agent salaries from the owner's PuckPedia CSV (SPEC §3, §4; DECISIONS, "M3:
free-agent salary CSV format").

The owner pastes PuckPedia's salary tables (skaters and goalies) into a CSV and
adds one header row. Columns are found by header name, case-insensitive:
``Player``, ``Pos`` and ``Cap Hit`` are required, ``Team`` is optional, and
every other column is ignored. A missing column or a malformed value rejects
the whole file, naming its line: nothing is guessed. Names are tidied
(whitespace) but not normalized; matching them to Yahoo is the matcher's job.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from fha.domain.names import position_group

REQUIRED = ("Player", "Pos", "Cap Hit")
OPTIONAL = ("Team",)
DOLLARS = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+)")
NBSP = "\N{NO-BREAK SPACE}"


class SalaryCsvError(ValueError):
    """The file can't be imported; the message names the line and the problem."""


@dataclass(frozen=True)
class SalaryRow:
    name: str  # as written, whitespace tidied: "Carlsson, Leo"
    position: str  # uppercased, e.g. "C", "D", "G", "C/L"
    team: str | None  # as written, when the file has a Team column
    aav: int  # the cap hit in dollars
    line: int  # the row's line in the file, for messages


class SalarySource(Protocol):
    """Where free agents' salaries come from (SPEC §3). Phase 1: the CSV."""

    async def rows(self) -> list[SalaryRow]: ...


class CsvSalarySource:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def rows(self) -> list[SalaryRow]:
        return parse_salary_csv(self._data)


class FakeSalarySource:
    def __init__(self, rows: Sequence[SalaryRow]) -> None:
        self._rows = list(rows)

    async def rows(self) -> list[SalaryRow]:
        return list(self._rows)


def decode(data: bytes) -> str:
    """UTF-8 (a byte-order mark is dropped), else Mac Roman, which is what the owner's
    spreadsheet app saves; non-breaking spaces become plain spaces."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("mac_roman")
    return text.replace(NBSP, " ")


def parse_salary_csv(data: bytes) -> list[SalaryRow]:
    reader = csv.reader(io.StringIO(decode(data)))
    columns: dict[str, int] | None = None
    rows: list[SalaryRow] = []
    for cells in reader:
        if not any(cell.strip() for cell in cells):
            continue  # blank lines between pasted pages
        if columns is None:
            columns = _columns(cells, reader.line_num)
        else:
            rows.append(_row(cells, columns, reader.line_num))
    if columns is None:
        raise SalaryCsvError("the file is empty")
    return rows


def _columns(header: list[str], line: int) -> dict[str, int]:
    wanted = {name.casefold(): name for name in (*REQUIRED, *OPTIONAL)}
    columns: dict[str, int] = {}
    for index, label in enumerate(header):
        name = wanted.get(label.strip().casefold())
        if name is None:
            continue
        if name in columns:
            raise SalaryCsvError(f"line {line}: two {name!r} columns")
        columns[name] = index
    missing = [name for name in REQUIRED if name not in columns]
    if missing:
        plural = "s" if len(missing) > 1 else ""
        raise SalaryCsvError(
            f"line {line}: missing column{plural}: {', '.join(missing)} "
            "(the header row needs Player, Pos and Cap Hit)"
        )
    return columns


def _row(cells: list[str], columns: dict[str, int], line: int) -> SalaryRow:
    def cell(name: str) -> str:
        index = columns[name]
        if index >= len(cells):
            raise SalaryCsvError(f"line {line}: no {name} value (the row is short)")
        return cells[index].strip()

    name = " ".join(cell("Player").split())
    if not name:
        raise SalaryCsvError(f"line {line}: Player is blank")
    position = cell("Pos")
    if position_group(position) is None:
        raise SalaryCsvError(f"line {line}: Pos {position!r} is not a position")
    cap = cell("Cap Hit")
    dollars = DOLLARS.fullmatch(cap)
    if dollars is None:
        raise SalaryCsvError(f"line {line}: Cap Hit {cap!r} is not a dollar amount")
    team = None
    if "Team" in columns and columns["Team"] < len(cells):
        team = cells[columns["Team"]].strip() or None
    return SalaryRow(name, position.upper(), team, int(dollars[1].replace(",", "")), line)
