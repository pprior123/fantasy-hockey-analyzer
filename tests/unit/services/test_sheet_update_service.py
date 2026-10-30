"""The owner's-tab update from the stored pieces (SPEC §4a "Writing"): which tab,
which rows, which cap hits, and the digest the confirm checks."""

from dataclasses import replace
from typing import Any

from fha.domain.sheet_update import Change
from fha.services.free_agents import set_aav_override
from fha.services.league_sheet import COLLECTION, ROW_BINDINGS, bind_tab, tab_reports
from fha.services.sheet_update import build_update, prepare_update, write_update
from fha.sources.league_sheet.grid import Grid, Tab
from fha.sources.league_sheet.models import LeagueSheetError
from fha.sources.league_sheet.parse import parse_sheet
from fha.sources.league_sheet.writer import FakeLeagueSheetWriter
from fha.sources.yahoo.models import RosterSlot
from fha.storage.memory import InMemoryRepository
from tests.unit.league_sheet import sheets as s
from tests.unit.services.snapshots import synthetic_snapshot
from tests.unit.services.test_league_sheet_service import NO_ALIASES, ONE, TAB_ONE, TAB_TWO, TWO

SLOTS = (RosterSlot("C", 3, True), RosterSlot("BN", 9, False), RosterSlot("IR+", 3, False))


async def snapshot(teams: Any = (ONE, TWO)) -> Any:
    snap = await synthetic_snapshot(available=0)
    return replace(snap, teams=teams, settings=replace(snap.settings, roster_slots=SLOTS))


async def bound_repo() -> InMemoryRepository:
    repo = InMemoryRepository()
    await bind_tab(repo, "Pinecone", ONE.team_key)
    await bind_tab(repo, "Maple", TWO.team_key)
    return repo


async def update(sheet: Any = None, teams: Any = (ONE, TWO), ticked: Any = (), aav: Any = None):  # type: ignore[no-untyped-def]
    sheet = sheet if sheet is not None else parse_sheet(s.grid(TAB_ONE, TAB_TWO))
    snap = await snapshot(teams)
    reports = await tab_reports(await bound_repo(), sheet, snap.teams, NO_ALIASES)
    return build_update(snap, sheet, reports, aav or {"3": 8_000_000}, ticked)


async def test_the_owners_tab_is_planned_with_the_review_row_waiting() -> None:
    u = await update()
    assert u.problem is None
    assert u.tab is not None
    assert u.tab.name == "Pinecone"
    assert u.plan is not None
    # Rows 7-11: Ada, Andersen, Oetterger (review), Zed (not on the roster), Cy.
    assert [(r.row, r.status) for r in u.rows] == [
        (7, "matched"),
        (8, "matched"),
        (9, "review"),
        (10, "unmatched"),
        (11, "matched"),
        (12, "matched"),
    ]
    assert [r.name for r in u.reviews] == ["Oetterger"]
    assert "Jake Oettinger" in u.reviews[0].candidates
    assert not u.plan.ok
    assert any("row 9 (Oetterger) awaits match review" in r for r in u.plan.refusals)


async def test_ticking_the_review_row_drops_it_and_adds_the_missing_player() -> None:
    u = await update(ticked=(9, 7, 99))  # only review rows can be ticked
    assert u.ticked == (9,)
    assert u.plan is not None
    assert u.plan.ok, u.plan.refusals
    assert set(u.plan.changes) == {
        Change("drop", "Oetterger", 9, None),
        Change("drop", "Zed Nobody", 10, None),
        Change("add", "Oettinger, Jake", None, 9),
    }
    assert u.layout is not None
    assert (u.layout.first_row, u.layout.last_row, u.layout.header_row) == (7, 11, 6)
    # Oetterger's $8.25M and Zed's $0.9M go; Oettinger's $8M comes in.
    assert u.plan.payroll_after == u.plan.payroll - 8_250_000 - 900_000 + 8_000_000


