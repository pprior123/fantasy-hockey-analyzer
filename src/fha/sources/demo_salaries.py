"""Built-in salaries for the demo league (``FHA_DEMO=1``): no uploads needed (issue #14).

``demo_sheet_grid(snapshot)`` writes a synthetic league sheet for the demo
league's teams, in memory: a summary tab with the cap, and one tab per team
laid out like the real ones (DECISIONS, "M3: league sheet layouts seen in the
real sheet"). ``DemoLeagueSheet`` parses it as the real sheet is parsed, so
the demo exercises the same path; no ``.xlsx`` is written or read.
``demo_free_agent_rows(snapshot)`` gives free-agent cap hits as the
PuckPedia CSV would.

What it shows, deterministically (seeded, like ``demo_snapshot``):

- payrolls near ``PAYROLL_TARGETS``, the spread of a real league, with one
  team over the cap;
- about 12% entry-level deals, and cap hits that follow last season's points;
- the salary column in G on one tab, and the range starting at row 8 on
  another;
- one row on each of three tabs written "A. Surname", which waits in Admin's
  match review (the other tabs' rows add up to their payroll);
- about 15% of free agents with no row, so they show "—".

Names are the demo league's made-up ones. The sheet has no contact block.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from fha.sources.league_sheet.grid import Cell, Grid, Tab, column_number
from fha.sources.league_sheet.models import ParsedSheet
from fha.sources.league_sheet.parse import parse_sheet
from fha.sources.puckpedia import SalaryRow
from fha.sources.yahoo.models import LeagueSnapshot, Player

CAP = 119_600_000
MINIMUM = 775_000  # the league minimum salary
ENTRY_LEVEL_MAX = 950_000
SUMMARY = "Summary"
CAP_CELL = "B3"
SEED = 27
# One per team, in team order: the last is over the cap.
PAYROLL_TARGETS = (
    112_000_000,
    115_500_000,
    118_800_000,
    117_300_000,
    119_200_000,
    115_100_000,
    114_800_000,
    123_700_000,
)
START_AT_ROW_8 = 3  # the tab whose range starts a row lower
SALARY_IN_G = 4  # the tab whose salaries are in column G
VARIANT_TABS = (1, 4, 7)  # tabs with one row written "A. Surname" (the others add up)
VARIANT_ROW = 5
ENTRY_LEVEL_SHARE = 0.12
NO_ROW_SHARE = 0.15  # free agents PuckPedia has no row for
MINIMUM_SHARE = 0.6  # free agents on a minimum deal
SHEET_POSITIONS = {"LW": "L", "RW": "R"}  # the sheet writes wingers' positions as one letter


def sheet_position(player: Player) -> str:
    first = player.eligible_positions[0] if player.eligible_positions else "C"
    return SHEET_POSITIONS.get(first, first)


def sheet_team(player: Player) -> str:
    return player.nhl_team.upper()


class DemoLeagueSheet:
    """The built-in sheet, as a ``LeagueSheetSource`` (SPEC §3)."""

    def __init__(self, snapshot: LeagueSnapshot) -> None:
        self._snapshot = snapshot

    async def fetch(self) -> ParsedSheet:
        return parse_sheet(demo_sheet_grid(self._snapshot))

    def __repr__(self) -> str:
        return "DemoLeagueSheet()"


@dataclass(frozen=True)
class _Row:
    name: str
    pos: str
    team: str
    salary: int


def demo_sheet_grid(snapshot: LeagueSnapshot) -> Grid:
    rng = random.Random(SEED)  # noqa: S311 - demo data, not security
    tabs = [_summary()]
    for t, (team, target) in enumerate(zip(snapshot.teams, PAYROLL_TARGETS, strict=False)):
        counted = [e.player for e in team.roster if not e.in_ir_slot]
        ir = [e for e in team.roster if e.in_ir_slot]
        everyone = counted + [e.player for e in ir]
        raw = {p.player_id: _raw_salary(rng, snapshot, p) for p in everyone}
        spread = sum(raw[p.player_id] - MINIMUM for p in counted)
        scale = (target - MINIMUM * len(counted)) / spread if spread else 1.0
        salary = {pid: round(MINIMUM + (s - MINIMUM) * scale, -3) for pid, s in raw.items()}
        rows = [
            _Row(
                _variant(p.name) if t in VARIANT_TABS and i == VARIANT_ROW else p.name,
                sheet_position(p),
                sheet_team(p),
                int(salary[p.player_id]),
            )
            for i, p in enumerate(counted)
        ]
        below = [
            (
                e.selected_position,
                _Row(
                    e.player.name,
                    sheet_position(e.player),
                    sheet_team(e.player),
                    int(salary[e.player.player_id]),
                ),
            )
            for e in ir
        ]
        tabs.append(
            _team_tab(
                f"Manager {chr(ord('A') + t)}",
                rows,
                below,
                salary_col="G" if t == SALARY_IN_G else "F",
                start=8 if t == START_AT_ROW_8 else 7,
            )
        )
    return Grid(tuple(tabs))


def demo_free_agent_rows(snapshot: LeagueSnapshot) -> list[SalaryRow]:
    rng = random.Random(SEED + 1)  # noqa: S311 - demo data, not security
    rows: list[SalaryRow] = []
    for p in snapshot.available:
        if rng.random() < NO_ROW_SHARE:
            continue
        first, _, last = p.name.partition(" ")
        if rng.random() < MINIMUM_SHARE:
            aav = MINIMUM
        else:
            aav = int(round(rng.uniform(800_000, 4_500_000), -3))
        line = len(rows) + 2  # as if from a file with a header row
        rows.append(SalaryRow(f"{last}, {first}", sheet_position(p), sheet_team(p), aav, line))
    return rows


def _raw_salary(rng: random.Random, snapshot: LeagueSnapshot, player: Player) -> float:
    """A cap hit before scaling to the team's payroll: goalies at random, skaters by
    last season's points a game, some on entry-level deals."""
    if player.position_type == "G":
        return rng.uniform(1_000_000, 8_000_000)
    if rng.random() < ENTRY_LEVEL_SHARE:
        return rng.uniform(MINIMUM, ENTRY_LEVEL_MAX)
    line = (snapshot.last_season_stats or {}).get(player.player_key)
    values = line.values if line is not None else {}

    def stat(stat_id: str) -> float:
        raw = values.get(stat_id, "-")
        return float(raw) if raw != "-" else 0.0

    points_a_game = (stat("1") + stat("2")) / (stat("0") or 1)
    return max(MINIMUM, (800_000 + points_a_game * 9_000_000) * rng.uniform(0.7, 1.3))


