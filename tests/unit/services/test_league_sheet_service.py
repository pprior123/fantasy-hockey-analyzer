"""The league sheet in the app: store, bind tabs, match rows in roster scope, report."""

import json
from typing import Any

import pytest

from fha.domain.matcher import NO_ALIASES, Aliases, Status
from fha.services.league_sheet import (
    COLLECTION,
    LATEST,
    LeagueSheetServiceError,
    RosteredSalary,
    bind_tab,
    confirm_row,
    decode_sheet,
    encode_sheet,
    load_league_sheet,
    load_tab_bindings,
    match_tab,
    payrolls,
    read_league_sheet,
    rostered_salaries,
    suggest_bindings,
    tab_reports,
)
from fha.sources.league_sheet.parse import parse_sheet
from fha.sources.league_sheet.source import FakeLeagueSheet
from fha.sources.yahoo.models import Player, RosterEntry, Team
from fha.storage.memory import InMemoryRepository
from tests.unit.league_sheet import sheets as s

T0 = 1_800_000_000.0


class Clock:
    def now(self) -> float:
        return T0


def yplayer(pid: str, name: str, team: str = "TB", pos: str = "C") -> Player:
    ptype = "G" if pos == "G" else "P"
    return Player(f"465.p.{pid}", pid, name, team, pos, (pos,), ptype)


ONE = Team(
    "465.l.8076.t.1",
    "Team 1",
    True,
    (
        RosterEntry(yplayer("1", "Ada Knight"), "C"),
        RosterEntry(yplayer("2", "Frederik Andersen", "Car", "G"), "G"),
        RosterEntry(yplayer("3", "Jake Oettinger", "Dal", "G"), "BN"),
        RosterEntry(yplayer("4", "Bo Stone", "Edm", "D"), "IR"),
        RosterEntry(yplayer("5", "Cy Wall", "Bos", "LW"), "LW"),
    ),
)
TWO = Team(
    "465.l.8076.t.2",
    "Team 2",
    False,
    (
        RosterEntry(yplayer("11", "Dee Hill", "Ott"), "C"),
        RosterEntry(yplayer("12", "Eve Moss", "Sea", "D"), "D"),
    ),
)

TAB_ONE = s.TeamTab(
    "Pinecone",
    [
        s.Player("Ada Knight", "C", "TBL", 5_000_000),
        s.Player("Andersen", "G", "CAR", 3_400_000),  # surname only
        s.Player("Oetterger", "G", "DAL", 8_250_000),  # a typo: review only
        s.Player("Zed Nobody", "C", "TBL", 900_000),  # not on the Yahoo roster
        s.Player("Cy Wall", "LW", "BOS", "???"),  # no numeric salary
    ],
    below=[s.IRRow("IR", s.Player("Bo Stone", "D", "EDM", 2_000_000))],
)
TAB_TWO = s.TeamTab(
    "Maple",
    [s.Player("Dee Hill", "C", "OTT", 1_000_000), s.Player("Moss, Eve", "D", "SEA", 2_000_000)],
)


def sheet() -> Any:
    return parse_sheet(s.grid(TAB_ONE, TAB_TWO))


async def test_the_parsed_sheet_is_stored_and_read_back() -> None:
    repo = InMemoryRepository()
    assert await load_league_sheet(repo) is None
    parsed = sheet()
    got = await read_league_sheet(repo, FakeLeagueSheet(parsed), Clock())
    assert got == parsed
    assert await load_league_sheet(repo) == (parsed, T0)
    assert decode_sheet(encode_sheet(parsed)) == parsed


async def test_no_contact_detail_is_ever_stored() -> None:
    repo = InMemoryRepository()
    await read_league_sheet(repo, FakeLeagueSheet(sheet()), Clock())
    stored = json.dumps(repo.collections)
    for secret in s.CONTACT:
        assert secret not in stored


def test_tabs_are_suggested_by_roster_overlap() -> None:
    assert suggest_bindings(sheet(), [ONE, TWO], NO_ALIASES) == {
        "Pinecone": ONE.team_key,
        "Maple": TWO.team_key,
    }


