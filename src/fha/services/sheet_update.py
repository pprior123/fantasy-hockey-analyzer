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
import logging
import secrets
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Literal

from fha.domain.matcher import Status
from fha.domain.sheet_update import (
    GuardError,
    Member,
    RowStatus,
    TabLayout,
    TabRow,
    UpdatePlan,
    guard,
    in_ir_block,
    plan_update,
    row_values,
)
from fha.services.aliases import load_aliases
from fha.services.clock import Clock
from fha.services.free_agents import load_free_agent_salaries, row_key
from fha.services.league_sheet import (
    RowMatch,
    TabReport,
    bind_written_rows,
    load_row_bindings,
    read_league_sheet,
    tab_reports,
)
from fha.sources.league_sheet.grid import parse_a1
from fha.sources.league_sheet.models import LeagueSheetError, ParsedSheet, ParsedTab
from fha.sources.league_sheet.source import LeagueSheetSource
from fha.sources.league_sheet.writer import (
    LeagueSheetWriter,
    NotSentError,
    RejectedWriteError,
    UncertainWriteError,
)
from fha.sources.yahoo.models import IR_SLOTS, LeagueSnapshot, Player
from fha.storage.repository import Repository, RepositoryError

log = logging.getLogger(__name__)


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
    problem_code: str | None = None


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
        return SheetUpdate(
            None, "Your team isn't in the Yahoo data.", None, problem_code="problem_no_team"
        )
    report = next((r for r in reports if r.team_key == mine.team_key), None)
    if report is None:
        return SheetUpdate(
            None,
            "No tab is bound to your team yet (Admin, Tabs and Yahoo teams).",
            None,
            problem_code="problem_unbound",
        )
    tab = report.tab
    if tab.status != "ok" or tab.payroll is None or tab.payroll_range is None:
        return SheetUpdate(
            tab,
            f"Your tab {tab.name!r} can't be read: {tab.reason}",
            None,
            problem_code="problem_unreadable_tab",
        )
    if tab.name_column is None or tab.salary_column is None or tab.header_row is None:
        return SheetUpdate(
            tab,
            "The stored sheet predates this feature: read the sheet again.",
            None,
            problem_code="problem_old_sheet",
        )
    layout = _layout(tab, tab.payroll_range, tab.header_row, tab.name_column, tab.salary_column)
    rows = tuple(_row(m) for m in report.rows)
    # A named IR row outside the IR block may be a contact line: the plan refuses it by
    # row number, and it is never offered for review here.
    reviews = tuple(
        ReviewRow(m.row.row, m.row.name, tuple(c.candidate.name for c in m.result.candidates))
        for m in report.rows
        if m.result is not None
        and m.result.status in (Status.REVIEW, Status.AMBIGUOUS)
        and (m.row.counted or in_ir_block(m.row.row, layout))
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
        ir_block=tab.ir_block,
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


# ---------------------------------------------------------------- the write


WriteKind = Literal["written", "up_to_date", "refused", "unreadable", "rejected", "unknown"]
BeforeWrite = Callable[[SheetUpdate, Mapping[str, str]], Awaitable[str | None]]
AttemptSource = Literal["auto", "manual"]
WRITE_ATTEMPTS = "sheet_write_attempts"
ATTEMPT_SECONDS = 600


@dataclass(frozen=True)
class WriteJournal:
    repo: Repository
    source: AttemptSource

    async def record(self, attempt_id: str, at: float, cells: int) -> None:
        await self.repo.put(
            WRITE_ATTEMPTS,
            attempt_id,
            {"at": at, "deadline": at + ATTEMPT_SECONDS, "source": self.source, "cells": cells},
        )

    async def clear(self, attempt_id: str) -> None:
        try:
            await self.repo.delete(WRITE_ATTEMPTS, attempt_id)
        except RepositoryError as error:
            log.warning("Sheet write attempt could not be cleared (%s)", type(error).__name__)


@dataclass(frozen=True)
class WriteResult:
    kind: WriteKind
    message: str
    code: str | None = None
    attempted: int = 0
    applied: Literal["yes", "no", "maybe"] = "no"
    payroll: int | None = None
    refusal: str | None = None
    changes: tuple[str, ...] = ()
    attempt_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.kind in ("written", "up_to_date")

    @property
    def written(self) -> int:
        return self.attempted if self.applied == "yes" else 0


async def write_update(
    repo: Repository,
    clock: Clock,
    source: LeagueSheetSource,
    writer: LeagueSheetWriter,
    snapshot: LeagueSnapshot,
    expected: str,
    ticked: Collection[int] = (),
    *,
    before_write: BeforeWrite | None = None,
    journal: WriteJournal | None = None,
    deadline: float | None = None,
) -> WriteResult:
    """Re-read the sheet, recompute the plan, and write it only if it is the one the
    owner saw (``expected`` is the preview's digest) and every other tab is protected
    against the app. Then bind the new rows and read the tab back."""
    try:
        sheet = await read_league_sheet(repo, source, clock)
    except LeagueSheetError as e:
        return WriteResult(
            "unreadable",
            f"Nothing was written: the sheet couldn't be read ({e}).",
            "sheet_unreadable",
        )
    except RepositoryError:
        return WriteResult(
            "unreadable", "Nothing was written: the sheet couldn't be stored.", "sheet_unreadable"
        )
    try:
        update = await prepare_update(repo, snapshot, sheet, ticked)
    except RepositoryError:
        return _storage_failed()
    plan, tab = update.plan, update.tab
    if update.problem or plan is None or tab is None or update.layout is None:
        return WriteResult("refused", f"Nothing was written: {update.problem}", update.problem_code)
    if plan.up_to_date:
        return WriteResult(
            "up_to_date", f"Nothing to write: {tab.name} already matches your roster."
        )
    if update.digest != expected:
        return WriteResult(
            "refused",
            "Nothing was written: the tab or your roster changed since the preview. "
            "Check the new preview.",
            "tab_changed",
        )
    if not plan.ok:
        refusal = plan.refusals[0]
        return WriteResult("refused", f"Nothing was written: {refusal}", "refusal", refusal=refusal)
    open_tabs = _unprotected(sheet, tab.name)
    if open_tabs is None:
        return WriteResult(
            "refused",
            "Nothing was written: the sheet's protections couldn't be checked.",
            "protections_unknown",
        )
    if open_tabs:
        names = ", ".join(open_tabs[:4]) + (" …" if len(open_tabs) > 4 else "")
        return WriteResult(
            "refused",
            f"Nothing was written: the app can still edit {len(open_tabs)} other tab(s) "
            f"({names}). Protect them first (Data → Protect sheets and ranges).",
            "others_unprotected",
        )
    if tab.name not in (sheet.editable_tabs or ()):
        return WriteResult(
            "refused",
            f"Nothing was written: {tab.name} is protected against the app. Add the app's "
            f"service account to its protection (Data → Protect sheets and ranges → {tab.name}).",
            "own_tab_locked",
        )
    try:
        guard(plan.edits, update.rows, update.layout)
    except GuardError as e:  # a bug: the plan left its own allowed cells
        log.error("sheet update refused by the guard: %s", e)
        return WriteResult("refused", f"Nothing was written: {e}.", "guard")
    if before_write is not None:
        try:
            code = await before_write(update, await load_row_bindings(repo, tab.name))
        except RepositoryError:
            return _storage_failed()
        if code is not None:
            return WriteResult("refused", "Nothing was written: the automatic check refused.", code)
    if journal is None:
        return await _write_and_verify(
            repo,
            clock,
            source,
            writer,
            snapshot,
            tab,
            plan,
            update.rows,
            update.layout,
            deadline=deadline,
        )

    attempt_id = secrets.token_hex(8)
    post_started = False
    result = None

    def posting() -> None:
        nonlocal post_started
        post_started = True

    try:
        try:
            await journal.record(attempt_id, clock.now(), len(plan.edits))
        except RepositoryError:
            return _storage_failed()
        result = await _write_and_verify(
            repo,
            clock,
            source,
            writer,
            snapshot,
            tab,
            plan,
            update.rows,
            update.layout,
            deadline=deadline,
            on_send=posting,
        )
        return replace(result, attempt_id=attempt_id)
    finally:
        if not post_started or (result is not None and result.kind != "unknown"):
            await journal.clear(attempt_id)


def _storage_failed() -> WriteResult:
    return WriteResult(
        "unreadable", "Nothing was written: the app's storage failed.", "storage_failed"
    )


async def _write_and_verify(
    repo: Repository,
    clock: Clock,
    source: LeagueSheetSource,
    writer: LeagueSheetWriter,
    snapshot: LeagueSnapshot,
    tab: ParsedTab,
    plan: UpdatePlan,
    rows: Sequence[TabRow],
    layout: TabLayout,
    *,
    deadline: float | None = None,
    on_send: Callable[[], None] | None = None,
) -> WriteResult:
    lost: UncertainWriteError | None = None
    try:
        if deadline is None and on_send is None:
            await writer.write(tab, plan.edits)
        else:
            await writer.write(tab, plan.edits, deadline=deadline, on_send=on_send)
    except UncertainWriteError as e:  # Google may have applied it: look before saying
        lost = e
    except RejectedWriteError as e:
        return WriteResult(
            "rejected",
            f"The write failed and nothing was changed ({e}).",
            "google_refused",
            attempted=len(plan.edits),
        )
    except LeagueSheetError as e:
        code = "out_of_time" if isinstance(e, NotSentError) and e.deadline_expired else "not_sent"
        return WriteResult("rejected", f"The write failed and nothing was changed ({e}).", code)
    written = len(plan.edits)
    try:
        after: ParsedSheet | None = None
        if lost is not None:
            log.warning("sheet update: %s; checking the tab", lost)
            try:
                after = await read_league_sheet(repo, source, clock)
                landed = await _landed(repo, snapshot, after, plan)
            except LeagueSheetError as e:
                return WriteResult(
                    "unknown",
                    f"The write's outcome is unknown ({lost}), and the tab couldn't be read "
                    f"to check ({e}). Check the tab before writing again.",
                    "lost_answer",
                    attempted=written,
                    applied="maybe",
                )
            except RepositoryError as e:  # not "Written": nothing is known yet
                log.warning("checking a lost write, the app's storage failed (%s)", e.summary)
                return WriteResult(
                    "unknown",
                    f"The write's outcome is unknown ({lost}), and the app's storage failed "
                    "while checking. Check the tab before writing again.",
                    "lost_answer",
                    attempted=written,
                    applied="maybe",
                )
            if landed is False:  # Google may still be applying it: not a "no"
                return WriteResult(
                    "unknown",
                    f"Google's answer was lost ({lost}) and the tab shows no change yet, but "
                    "Google may still apply it. Open this page again in a minute before "
                    "writing again.",
                    "lost_answer",
                    attempted=written,
                    applied="maybe",
                )
            if landed is None:
                return WriteResult(
                    "unknown",
                    f"The write's outcome is unknown ({lost}): the tab holds neither the old "
                    "cells nor the new ones. Check the tab.",
                    "lost_answer",
                    attempted=written,
                    applied="maybe",
                )
        log.info(
            "sheet update written to %s: %s",
            tab.name,
            "; ".join(f"{c.action} {c.name}" for c in plan.changes),
        )
        # Google has the change now: from here on, say so whatever fails
        await bind_written_rows(repo, tab.name, _added_keys(plan, rows, layout))
        result = await _read_back(repo, clock, source, snapshot, plan, written, after)
        if result.kind == "written":
            result = replace(result, changes=tuple(f"{c.action} {c.name}" for c in plan.changes))
        if lost is not None:
            lead = "Google's answer was lost, but the write went through. "
            return replace(result, message=lead + result.message)
        return result
    except RepositoryError as e:
        log.warning("after the sheet write, the app's storage failed (%s)", e.summary)
        return WriteResult(
            "unknown",
            f"Written ({written} cells), but the app's storage failed afterwards. "
            "Check the tab, then read the sheet again (Admin).",
            "unverified",
            attempted=written,
            applied="yes",
        )


def _unprotected(sheet: ParsedSheet, mine: str) -> list[str] | None:
    """The other tabs the app could edit, or None if the sheet doesn't say."""
    if sheet.editable_tabs is None:
        return None
    return [t for t in sheet.editable_tabs if t != mine]


def _added_keys(plan: UpdatePlan, rows: Sequence[TabRow], layout: TabLayout) -> dict[str, str]:
    """Row keys of the rows the plan adds, as the parser will read them back. A cell
    the plan leaves alone (the new value equals the old) keeps the row's value."""
    cells = {r.row: row_values(r, layout) for r in rows}
    for e in plan.edits:
        cells.setdefault(e.row, {})[e.column] = e.new

    def text(row: int, column: str | None) -> str:
        value = cells.get(row, {}).get(column) if column else None
        return " ".join(str(value or "").split())

    return {
        row_key(text(row, layout.name_column), text(row, layout.position_column)): player_id
        for row, player_id in plan.added
    }


def _cell_values(rows: Sequence[TabRow], layout: TabLayout) -> dict[int, dict[str, object]]:
    """The four cells of each row the parser read, and of each row it found blank (all
    four cells, no formula). A row it skipped (no name, say) is absent: unknown, never
    taken as blank. So is a parsed salary cell with no number: it may hold text."""
    values: dict[int, dict[str, object]] = {
        r: dict.fromkeys(layout.columns) for r in (*layout.blank_rows, *layout.empty_ir_rows)
    }
    for r in rows:
        known: dict[str, object] = dict(row_values(r, layout))
        if r.salary is None:
            del known[layout.salary_column]
        values[r.row] = known
    return values


async def _landed(
    repo: Repository, snapshot: LeagueSnapshot, sheet: ParsedSheet, plan: UpdatePlan
) -> bool | None:
    """After a lost answer: True if the tab holds every new cell, False if it still
    holds every old one (not applied yet: Google may still apply it), None if
    neither or unknown."""
    after = await prepare_update(repo, snapshot, sheet)
    if after.layout is None:
        return None
    values = _cell_values(after.rows, after.layout)

    def holds(row: int, column: str, value: object) -> bool:
        return column in values.get(row, {}) and values[row][column] == value

    if all(holds(e.row, e.column, e.new) for e in plan.edits):
        return True
    if all(holds(e.row, e.column, e.old) for e in plan.edits):
        return False
    return None


async def _read_back(
    repo: Repository,
    clock: Clock,
    source: LeagueSheetSource,
    snapshot: LeagueSnapshot,
    plan: UpdatePlan,
    written: int,
    sheet: ParsedSheet | None = None,
) -> WriteResult:
    """Read the tab back (or use ``sheet``, already read after the write) and check it."""

    def failed(why: str) -> WriteResult:
        return WriteResult(
            "unknown",
            f"Written ({written} cells), but {why}. Check the tab.",
            "unverified",
            attempted=written,
            applied="yes",
        )

    if sheet is None:
        try:
            sheet = await read_league_sheet(repo, source, clock)
        except LeagueSheetError as e:
            return failed(f"the tab couldn't be read back ({e})")
    after = await prepare_update(repo, snapshot, sheet)
    if after.tab is None or after.plan is None or after.layout is None:
        return failed(f"the tab can't be planned now ({after.problem})")
    values = _cell_values(after.rows, after.layout)
    wrong = [
        f"{e.column}{e.row}"
        for e in plan.edits
        if e.column not in values.get(e.row, {}) or values[e.row][e.column] != e.new
    ]
    if wrong:
        return failed(f"these cells don't hold what was written: {', '.join(wrong[:6])}")
    payroll = after.tab.payroll
    summed = sum(1 for r in after.rows if r.counted and r.salary is not None)
    expected = plan.payroll_after
    if payroll is None or expected is None or abs(payroll - expected) > (summed + 1) / 2:
        return failed(f"PAYROLL reads {_money(payroll)}, not the expected {_money(expected)}")
    if after.plan.changes:
        return failed("the tab still differs from your roster")
    return WriteResult(
        "written",
        f"Written: {written} cells on {after.tab.name}. PAYROLL is now {_money(payroll)}, "
        "as expected.",
        attempted=written,
        applied="yes",
        payroll=payroll,
    )


def _money(amount: int | None) -> str:
    return "unknown" if amount is None else f"${amount:,}"