def _variant(name: str) -> str:
    """ "A. Surname", as a manager might type it."""
    first, _, last = name.partition(" ")
    return f"{first[0]}. {last}"


def _summary() -> Tab:
    return Tab.from_rows(SUMMARY, [["Demo keeper league 2026-27"], [], ["Salary cap", CAP]])


def _team_tab(
    title: str,
    rows: list[_Row],
    below: list[tuple[str, _Row]],
    *,
    salary_col: str,
    start: int,
) -> Tab:
    cells: dict[tuple[int, int], Cell] = {}

    def put(row: int, col: str, value: str | int | None, formula: str | None = None) -> None:
        cells[(row, column_number(col))] = Cell(value, formula)

    end = start + len(rows) - 1
    put(1, "A", "Demo keeper league 2026-27")
    put(2, "A", "CAP:")
    put(2, "C", CAP, f"='{SUMMARY}'!{CAP_CELL}")
    put(3, "A", "PAYROLL")
    put(3, "C", sum(r.salary for r in rows), f"=SUM({salary_col}{start}:{salary_col}{end})")
    header = start - 1
    for col, text in (("C", "NAME"), ("D", "Position"), ("E", "NHL Team"), (salary_col, "26-27")):
        put(header, col, text)
    for i, (label, row) in enumerate([("", r) for r in rows] + below):
        r = start + i
        if label:
            put(r, "A", label)
        put(r, "C", row.name)
        put(r, "D", row.pos)
        put(r, "E", row.team)
        put(r, salary_col, row.salary)
    return Tab(title, cells)
