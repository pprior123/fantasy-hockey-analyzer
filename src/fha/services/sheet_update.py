"""The owner's-tab update (SPEC §4a "Writing"): from the league sheet, the Yahoo
roster and the free-agent cap hits to a ``plan_update`` and its digest.

``prepare_update`` matches the sheet's tabs as every screen does (``tab_reports``)
and plans the owner's tab only: the one bound to the team Yahoo marks as the
owner's (``is_owned_by_current_login``). Anything that stops a plan before the
domain can make one (no tab bound, an unrecognized tab, a sheet stored before
this feature) is a ``problem``, said in the owner's words.

The digest names the tab's parsed state, the edits and the owner's ticks. The
confirm re-reads the sheet, recomputes, and writes only if the digest is the one
the preview showed: the plan the owner tapped is the plan written.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass

from fha.domain.matcher import Status
from fha.domain.sheet_update import (
    Member,
    RowStatus,
    TabLayout,
    TabRow,
    UpdatePlan,
    plan_update,
)
from fha.services.aliases import load_aliases
from fha.services.free_agents import load_free_agent_salaries
from fha.services.league_sheet import RowMatch, TabReport, tab_reports
from fha.sources.league_sheet.grid import parse_a1
from fha.sources.league_sheet.models import ParsedSheet, ParsedTab
from fha.sources.yahoo.models import IR_SLOTS, LeagueSnapshot, Player
from fha.storage.repository import Repository


@dataclass(frozen=True)
class ReviewRow:
    """A row awaiting match review, with the roster players it might be."""

    row: int
    name: str
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class SheetUpdate:
    tab: ParsedTab | None
    problem: str | None  # why there's no plan at all; None when there is one
    plan: UpdatePlan | None
    rows: tuple[TabRow, ...] = ()
    layout: TabLayout | None = None
    reviews: tuple[ReviewRow, ...] = ()
    ticked: tuple[int, ...] = ()
    digest: str = ""


async def prepare_update(
    repo: Repository, snapshot: LeagueSnapshot, sheet: ParsedSheet, ticked: Collection[int] = ()
) -> SheetUpdate:
    """The plan for the owner's tab from this sheet (new automatic matches persist,
    as on every screen)."""
    reports = await tab_reports(repo, sheet, snapshot.teams, await load_aliases(repo))
    free_agents = await load_free_agent_salaries(repo)
    return build_update(snapshot, sheet, reports, free_agents.aav, ticked)


def build_update(
    snapshot: LeagueSnapshot,
    sheet: ParsedSheet,
    reports: Sequence[TabReport],
    free_agent_aav: Mapping[str, int | None],
    ticked: Collection[int] = (),
) -> SheetUpdate:
    mine = snapshot.my_team
    if mine is None:
        return SheetUpdate(None, "Your team isn't in the Yahoo data.", None)
    report = next((r for r in reports if r.team_key == mine.team_key), None)
    if report is None:
        return SheetUpdate(
            None, "No tab is bound to your team yet (Admin, Tabs and Yahoo teams).", None
        )
    tab = report.tab
    if tab.status != "ok" or tab.payroll is None or tab.payroll_range is None:
        return SheetUpdate(tab, f"Your tab {tab.name!r} can't be read: {tab.reason}", None)
    if tab.name_column is None or tab.salary_column is None or tab.header_row is None:
        return SheetUpdate(
            tab, "The stored sheet predates this feature: read the sheet again.", None
        )
    layout = _layout(tab, tab.payroll_range, tab.header_row, tab.name_column, tab.salary_column)
    rows = tuple(_row(m) for m in report.rows)
    reviews = tuple(
        ReviewRow(m.row.row, m.row.name, tuple(c.candidate.name for c in m.result.candidates))
        for m in report.rows
        if m.result is not None and m.result.status in (Status.REVIEW, Status.AMBIGUOUS)
    )
    review_rows = {r.row for r in reviews}
    ticks = tuple(sorted(t for t in set(ticked) if t in review_rows))
    active, ir = _slots(snapshot)
    plan = plan_update(
        rows,
        layout,
        tuple(_member(e.player, e.in_ir_slot, free_agent_aav) for e in mine.roster),
        payroll=tab.payroll,
        tab_cap=tab.cap,
        cap=sheet.cap,
        active_slots=active,
        ir_slots=ir,
        ticked=ticks,
    )
    return SheetUpdate(
        tab, None, plan, rows, layout, reviews, ticks, digest(tab, rows, plan, ticks)
    )


def digest(tab: ParsedTab, rows: Sequence[TabRow], plan: UpdatePlan, ticked: Sequence[int]) -> str:
    """A SHA-256 of what the owner sees: the tab's state, the edits and the ticks."""
    state = {
        "tab": tab.name,
        "sheet_id": tab.sheet_id,
        "payroll": tab.payroll,
        "cap": plan.cap,
        "rows": [asdict(r) for r in rows],
        "edits": [asdict(e) for e in plan.edits],
        "refusals": list(plan.refusals),
        "ticked": list(ticked),
    }
    blob = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _layout(
    tab: ParsedTab, payroll_range: str, header_row: int, name_column: str, salary_column: str
) -> TabLayout:
    start, end = payroll_range.split(":")
    r1, r2 = parse_a1(start)[0], parse_a1(end)[0]
    return TabLayout(
        name_column=name_column,
        position_column=tab.position_column,
        team_column=tab.team_column,
        salary_column=salary_column,
        first_row=r1 + 1 if header_row == r1 else r1,
        last_row=r2,
        header_row=header_row,
        blank_rows=tab.blank_rows,
        empty_ir_rows=tuple(r for r, _ in tab.empty_ir_rows),
    )


def _row(match: RowMatch) -> TabRow:
    status = match.result.status if match.result is not None else None
    kind: RowStatus = (
        "matched"
        if match.player_id is not None
        else ("review" if status in (Status.REVIEW, Status.AMBIGUOUS) else "unmatched")
    )
    r = match.row
    return TabRow(
        row=r.row,
        name=r.name,
        position=r.position,
        team=r.team,
        salary=r.salary,
        counted=r.counted,
        status=kind,
        player_id=match.player_id,
        has_formula=r.has_formula,
        negative_salary=r.negative_salary,
    )


def _member(player: Player, in_ir: bool, aav: Mapping[str, int | None]) -> Member:
    return Member(
        player_id=player.player_id,
        first_name=player.first_name,
        last_name=player.last_name,
        full_name=player.name,
        position=player.display_position,
        team=player.nhl_team,
        in_ir=in_ir,
        aav=aav.get(player.player_id),
    )


def _slots(snapshot: LeagueSnapshot) -> tuple[int, int]:
    """The league's slot counts outside IR and in IR (SPEC §1: read at run time)."""
    slots = snapshot.settings.roster_slots
    ir = sum(s.count for s in slots if s.position in IR_SLOTS)
    return sum(s.count for s in slots) - ir, ir
