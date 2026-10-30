"""The edits that bring the owner's tab in line with their Yahoo roster (SPEC §4a
"Writing"). Pure; amounts are whole dollars.

::

    target(p)  = p is on the tab: counted if outside an IR slot, in an IR row if in one
    drop       = a row matched to no roster player (or one the owner ticked)
    to IR      = a counted row whose player now sits in an IR slot
    back       = an IR row whose player is now outside IR
    add        = a roster player in no row

Rows are never inserted or deleted: a drop clears the row's four cells (name,
position, team, salary), a move copies them verbatim to a free row of the other
kind, and an add writes ``Last, First`` with Yahoo's position and team codes and
the cap hit. Free rows: the rows this plan frees first, in row order, then the
tab's blank rows. Column A (the IR labels) and the header row are never written.

    payroll_after = payroll + Σ (new - old salary) over counted cells that change
    room_after    = cap - payroll_after             # must be >= 0

The plan refuses, with every reason it finds, rather than guess. A refused plan
has no edits. ``guard`` re-checks that every edit stays in the allowed cells.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

CellValue = str | int | None
RowStatus = Literal["matched", "unmatched", "review"]  # review: REVIEW or AMBIGUOUS


@dataclass(frozen=True)
class TabRow:
    """One player row of the tab, as parsed, with its match against the roster."""

    row: int
    name: str
    position: str
    team: str
    salary: int | None  # None: the cell holds no number (blank, ``???``)
    counted: bool  # in the PAYROLL range; False: an IR row below it
    status: RowStatus
    player_id: str | None = None  # the matched roster player
    has_formula: bool = False
    negative_salary: bool = False


@dataclass(frozen=True)
class TabLayout:
    """Where the tab's cells are (column letters; position / team may be absent)."""

    name_column: str
    position_column: str | None
    team_column: str | None
    salary_column: str
    first_row: int  # the PAYROLL range's first player row (below any header in it)
    last_row: int  # the PAYROLL range's last row
    header_row: int
    blank_rows: tuple[int, ...]
    empty_ir_rows: tuple[int, ...]

    @property
    def columns(self) -> tuple[str, ...]:
        """The four columns the app may write, in sheet order."""
        cols = (self.name_column, self.position_column, self.team_column, self.salary_column)
        return tuple(c for c in cols if c is not None)


@dataclass(frozen=True)
class Member:
    """A player on the owner's Yahoo roster."""

    player_id: str
    first_name: str
    last_name: str
    full_name: str
    position: str  # Yahoo's display position, e.g. "C,LW"
    team: str  # Yahoo's NHL team code, e.g. "TB"
    in_ir: bool
    aav: int | None  # the cap hit for a player in no row; None: unknown


@dataclass(frozen=True)
class CellEdit:
    row: int
    column: str
    old: CellValue
    new: CellValue


Action = Literal["drop", "to_ir", "back", "add"]


@dataclass(frozen=True)
class Change:
    """One player-level change, for the preview."""

    action: Action
    name: str
    from_row: int | None
    to_row: int | None


@dataclass(frozen=True)
class UpdatePlan:
    changes: tuple[Change, ...]
    edits: tuple[CellEdit, ...]  # empty when refused
    refusals: tuple[str, ...]
    payroll: int
    payroll_after: int | None  # None when a salary it needs is unknown
    cap: int | None

    @property
    def ok(self) -> bool:
        return not self.refusals

    @property
    def up_to_date(self) -> bool:
        return self.ok and not self.changes

    @property
    def room_after(self) -> int | None:
        if self.cap is None or self.payroll_after is None:
            return None
        return self.cap - self.payroll_after


def sheet_name(member: Member) -> str:
    """``Last, First``; with no last name from Yahoo, split the full name at the
    first space ("James van Riemsdyk" → "van Riemsdyk, James")."""
    first, last = member.first_name.strip(), member.last_name.strip()
    if not last:
        head, _, tail = (member.full_name.strip() or first).partition(" ")
        first, last = head, tail
    return f"{last}, {first}" if first and last else (last or first)


