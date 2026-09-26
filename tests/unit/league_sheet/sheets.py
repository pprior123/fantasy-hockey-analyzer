"""Synthetic league sheets for tests: made-up names, every layout variant seen in
the real sheet (DECISIONS, "M3: league sheet layouts seen in the real sheet").

Every tab carries a fake contact block below its rows (as the real tabs do),
and the summary tab carries fake contact details too. So a test can assert
that none of ``CONTACT`` reaches a parsed result or an error.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from fha.sources.league_sheet.grid import Cell, Grid, Tab, column_number

SUMMARY = "Summary"
CAP = 119_600_000
# Fake contact details: none of these may ever leave the grid.
CONTACT = (
    "pat.manager@example.com",
    "555-0142",
    "Pat Q. Manager",
    "robin.gm@example.org",
    "555-0199",
    "Robin R. Gm",
)

Salary = int | float | str | None


@dataclass(frozen=True)
class Player:
    name: str
    pos: str = "C"
    team: str = "TBL"
    salary: Salary = 1_000_000


@dataclass(frozen=True)
class IRRow:
    label: str  # "IR", "IR+", or anything else for a row that is not IR
    player: Player | None = None  # None: an empty IR slot


@dataclass
class TeamTab:
    title: str
    players: Sequence[Player | None]  # None: a blank row inside the range
    start: int = 7  # the payroll range's first row
    header: int | Literal["above", "in_range"] = "above"  # a row number, or where
    salary_col: str = "F"
    name_col: str = "C"
    pos_col: str = "D"
    team_col: str = "E"
    headers: dict[str, str] = field(
        default_factory=lambda: {
            "name": "NAME",
            "pos": "Position",
            "team": "NHL Team",
            "salary": "25-26",
        }
    )
    payroll: Literal["direct", "sum_ref", "cell_ref"] = "direct"
    payroll_formula: str | None = None  # overrides the generated formula
    payroll_value: Salary = "computed"  # "computed": what SUM gives
    below: Sequence[IRRow | Player] = ()  # IR rows, or unlabelled players below
    cap_formula: str | None = f"='{SUMMARY}'!B3"
    cap_value: int | None = CAP
    extra_salary_cols: Sequence[str] = ()  # other seasons, not counted
    payroll_label: str = "PAYROLL"

    @property
    def end(self) -> int:
        return self.start + len(self.players) - 1 + (1 if self.header == "in_range" else 0)

    def tab(self) -> Tab:
        cells: dict[tuple[int, int], Cell] = {}

        def put(row: int, col: str, value: Any = None, formula: str | None = None) -> None:
            if value is not None or formula is not None:
                cells[(row, column_number(col))] = Cell(value, formula)

        s = self.salary_col
        first_player = self.start + (1 if self.header == "in_range" else 0)
        if self.header == "in_range":
            header_row = self.start
        elif self.header == "above":
            header_row = self.start - 1
        else:
            header_row = self.header
        rng = f"{s}{self.start}:{s}{self.end}"
        total = sum(
            p.salary for p in self.players if p is not None and isinstance(p.salary, int | float)
        )
        value = total if self.payroll_value == "computed" else self.payroll_value

        put(1, "A", "Keeper league 2026-27")
        put(2, "A", "CAP:")
        put(2, "C", self.cap_value, self.cap_formula)
        put(3, "A", self.payroll_label)
        after = self.end + len(self.below) + 3
        if self.payroll_formula is not None:
            put(3, "C", value, self.payroll_formula)
        elif self.payroll == "direct":
            put(3, "C", value, f"=SUM({rng})")
        elif self.payroll == "sum_ref":
            put(3, "C", value, f"=SUM(C{after})")
            put(after, "C", value, f"=SUM({rng})")
        else:
            put(3, "C", value, f"=C{after}")
            put(after, "C", value, f"=sum({rng})")

        put(header_row, self.name_col, self.headers["name"])
        put(header_row, self.pos_col, self.headers["pos"])
        put(header_row, self.team_col, self.headers["team"])
        put(header_row, s, self.headers["salary"])
        for extra in self.extra_salary_cols:
            put(header_row, extra, "27-28")
        for i, p in enumerate(self.players):
            r = first_player + i
            if p is None:
                continue
            put(r, "B", i + 1)  # a row number, as some tabs have
            put(r, self.name_col, p.name)
            put(r, self.pos_col, p.pos)
            put(r, self.team_col, p.team)
            put(r, s, p.salary)
            for extra in self.extra_salary_cols:
                put(r, extra, 2_000_000)
        for j, item in enumerate(self.below):
            r = self.end + 1 + j
            if isinstance(item, IRRow):
                put(r, "A", item.label)
                put(r, "B", 99)
                p = item.player
            else:
                p = item
            if p is not None:
                put(r, self.name_col, p.name)
                put(r, self.pos_col, p.pos)
                put(r, self.team_col, p.team)
                put(r, s, p.salary)
        # The contact block, below everything (as on the real tabs).
        put(after + 2, "C", "Manager")
        put(after + 2, "D", "Email")
        put(after + 3, "A", CONTACT[2])
        put(after + 3, "C", CONTACT[0])
        put(after + 3, "D", CONTACT[1])
        return Tab(self.title, cells)


def summary_tab(cap: int | None = CAP) -> Tab:
    return Tab.from_rows(
        SUMMARY,
        [
            ["Managers"],
            [],
            ["Salary cap", cap],
            [],
            [CONTACT[5], CONTACT[3], CONTACT[4]],
        ],
    )


def notes_tab() -> Tab:
    return Tab.from_rows("Rule ideas", [["Proposal", "Votes"], ["Bigger bench", 3]])


def roster(n: int, prefix: str = "Skater", **kw: Any) -> list[Player]:
    return [Player(f"{prefix} {i + 1}", salary=1_000_000 + i * 250_000, **kw) for i in range(n)]


def grid(*tabs: TeamTab | Tab, summary: Tab | None = None) -> Grid:
    built = [t.tab() if isinstance(t, TeamTab) else t for t in tabs]
    return Grid((summary if summary is not None else summary_tab(), *built))