def test_no_suggestion_without_a_clear_majority_or_for_a_team_twice() -> None:
    parsed = parse_sheet(s.grid(TAB_TWO, s.TeamTab("Copy", TAB_TWO.players)))
    assert suggest_bindings(parsed, [ONE, TWO], NO_ALIASES) == {"Maple": None, "Copy": None}
    stranger = parse_sheet(s.grid(s.TeamTab("Odd", [s.Player("Q Q"), s.Player("Dee Hill")])))
    assert suggest_bindings(stranger, [ONE, TWO], NO_ALIASES) == {"Odd": TWO.team_key}  # 1 of 2
    lonely = parse_sheet(s.grid(s.TeamTab("Odd", [s.Player("Q Q"), s.Player("R R")])))
    assert suggest_bindings(lonely, [ONE, TWO], NO_ALIASES) == {"Odd": None}
    assert suggest_bindings(lonely, [], NO_ALIASES) == {"Odd": None}


async def test_binding_a_tab_and_one_team_per_tab() -> None:
    repo = InMemoryRepository()
    await bind_tab(repo, "Pinecone", ONE.team_key)
    with pytest.raises(LeagueSheetServiceError, match="already bound to tab 'Pinecone'"):
        await bind_tab(repo, "Maple", ONE.team_key)
    await bind_tab(repo, "Pinecone", ONE.team_key)  # rebinding the same is fine
    assert await load_tab_bindings(repo) == {"Pinecone": ONE.team_key}
    await bind_tab(repo, "Pinecone", None)
    assert await load_tab_bindings(repo) == {}


def test_rows_match_within_the_roster_and_discrepancies_are_reported() -> None:
    tab = sheet().tab("Pinecone")
    report = match_tab(tab, ONE, NO_ALIASES, {})
    by_name = {m.row.name: m for m in report.rows}
    assert by_name["Ada Knight"].player_id == "1"
    assert (by_name["Andersen"].player_id, by_name["Andersen"].how) == ("2", "auto:surname")
    oet = by_name["Oetterger"]
    assert oet.player_id is None
    assert oet.result is not None
    assert oet.result.status is Status.REVIEW
    assert oet.result.candidates[0].candidate.name == "Jake Oettinger"
    assert by_name["Bo Stone"].row.counted is False
    assert [p.name for p in report.missing_from_tab] == ["Jake Oettinger"]
    assert [r.name for r in report.not_on_roster] == ["Oetterger", "Zed Nobody"]
    # Counted, matched, numeric: Knight + Andersen (Wall's is "???", Stone is IR).
    assert report.matched_counted_total == 8_400_000
    assert report.payroll == tab.payroll
    assert report.payroll_differs  # the unmatched counted rows are missing from the sum
    assert report.has_discrepancies


def test_a_clean_tab_has_no_discrepancies() -> None:
    report = match_tab(sheet().tab("Maple"), TWO, NO_ALIASES, {})
    assert [m.player_id for m in report.rows] == ["11", "12"]
    assert not report.has_discrepancies
    assert report.payroll == 3_000_000


def test_an_unbound_tab_has_no_payroll_and_no_discrepancies_yet() -> None:
    report = match_tab(sheet().tab("Maple"), None, NO_ALIASES, {})
    assert report.team_key is None
    assert report.payroll is None
    assert not report.has_discrepancies
    assert all(m.player_id is None for m in report.rows)


def test_an_unrecognized_tab_has_no_payroll() -> None:
    broken = s.TeamTab("Broken", [s.Player("Dee Hill")], payroll_formula="=F7+F8")
    tab = parse_sheet(s.grid(broken)).tab("Broken")
    assert tab is not None
    assert tab.status == "unrecognized"
    assert match_tab(tab, TWO, NO_ALIASES, {}).payroll is None