def plan_update(
    rows: Sequence[TabRow],
    layout: TabLayout,
    roster: Sequence[Member],
    *,
    payroll: int,
    tab_cap: int | None,
    cap: int | None,
    active_slots: int,
    ir_slots: int,
    ticked: Collection[int] = (),
) -> UpdatePlan:
    """The edits that make the tab match ``roster``, or the reasons it can't.

    ``ticked``: rows awaiting match review that the owner marked as dropped.
    ``active_slots`` / ``ir_slots``: the league's non-IR and IR slot counts.
    """
    refusals: list[str] = []
    _check_tab(rows, payroll, tab_cap, cap, refusals)
    by_player = {r.player_id: r for r in rows if r.status == "matched" and r.player_id}

    drops = [
        r for r in rows if r.status == "unmatched" or (r.status == "review" and r.row in ticked)
    ]
    for r in rows:
        if r.status == "review" and r.row not in ticked:
            refusals.append(
                f"row {r.row} ({r.name}) awaits match review: confirm it in Admin, "
                "or tick “drop this row”"
            )
    # A matched player moves when his row's kind disagrees with his slot.
    moving = [(m, by_player[m.player_id]) for m in roster if m.player_id in by_player]
    moving = [(m, r) for m, r in moving if r.counted == m.in_ir]
    to_ir = [(m, r) for m, r in moving if m.in_ir]
    back = [(m, r) for m, r in moving if not m.in_ir]
    adds = [m for m in roster if m.player_id not in by_player]

    freed_counted = sorted([r.row for r in drops if r.counted] + [r.row for _, r in to_ir])
    freed_ir = sorted([r.row for r in drops if not r.counted] + [r.row for _, r in back])
    counted_pool = [*freed_counted, *layout.blank_rows]
    ir_pool = [*freed_ir, *layout.empty_ir_rows]

    if "A" in layout.columns:
        refusals.append("the tab's player columns include column A, which holds the IR labels")
    olds = {r.row: _values(r, layout) for r in rows}
    cells: dict[tuple[int, str], CellValue] = {}  # (row, column) -> new value
    # Every freed row is cleared first, so a fill into a freed row always wins.
    for r in [*drops, *(r for _, r in to_ir), *(r for _, r in back)]:
        cells.update(dict.fromkeys((r.row, c) for c in layout.columns))  # None: clear
    changes = [Change("drop", r.name, r.row, None) for r in drops]
    for into_ir, moves, pool in ((True, to_ir, ir_pool), (False, back, counted_pool)):
        for m, r in moves:
            if r.salary is None or r.has_formula:
                refusals.append(_unmovable(r))
            target = _take(pool, m, into_ir, refusals)
            if target is not None:
                cells.update({(target, c): v for c, v in olds[r.row].items()})
            changes.append(Change("to_ir" if into_ir else "back", r.name, r.row, target))
    if adds and (layout.position_column is None or layout.team_column is None):
        refusals.append("the tab has no position or NHL team column, so a new row can't be written")
    for m in adds:
        target = _take(ir_pool if m.in_ir else counted_pool, m, m.in_ir, refusals)
        if m.aav is None:
            refusals.append(f"{m.full_name}'s cap hit is unknown: set it with Cap hit edit (Admin)")
        if target is not None:
            cells.update({(target, c): v for c, v in _new_row(m, layout).items()})
        changes.append(Change("add", sheet_name(m), None, target))

    for r in drops:
        if r.has_formula:
            refusals.append(f"row {r.row} ({r.name}) holds a formula: change it by hand")

    edits = tuple(
        CellEdit(row, col, olds.get(row, {}).get(col), new)
        for (row, col), new in sorted(cells.items())
        if olds.get(row, {}).get(col) != new
    )
    unknown = any(m.aav is None and not m.in_ir for m in adds)
    payroll_after = None if unknown else _payroll_after(payroll, edits, layout)
    _check_result(rows, layout, edits, payroll_after, cap, active_slots, ir_slots, refusals)
    return UpdatePlan(
        changes=tuple(changes),
        edits=() if refusals else edits,
        refusals=tuple(dict.fromkeys(refusals)),
        payroll=payroll,
        payroll_after=payroll_after,
        cap=cap,
    )


def allowed_rows(rows: Iterable[TabRow], layout: TabLayout) -> frozenset[int]:
    """The rows the app may write: the PAYROLL range's player rows and the IR rows."""
    in_range = range(layout.first_row, layout.last_row + 1)
    ir = {r.row for r in rows if not r.counted} | set(layout.empty_ir_rows)
    return frozenset({*in_range, *ir} - {layout.header_row})