async def test_the_incoming_cap_hit_comes_from_the_free_agent_salaries() -> None:
    u = await update(ticked=(9,), aav={"99": 1})
    assert u.plan is not None
    assert "Jake Oettinger's cap hit is unknown" in " ".join(u.plan.refusals)


async def test_the_digest_follows_the_tab_the_edits_and_the_ticks() -> None:
    a, b = await update(ticked=(9,)), await update(ticked=(9,))
    assert a.digest == b.digest
    assert len(a.digest) == 64
    assert (await update()).digest != a.digest
    assert (await update(ticked=(9,), aav={"3": 7_000_000})).digest != a.digest


async def test_slot_counts_come_from_the_league_settings() -> None:
    u = await update(ticked=(9,))
    assert u.plan is not None
    assert u.plan.ok
    snap = await snapshot()
    tight = replace(
        snap.settings, roster_slots=(RosterSlot("C", 3, True), RosterSlot("IR+", 1, False))
    )
    sheet = parse_sheet(s.grid(TAB_ONE, TAB_TWO))
    reports = await tab_reports(await bound_repo(), sheet, snap.teams, NO_ALIASES)
    tight_update = build_update(replace(snap, settings=tight), sheet, reports, {"3": 1}, (9,))
    assert tight_update.plan is not None
    assert "more than the league's 3" in " ".join(tight_update.plan.refusals)


async def test_problems_before_any_plan() -> None:
    nobody = replace(ONE, is_mine=False)
    assert (await update(teams=(nobody, TWO))).problem == "Your team isn't in the Yahoo data."

    snap = await snapshot()
    sheet = parse_sheet(s.grid(TAB_ONE, TAB_TWO))
    reports = await tab_reports(InMemoryRepository(), sheet, snap.teams, NO_ALIASES)
    assert build_update(snap, sheet, reports, {}).problem == (
        "No tab is bound to your team yet (Admin, Tabs and Yahoo teams)."
    )

    broken = replace(TAB_ONE, payroll_formula="=F7+F8")
    u = await update(sheet=parse_sheet(s.grid(broken, TAB_TWO)))
    assert u.problem is not None
    assert u.problem.startswith("Your tab 'Pinecone' can't be read: PAYROLL formula")

    parsed = parse_sheet(s.grid(TAB_ONE, TAB_TWO))
    old = replace(parsed, tabs=tuple(replace(t, name_column=None) for t in parsed.tabs))
    assert (await update(sheet=old)).problem == (
        "The stored sheet predates this feature: read the sheet again."
    )


async def test_prepare_reads_the_stored_free_agent_salaries() -> None:
    repo = await bound_repo()
    sheet = parse_sheet(s.grid(TAB_ONE, TAB_TWO))
    u = await prepare_update(repo, await snapshot(), sheet, ticked=(9,))
    assert u.plan is not None
    assert "Jake Oettinger's cap hit is unknown" in " ".join(u.plan.refusals)  # none imported


# ---------------------------------------------------------------- the write


AFTER_ONE = s.TeamTab(
    "Pinecone",
    [
        s.Player("Ada Knight", "C", "TBL", 5_000_000),
        s.Player("Andersen", "G", "CAR", 3_400_000),
        s.Player("Oettinger, Jake", "G", "Dal", 8_000_000),  # written by the app
        None,  # Zed's row, cleared
        s.Player("Cy Wall", "LW", "BOS", "???"),
    ],
    below=[s.IRRow("IR", s.Player("Bo Stone", "D", "EDM", 2_000_000))],
)


def live(*tabs: s.TeamTab, editable: tuple[str, ...] = ("Pinecone",), unknown: bool = False) -> Any:
    """A sheet as the API reads it: tab IDs, and every tab but ``editable`` locked."""
    built = s.grid(*tabs)
    return parse_sheet(
        Grid(
            tuple(
                Tab(t.title, t.cells, i, None if unknown else t.title not in editable)
                for i, t in enumerate(built)
            )
        )
    )