def test_two_rows_on_one_player_trust_neither() -> None:
    twice = s.TeamTab("Twice", [s.Player("Dee Hill", "C"), s.Player("Hill", "C")])
    report = match_tab(parse_sheet(s.grid(twice)).tab("Twice"), TWO, NO_ALIASES, {})
    assert [m.player_id for m in report.rows] == [None, None]
    assert [p.name for p in report.missing_from_tab] == ["Dee Hill", "Eve Moss"]


def test_aliases_apply_in_roster_scope() -> None:
    odd = s.TeamTab("Odd", [s.Player("Evie Mossberg", "D")])
    aliases = Aliases().with_alias("Eve Moss", "Evie Mossberg")
    report = match_tab(parse_sheet(s.grid(odd)).tab("Odd"), TWO, aliases, {})
    assert report.rows[0].player_id == "12"


async def test_reports_persist_automatic_matches_once() -> None:
    repo = InMemoryRepository()
    await bind_tab(repo, "Pinecone", ONE.team_key)
    first = await tab_reports(repo, sheet(), [ONE, TWO], NO_ALIASES)
    stored = await repo.get(COLLECTION, "row_bindings")
    assert stored is not None
    assert set(stored["entries"]["Pinecone"]) == {
        "ada knight|F",
        "andersen|G",
        "cy wall|F",
        "bo stone|D",
    }
    again = await tab_reports(repo, sheet(), [ONE, TWO], NO_ALIASES)
    assert again == first
    assert [r.team_key for r in first] == [ONE.team_key, None]


async def test_a_confirmed_row_keeps_its_player_and_rebinding_the_tab_forgets_rows() -> None:
    repo = InMemoryRepository()
    with pytest.raises(LeagueSheetServiceError, match="isn't bound"):
        await confirm_row(repo, "Pinecone", "oetterger|G", "3")
    await bind_tab(repo, "Pinecone", ONE.team_key)
    await confirm_row(repo, "Pinecone", "oetterger|G", "3")
    [report, _] = await tab_reports(repo, sheet(), [ONE, TWO], NO_ALIASES)
    oet = next(m for m in report.rows if m.row.name == "Oetterger")
    assert (oet.player_id, oet.how) == ("3", "confirmed")
    assert report.missing_from_tab == ()
    await bind_tab(repo, "Pinecone", TWO.team_key)
    stored = await repo.get(COLLECTION, "row_bindings")
    assert stored is not None
    assert "Pinecone" not in stored["entries"]


async def test_a_bound_player_who_left_the_roster_is_matched_again() -> None:
    repo = InMemoryRepository()
    await bind_tab(repo, "Maple", TWO.team_key)
    await confirm_row(repo, "Maple", "dee hill|F", "999")  # no longer on the roster
    [_, report] = await tab_reports(repo, sheet(), [ONE, TWO], NO_ALIASES)
    assert report.rows[0].player_id == "11"
    stored = await repo.get(COLLECTION, "row_bindings")
    assert stored is not None
    assert stored["entries"]["Maple"]["dee hill|F"] == {"player_id": "11", "how": "auto:exact"}


async def test_salaries_and_payrolls_for_the_app() -> None:
    repo = InMemoryRepository()
    await bind_tab(repo, "Pinecone", ONE.team_key)
    await bind_tab(repo, "Maple", TWO.team_key)
    reports = await tab_reports(repo, sheet(), [ONE, TWO], NO_ALIASES)
    salaries = rostered_salaries(reports)
    assert salaries["1"] == RosteredSalary(5_000_000, True, "Pinecone")
    assert salaries["4"] == RosteredSalary(2_000_000, False, "Pinecone")  # IR: not counted
    assert salaries["5"] == RosteredSalary(None, True, "Pinecone")  # "???"
    assert "3" not in salaries  # the typo row awaits review
    assert payrolls(reports) == {
        ONE.team_key: sheet().tab("Pinecone").payroll,
        TWO.team_key: 3_000_000,
    }


async def test_stored_sheet_document_shape() -> None:
    repo = InMemoryRepository()
    await read_league_sheet(repo, FakeLeagueSheet(sheet()), Clock())
    doc = await repo.get(COLLECTION, LATEST)
    assert doc is not None
    assert set(doc) == {"read_at", "sheet"}
