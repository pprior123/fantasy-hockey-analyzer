"""The demo's built-in salaries, stored on first use: the sheet read and bound, rows imported."""

import asyncio
from dataclasses import replace
from typing import Any

from fha.domain.matcher import NO_ALIASES, Status
from fha.services.demo import DemoSalaries, seed_demo_salaries
from fha.services.free_agents import import_free_agent_salaries, pending_reviews
from fha.services.league_sheet import load_league_sheet, load_tab_bindings, read_league_sheet
from fha.services.league_view import load_salaries
from fha.sources.demo_salaries import CAP, DemoLeagueSheet, demo_free_agent_rows
from fha.sources.league_sheet.models import ParsedSheet
from fha.sources.league_sheet.source import FakeLeagueSheet
from fha.sources.puckpedia import SalaryRow
from fha.sources.yahoo.demo import demo_snapshot
from fha.storage.memory import InMemoryRepository
from tests.unit.services.test_free_agents import CountingRepository

SNAPSHOT = demo_snapshot()
T0 = 1_800_000_000.0


class Clock:
    def now(self) -> float:
        return T0


async def seed(repo: InMemoryRepository, sheet: Any = None, rows: Any = None) -> None:
    await seed_demo_salaries(
        repo,
        Clock(),
        SNAPSHOT,
        sheet if sheet is not None else DemoLeagueSheet(SNAPSHOT),
        rows if rows is not None else demo_free_agent_rows(SNAPSHOT),
    )


async def test_an_empty_store_gets_the_sheet_read_every_tab_bound_and_the_rows_imported() -> None:
    repo = InMemoryRepository()
    await seed(repo)
    stored = await load_league_sheet(repo)
    assert stored is not None
    assert stored[1] == T0
    bindings = await load_tab_bindings(repo)
    # Each tab is bound to the team it was written for (by roster overlap, not by name).
    assert [bindings[t.name] for t in stored[0].tabs] == [t.team_key for t in SNAPSHOT.teams]

    salaries, _ = await load_salaries(repo, SNAPSHOT)
    assert salaries.cap == CAP
    assert set(salaries.payrolls) == {t.team_key for t in SNAPSHOT.teams}
    assert sum(p is not None and p > CAP for p in salaries.payrolls.values()) == 1
    rostered = [e.player.player_id for t in SNAPSHOT.teams for e in t.roster]
    assert len(salaries.rostered) == len(rostered) - 3  # all but the variants
    available = [p.player_id for p in SNAPSHOT.available]
    assert 0.7 * len(available) <= len(set(salaries.free_agents) & set(available))


async def test_the_spelling_variants_wait_in_the_match_review() -> None:
    repo = InMemoryRepository()
    await seed(repo)
    _, reports = await load_salaries(repo, SNAPSHOT)
    waiting = [r for rep in reports for r in rep.not_on_roster]
    assert len(waiting) == 3
    assert all(r.name[1:3] == ". " for r in waiting)
    under_review = [m for rep in reports for m in rep.rows if m.player_id is None]
    assert {m.result.status for m in under_review if m.result} <= {Status.REVIEW, Status.AMBIGUOUS}
    # Those tabs' payrolls still come from the tab, but no longer add up; the rest do.
    assert [rep.payroll_differs for rep in reports] == [bool(rep.not_on_roster) for rep in reports]
    assert sum(rep.payroll_differs for rep in reports) == 3


async def test_seeding_again_writes_nothing() -> None:
    repo = CountingRepository()
    await seed(repo)
    writes = repo.writes
    await seed(repo)
    assert repo.writes == writes


async def test_a_sheet_the_owner_already_read_is_kept_unbound_and_untouched() -> None:
    repo = InMemoryRepository()
    theirs = ParsedSheet(cap=1, cap_source="'Summary'!B3", tabs=())
    await read_league_sheet(repo, FakeLeagueSheet(theirs), Clock())
    demo = FakeLeagueSheet(ParsedSheet(cap=2, cap_source=None, tabs=()))
    await seed(repo, sheet=demo)
    stored = await load_league_sheet(repo)
    assert stored is not None
    assert stored[0] == theirs
    assert demo.fetches == 0
    assert await load_tab_bindings(repo) == {}


async def test_free_agent_rows_the_owner_already_imported_are_kept() -> None:
    repo = InMemoryRepository()
    player = SNAPSHOT.available[0]
    theirs = [SalaryRow(player.name, player.display_position, None, 5_000_000, 2)]
    await import_free_agent_salaries(repo, theirs, SNAPSHOT.pool, NO_ALIASES)
    await seed(repo)
    salaries, _ = await load_salaries(repo, SNAPSHOT)
    assert salaries.free_agents == {player.player_id: 5_000_000}
    assert await pending_reviews(repo, SNAPSHOT.pool, NO_ALIASES) == []


class ReadCountingRepository(CountingRepository):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        self.reads += 1
        return await super().get(collection, doc_id)


class SlowSheet(FakeLeagueSheet):
    async def fetch(self) -> ParsedSheet:
        await asyncio.sleep(0)  # the other first requests arrive meanwhile
        return await super().fetch()


async def test_demo_salaries_seed_once_even_when_asked_concurrently() -> None:
    parsed = await DemoLeagueSheet(SNAPSHOT).fetch()
    rows = demo_free_agent_rows(SNAPSHOT)

    async def run(times: int) -> tuple[ReadCountingRepository, SlowSheet, DemoSalaries]:
        repo, sheet = ReadCountingRepository(), SlowSheet(parsed)
        once = DemoSalaries(repo, Clock(), SNAPSHOT, sheet, rows)
        await asyncio.gather(*(once.ensure() for _ in range(times)))
        return repo, sheet, once

    alone, _, _ = await run(1)
    repo, sheet, once = await run(3)
    assert sheet.fetches == 1
    assert (repo.reads, repo.writes) == (alone.reads, alone.writes)  # the waiters didn't seed
    await once.ensure()
    assert (repo.reads, repo.writes) == (alone.reads, alone.writes)


async def test_a_tab_no_team_fits_is_left_for_the_owner_to_bind() -> None:
    repo = InMemoryRepository()
    parsed = await DemoLeagueSheet(SNAPSHOT).fetch()
    nobody = replace(parsed.tabs[0].rows[0], name="Nobody Rostered")
    stranger = replace(parsed.tabs[0], name="Somebody else", rows=(nobody,))
    await seed(repo, sheet=FakeLeagueSheet(replace(parsed, tabs=(*parsed.tabs, stranger))))
    bindings = await load_tab_bindings(repo)
    assert len(bindings) == len(SNAPSHOT.teams)
    assert "Somebody else" not in bindings