class Reads:
    """Each fetch returns the next sheet (or raises it, if it's an error)."""

    def __init__(self, *sheets: Any) -> None:
        self.sheets = list(sheets)

    async def fetch(self) -> Any:
        sheet = self.sheets.pop(0)
        if isinstance(sheet, Exception):
            raise sheet
        return sheet


class Clock:
    def now(self) -> float:
        return 1_800_000_000.0


async def confirm(
    *reads: Any,
    ticked: Any = (9,),
    digest: str | None = None,
    writer: Any = None,
    teams: Any = (ONE, TWO),
):  # type: ignore[no-untyped-def]
    repo = await bound_repo()
    await set_aav_override(repo, "3", 8_000_000)
    snap = await snapshot(teams)
    if digest is None:
        digest = (await prepare_update(repo, snap, reads[0], ticked)).digest
    writer = writer or FakeLeagueSheetWriter()
    result = await write_update(repo, Clock(), Reads(*reads), writer, snap, digest, ticked)
    return result, writer, repo


BEFORE = live(TAB_ONE, TAB_TWO)
AFTER = live(AFTER_ONE, TAB_TWO)


async def test_a_confirmed_plan_is_written_bound_and_read_back() -> None:
    result, writer, repo = await confirm(BEFORE, AFTER)
    assert result.ok, result.message
    assert (
        result.message == "Written: 7 cells on Pinecone. PAYROLL is now $16,400,000, as expected."
    )
    [(tab, edits)] = writer.writes
    assert tab == "Pinecone"
    assert {f"{e.column}{e.row}": e.new for e in edits} == {
        "C9": "Oettinger, Jake",
        "E9": "Dal",
        "F9": 8_000_000,
        "C10": None,
        "D10": None,
        "E10": None,
        "F10": None,
    }
    bindings = (await repo.get(COLLECTION, ROW_BINDINGS) or {})["entries"]["Pinecone"]
    assert {"player_id": "3", "how": "written"} in bindings.values()


async def test_a_changed_tab_or_roster_writes_nothing() -> None:
    result, writer, _ = await confirm(BEFORE, digest="0" * 64)
    assert not result.ok
    assert "changed since the preview" in result.message
    assert writer.writes == []


async def test_another_editable_tab_writes_nothing() -> None:
    sheet = live(TAB_ONE, TAB_TWO, editable=("Pinecone", "Maple"))
    result, writer, _ = await confirm(sheet)
    assert result.message == (
        "Nothing was written: the app can still edit 1 other tab(s) (Maple). "
        "Protect them first (Data → Protect sheets and ranges)."
    )
    assert writer.writes == []
    many = live(TAB_ONE, TAB_TWO, editable=("Pinecone", "Maple", "Summary"))
    assert "2 other tab(s) (Summary, Maple)" in (await confirm(many))[0].message


async def test_unknown_protections_write_nothing() -> None:
    result, writer, _ = await confirm(live(TAB_ONE, TAB_TWO, unknown=True))
    assert result.message == "Nothing was written: the sheet's protections couldn't be checked."
    assert writer.writes == []


async def test_a_refused_plan_writes_nothing() -> None:
    result, writer, _ = await confirm(BEFORE, ticked=())
    assert result.message.startswith("Nothing was written: row 9 (Oetterger) awaits match review")
    assert writer.writes == []


async def test_a_tab_that_already_matches_needs_nothing() -> None:
    result, writer, _ = await confirm(AFTER, ticked=())
    assert (result.ok, result.written) == (True, 0)
    assert result.message == "Nothing to write: Pinecone already matches your roster."
    assert writer.writes == []


async def test_no_plan_at_all_writes_nothing() -> None:
    nobody = replace(ONE, is_mine=False)
    result, _, _ = await confirm(BEFORE, teams=(nobody, TWO), digest="x")
    assert result.message == "Nothing was written: Your team isn't in the Yahoo data."


