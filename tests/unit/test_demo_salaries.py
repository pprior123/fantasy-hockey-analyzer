"""The demo league's built-in salaries: a synthetic league sheet and free-agent rows."""

import asyncio
import re
from dataclasses import replace

import pytest

from fha.domain.names import canonical_team, normalize_name
from fha.sources.demo_salaries import (
    CAP,
    ENTRY_LEVEL_MAX,
    MINIMUM,
    PAYROLL_TARGETS,
    DemoLeagueSheet,
    demo_free_agent_rows,
    demo_sheet_grid,
)
from fha.sources.league_sheet.models import ParsedSheet
from fha.sources.league_sheet.parse import parse_sheet
from fha.sources.yahoo.demo import demo_snapshot

SNAPSHOT = demo_snapshot()
POSITION_LETTERS = {"C", "L", "R", "D", "G"}


@pytest.fixture(scope="module")
def sheet() -> ParsedSheet:
    return parse_sheet(demo_sheet_grid(SNAPSHOT))


def test_every_team_has_a_recognized_tab_under_the_summary_cap(sheet: ParsedSheet) -> None:
    assert len(sheet.tabs) == len(SNAPSHOT.teams) == len(PAYROLL_TARGETS)
    assert [t.status for t in sheet.tabs] == ["ok"] * len(sheet.tabs)
    assert (sheet.cap, sheet.cap_source) == (CAP, "'Summary'!B3")
    assert sheet.other_tabs == ("Summary",)
    assert sheet.cap_mismatches == ()


def test_each_tab_lists_its_teams_roster_with_ir_rows_below(sheet: ParsedSheet) -> None:
    for tab, team in zip(sheet.tabs, SNAPSHOT.teams, strict=True):
        counted = [e.player for e in team.roster if not e.in_ir_slot]
        ir = [e for e in team.roster if e.in_ir_slot]
        assert len(tab.counted) == len(counted)
        assert [(r.name, r.ir) for r in tab.ir_rows] == [
            (e.player.name, e.selected_position) for e in ir
        ]
        # Every row names one of the team's players (a few are spelling variants).
        surnames = {p.name.split(" ", 1)[1] for p in counted}
        assert {r.name.split(" ", 1)[1] for r in tab.counted} == surnames


def test_payrolls_hit_their_targets_and_one_team_is_over_the_cap(sheet: ParsedSheet) -> None:
    for tab, target in zip(sheet.tabs, PAYROLL_TARGETS, strict=True):
        assert tab.payroll == tab.counted_total  # the PAYROLL formula sums the range
        assert tab.payroll is not None
        assert abs(tab.payroll - target) < 50_000
    assert sum(t.payroll is not None and t.payroll > CAP for t in sheet.tabs) == 1


def test_salaries_are_whole_thousands_from_the_minimum_with_entry_level_deals(
    sheet: ParsedSheet,
) -> None:
    salaries = [r.salary for t in sheet.tabs for r in t.rows]
    assert all(isinstance(s, int) and s >= MINIMUM and s % 1000 == 0 for s in salaries)
    cheap = sum(s is not None and s <= ENTRY_LEVEL_MAX * 1.25 for s in salaries)
    assert 0.05 * len(salaries) <= cheap <= 0.35 * len(salaries)
    assert max(s for s in salaries if s is not None) > 8_000_000  # stars cost more


def test_layouts_vary_as_the_real_sheet_does(sheet: ParsedSheet) -> None:
    ranges = [t.payroll_range for t in sheet.tabs]
    assert [t.salary_column for t in sheet.tabs].count("G") == 1
    assert {t.salary_column for t in sheet.tabs} == {"F", "G"}
    assert sum(r is not None and re.fullmatch(r"[FG]8:[FG]\d+", r) is not None for r in ranges) == 1


def test_rows_use_sheet_positions_and_known_team_codes(sheet: ParsedSheet) -> None:
    rows = [r for t in sheet.tabs for r in t.rows]
    assert {r.position for r in rows} <= POSITION_LETTERS
    assert all(canonical_team(r.team) is not None for r in rows)


def test_a_few_rows_are_spelling_variants_for_the_match_review(sheet: ParsedSheet) -> None:
    names = {p.name for t in SNAPSHOT.teams for p in (e.player for e in t.roster)}
    variants = {t.name: [r for r in t.rows if r.name not in names] for t in sheet.tabs}
    assert [len(v) for v in variants.values()] == [0, 1, 0, 0, 1, 0, 0, 1]
    rows = [r for v in variants.values() for r in v]
    assert all(re.fullmatch(r"[A-Z]\. \S+", r.name) and r.counted for r in rows)


def test_the_sheet_has_no_contact_details() -> None:
    texts = [
        c.value
        for tab in demo_sheet_grid(SNAPSHOT)
        for c in tab.cells.values()
        if isinstance(c.value, str)
    ]
    assert not [t for t in texts if "@" in t or re.search(r"\d{3}-\d{4}", t)]


def test_the_sheet_and_the_rows_are_deterministic() -> None:
    assert demo_sheet_grid(SNAPSHOT) == demo_sheet_grid(demo_snapshot())
    assert demo_free_agent_rows(SNAPSHOT) == demo_free_agent_rows(demo_snapshot())


def test_the_source_parses_the_built_in_sheet(sheet: ParsedSheet) -> None:
    source = DemoLeagueSheet(SNAPSHOT)
    assert asyncio.run(source.fetch()) == sheet
    assert repr(source) == "DemoLeagueSheet()"


def test_most_free_agents_have_a_row_written_as_puckpedia_does() -> None:
    rows = demo_free_agent_rows(SNAPSHOT)
    by_name = {p.name: p for p in SNAPSHOT.available}
    assert 0.75 * len(by_name) <= len(rows) <= 0.95 * len(by_name)
    for row in rows:
        last, first = row.name.split(", ")
        player = by_name[f"{first} {last}"]
        assert row.position == {"LW": "L", "RW": "R"}.get(
            player.eligible_positions[0], player.eligible_positions[0]
        )
        assert row.team == player.nhl_team.upper()
        assert row.aav >= MINIMUM
        assert row.aav % 1000 == 0
    assert [r.line for r in rows] == list(range(2, len(rows) + 2))  # as if from a file
    assert sum(r.aav == MINIMUM for r in rows) > len(rows) / 3
    assert len({normalize_name(r.name) for r in rows}) == len(rows)


def test_a_player_with_no_last_season_line_still_gets_a_salary() -> None:
    rookies = replace(SNAPSHOT, last_season_stats=None)
    tabs = parse_sheet(demo_sheet_grid(rookies)).tabs
    assert all(r.salary is not None and r.salary >= MINIMUM for t in tabs for r in t.rows)
