"""The owner's-tab update from the stored pieces (SPEC §4a "Writing"): which tab,
which rows, which cap hits, and the digest the confirm checks."""

from dataclasses import replace
from typing import Any

from fha.domain.sheet_update import Change
from fha.services.league_sheet import bind_tab, tab_reports
from fha.services.sheet_update import build_update, prepare_update
from fha.sources.league_sheet.parse import parse_sheet
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