class GuardError(ValueError):
    """An edit would leave the allowed cells. Never write after this."""


def guard(edits: Iterable[CellEdit], rows: Iterable[TabRow], layout: TabLayout) -> None:
    """Raise unless every edit is in an allowed row and one of the four columns."""
    allowed = allowed_rows(rows, layout)
    columns = set(layout.columns)
    for e in edits:
        if e.row not in allowed or e.column not in columns or e.column == "A":
            raise GuardError(f"edit outside the allowed cells: {e.column}{e.row}")
        if e.new is not None and not isinstance(e.new, str | int):
            raise GuardError(f"edit with an unwritable value at {e.column}{e.row}")


# ---------------------------------------------------------------- helpers


def _check_tab(
    rows: Sequence[TabRow],
    payroll: int,
    tab_cap: int | None,
    cap: int | None,
    refusals: list[str],
) -> None:
    if cap is None:
        refusals.append("the league cap is unknown (the summary tab's cap cell)")
    elif tab_cap != cap:
        refusals.append("the tab's CAP differs from the league cap: fix the tab by hand first")
    for r in rows:
        if r.negative_salary:
            refusals.append(f"row {r.row} ({r.name}) has a negative salary: fix it by hand first")
    summed = [r.salary for r in rows if r.counted and r.salary is not None]
    if abs(payroll - sum(summed)) > (len(summed) + 1) / 2:
        refusals.append(
            "the tab's PAYROLL isn't the sum of its own counted rows: fix the tab by hand first"
        )


def _take(pool: list[int], m: Member, into_ir: bool, refusals: list[str]) -> int | None:
    if pool:
        return pool.pop(0)
    where = "IR row below the PAYROLL range" if into_ir else "blank row inside the PAYROLL range"
    refusals.append(f"no free {where} for {m.full_name}: add one to the tab by hand")
    return None


def _unmovable(r: TabRow) -> str:
    if r.has_formula:
        return f"row {r.row} ({r.name}) holds a formula: move it by hand"
    return f"row {r.row} ({r.name}) has no salary number: fix it by hand first"


def _values(r: TabRow, layout: TabLayout) -> dict[str, CellValue]:
    values: dict[str, CellValue] = {layout.name_column: r.name or None}
    if layout.position_column is not None:
        values[layout.position_column] = r.position or None
    if layout.team_column is not None:
        values[layout.team_column] = r.team or None
    values[layout.salary_column] = r.salary
    return values


def _new_row(m: Member, layout: TabLayout) -> dict[str, CellValue]:
    values: dict[str, CellValue] = {layout.name_column: sheet_name(m)}
    if layout.position_column is not None:
        values[layout.position_column] = m.position or None
    if layout.team_column is not None:
        values[layout.team_column] = m.team or None
    values[layout.salary_column] = m.aav
    return values


def _payroll_after(payroll: int, edits: Sequence[CellEdit], layout: TabLayout) -> int:
    """PAYROLL after the edits: SUM adds numbers and skips blanks and text."""
    total = payroll
    for e in edits:  # no edit is above the range (``guard``), so "≤ last row" is counted
        if e.column == layout.salary_column and e.row <= layout.last_row:
            new = e.new if isinstance(e.new, int) else 0
            old = e.old if isinstance(e.old, int) else 0
            total += new - old
    return total


def _check_result(
    rows: Sequence[TabRow],
    layout: TabLayout,
    edits: Sequence[CellEdit],
    payroll_after: int | None,
    cap: int | None,
    active_slots: int,
    ir_slots: int,
    refusals: list[str],
) -> None:
    names: dict[int, CellValue] = {r.row: r.name for r in rows}
    names.update({e.row: e.new for e in edits if e.column == layout.name_column})
    listed = [row for row, name in names.items() if name]
    counted = sum(row <= layout.last_row for row in listed)
    ir = len(listed) - counted
    if counted > active_slots:
        refusals.append(
            f"the tab would list {counted} players outside IR, more than the league's "
            f"{active_slots}: check the Yahoo roster"
        )
    if ir > ir_slots:
        refusals.append(
            f"the tab would list {ir} IR players, more than the league's {ir_slots}: "
            "check the Yahoo roster"
        )
    if payroll_after is not None and cap is not None and payroll_after > cap:
        refusals.append(
            f"over the cap by ${payroll_after - cap:,} after these changes: "
            "fix the Yahoo roster first"
        )