async def test_failed_reads_and_writes_are_said_plainly() -> None:
    down = LeagueSheetError("Sheets API answered HTTP 503")
    repo = await bound_repo()
    snap = await snapshot()
    result = await write_update(repo, Clock(), Reads(down), FakeLeagueSheetWriter(), snap, "x")
    assert result.message == (
        "Nothing was written: the sheet couldn't be read (Sheets API answered HTTP 503)."
    )
    result, _, _ = await confirm(BEFORE, writer=FakeLeagueSheetWriter(down))
    assert result.message == (
        "The write failed and nothing was changed (Sheets API answered HTTP 503)."
    )


async def test_a_read_back_that_fails_or_differs_is_reported() -> None:
    down = LeagueSheetError("HTTP 503")
    result, _, _ = await confirm(BEFORE, down)
    assert (
        result.message
        == "Written (7 cells), but the tab couldn't be read back (HTTP 503). Check the tab."
    )
    assert (result.ok, result.written) == (False, 7)

    result, _, _ = await confirm(BEFORE, BEFORE)  # Google kept the old values
    assert result.message.startswith(
        "Written (7 cells), but these cells don't hold what was written: C9, E9, F9, C10"
    )

    wrong_payroll = replace(AFTER_ONE, payroll_value=16_400_009)
    result, _, _ = await confirm(BEFORE, live(wrong_payroll, TAB_TWO))
    assert "PAYROLL reads $16,400,009, not the expected $16,400,000" in result.message

    ghost = replace(AFTER_ONE, below=[*AFTER_ONE.below, s.IRRow("IR+", s.Player("Gus Ghost"))])
    result, _, _ = await confirm(BEFORE, live(ghost, TAB_TWO))
    assert (
        result.message
        == "Written (7 cells), but the tab still differs from your roster. Check the tab."
    )

    broken = replace(AFTER_ONE, payroll_formula="=F7+F8")
    result, _, _ = await confirm(BEFORE, live(broken, TAB_TWO))
    assert result.message.startswith(
        "Written (7 cells), but the tab can't be planned now (Your tab"
    )


async def test_a_new_row_that_keeps_some_cells_is_bound_by_what_the_row_holds() -> None:
    from fha.domain.sheet_update import Member, TabLayout, TabRow, plan_update
    from fha.services.free_agents import row_key
    from fha.services.sheet_update import _added_keys

    layout = TabLayout("C", "D", "E", "F", 7, 9, 6, (), ())
    rows = (TabRow(7, "Able, Ann", "C", "TOR", 1, True, "unmatched"),)
    member = Member("9", "Ann", "Able", "Ann Able", "C", "TB", False, 2)
    plan = plan_update(
        rows, layout, (member,), payroll=1, tab_cap=10, cap=10, active_slots=5, ir_slots=1
    )
    assert {e.column for e in plan.edits} == {"E", "F"}  # same name and position
    assert _added_keys(plan, rows, layout) == {row_key("Able, Ann", "C"): "9"}


async def test_a_guard_refusal_writes_nothing(monkeypatch: Any) -> None:
    from fha.domain.sheet_update import GuardError
    from fha.services import sheet_update

    def refuse(*args: Any) -> None:
        raise GuardError("edit outside the allowed cells: C40")

    monkeypatch.setattr(sheet_update, "guard", refuse)
    result, writer, _ = await confirm(BEFORE)
    assert result.message == "Nothing was written: edit outside the allowed cells: C40."
    assert writer.writes == []


async def test_a_storage_failure_after_the_write_still_says_it_was_written(
    monkeypatch: Any,
) -> None:
    from fha.services import sheet_update
    from fha.storage.repository import RepositoryError

    async def down(*args: Any) -> None:
        raise RepositoryError("project fha-prod refused", detail="HTTP 503")

    monkeypatch.setattr(sheet_update, "bind_written_rows", down)
    result, writer, _ = await confirm(BEFORE, AFTER)
    assert len(writer.writes) == 1
    assert (result.ok, result.written) == (False, 7)
    assert result.message == (
        "Written (7 cells), but the app's storage failed afterwards. "
        "Check the tab, then read the sheet again (Admin)."
    )
    assert "fha-prod" not in result.message
