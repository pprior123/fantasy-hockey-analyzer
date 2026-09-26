"""The league-sheet parser against every layout variant seen in the real sheet
(synthetic tabs; DECISIONS "M3: league sheet layouts seen in the real sheet")."""

import pytest

from fha.sources.league_sheet.grid import Cell, Grid, Tab
from fha.sources.league_sheet.models import ParsedSheet, ParsedTab, SheetRow
from fha.sources.league_sheet.parse import parse_sheet
from tests.unit.league_sheet.sheets import (
    CAP,
    CONTACT,
    SUMMARY,
    IRRow,
    Player,
    TeamTab,
    grid,
    notes_tab,
    roster,
    summary_tab,
)


def one(team: TeamTab) -> ParsedTab:
    parsed = parse_sheet(grid(team))
    [tab] = parsed.tabs
    return tab


def assert_no_contact(obj: object) -> None:
    text = repr(obj)
    leaked = [c for c in CONTACT if c in text]
    assert leaked == []


# ---------------------------------------------------------------- the payroll formula


def test_a_direct_sum_with_the_header_just_above() -> None:
    players = roster(3)
    tab = one(TeamTab("Aces", players))
    assert tab.status == "ok"
    assert tab.reason is None
    assert (tab.salary_column, tab.payroll_range, tab.payroll_formula) == (
        "F",
        "F7:F9",
        "=SUM(F7:F9)",
    )
    assert tab.payroll == 1_000_000 + 1_250_000 + 1_500_000 == tab.counted_total
    assert tab.rows == (
        SheetRow(7, "Skater 1", "C", "TBL", 1_000_000, counted=True),
        SheetRow(8, "Skater 2", "C", "TBL", 1_250_000, counted=True),
        SheetRow(9, "Skater 3", "C", "TBL", 1_500_000, counted=True),
    )


def test_payroll_through_a_sum_of_one_cell_with_the_header_inside_the_range() -> None:
    # =SUM(C36) -> C36 = SUM(F6:F31), and the header row is the range's first row
    team = TeamTab("Bees", roster(4), start=6, header="in_range", payroll="sum_ref")
    team.headers["salary"] = "2025-2026 Salary"
    tab = one(team)
    assert tab.status == "ok"
    assert tab.payroll_formula == "=SUM(C13)"  # C13 holds =SUM(F6:F10)
    assert tab.payroll_range == "F6:F10"
    assert [r.row for r in tab.rows] == [7, 8, 9, 10]  # the header row isn't a player
    assert tab.payroll == tab.counted_total


def test_payroll_through_a_plain_reference_to_a_lower_case_sum() -> None:
    # =C40 -> C40 = sum(F7:F34)
    tab = one(TeamTab("Cats", roster(2), payroll="cell_ref"))
    assert tab.status == "ok"
    assert tab.payroll_range == "F7:F8"
    assert len(tab.counted) == 2


@pytest.mark.parametrize("formula", ["=SUM($F$7:$F$9)", "= sum( f7 : f9 )", "=SUM(F9:F7)"])
def test_absolute_references_spacing_case_and_reversed_ranges(formula: str) -> None:
    tab = one(TeamTab("Dogs", roster(3), payroll_formula=formula))
    assert (tab.status, tab.payroll_range) == ("ok", "F7:F9")


@pytest.mark.parametrize("label", ["PAYROLL", "Payroll:", " payroll "])
def test_the_payroll_label_is_matched_loosely(label: str) -> None:
    assert one(TeamTab("Eels", roster(1), payroll_label=label)).status == "ok"


# ---------------------------------------------------------------- columns and rows


def test_salary_in_g_position_in_e_and_other_seasons_ignored() -> None:
    team = TeamTab(
        "Foxes",
        roster(2),
        salary_col="G",
        pos_col="E",
        team_col="F",
        extra_salary_cols=("H", "I"),
        headers={"name": "NAME", "pos": "Position", "team": "Team", "salary": "2025-2026 Cap Hit"},
    )
    tab = one(team)
    assert (tab.status, tab.salary_column) == ("ok", "G")
    assert [(r.position, r.team, r.salary) for r in tab.rows] == [
        ("C", "TBL", 1_000_000),
        ("C", "TBL", 1_250_000),
    ]


@pytest.mark.parametrize("header_row", [5, 6])
def test_the_header_may_be_up_to_three_rows_above(header_row: int) -> None:
    tab = one(TeamTab("Gnus", roster(2), start=8, header=header_row))
    assert tab.status == "ok"
    assert len(tab.counted) == 2


def test_a_header_four_rows_above_is_not_found() -> None:
    tab = one(TeamTab("Hens", roster(2), start=9, header=5))
    assert tab.status == "unrecognized"
    assert tab.reason == "no NAME header in rows 6-9 above the payroll range"


def test_header_synonyms_last_first_names_and_city_teams() -> None:
    players = [
        Player("Tremblay, Luc", pos="Right Wing", team="Tampa"),
        Player("Olsen, Kai", pos="D", team="Los Angeles"),
    ]
    headers = {"name": "Player", "pos": "Pos.", "team": "NHL Team", "salary": "25- 26 Salary"}
    tab = one(TeamTab("Ibis", players, headers=headers))
    assert [(r.name, r.position, r.team) for r in tab.rows] == [
        ("Tremblay, Luc", "Right Wing", "Tampa"),
        ("Olsen, Kai", "D", "Los Angeles"),
    ]


def test_a_tab_without_position_and_team_headers_still_reads_names() -> None:
    headers = {"name": "NAME", "pos": "", "team": "", "salary": "25-26"}
    tab = one(TeamTab("Jays", roster(1), headers=headers))
    assert (tab.status, tab.rows[0].position, tab.rows[0].team) == ("ok", "", "")


def test_blank_rows_are_skipped_and_text_salaries_are_none() -> None:
    players = [
        Player("Ann Able", salary=900_000),
        None,
        Player("Ben Baker", salary="???"),
        Player("Cy Cole", salary=0),
        Player("Di Dunn", salary=886_666.0),
        None,
    ]
    tab = one(TeamTab("Kiwis", players))
    assert [(r.row, r.name, r.salary) for r in tab.rows] == [
        (7, "Ann Able", 900_000),
        (9, "Ben Baker", None),
        (10, "Cy Cole", 0),
        (11, "Di Dunn", 886_666),
    ]
    assert tab.payroll == tab.counted_total == 900_000 + 886_666  # SUM skips text


def test_a_whitespace_salary_with_no_name_is_a_blank_row() -> None:
    tab = one(TeamTab("Lynx", [Player("Ann Able"), Player("", salary=" ")]))
    assert [r.name for r in tab.rows] == ["Ann Able"]


def test_a_salary_without_a_name_is_kept_so_the_owner_can_see_it() -> None:
    tab = one(TeamTab("Moles", [Player("", salary=750_000)]))
    assert [(r.name, r.salary) for r in tab.rows] == [("", 750_000)]


def test_numbers_in_text_columns_read_as_their_digits() -> None:
    tab = one(TeamTab("Newts", [Player("Ann Able", pos=91, team=2.5)]))  # type: ignore[arg-type]
    assert (tab.rows[0].position, tab.rows[0].team) == ("91", "2.5")


def test_names_are_trimmed_and_inner_spaces_collapsed() -> None:
    tab = one(TeamTab("Owls", [Player("  Ann   Able ")]))
    assert tab.rows[0].name == "Ann Able"


# ---------------------------------------------------------------- IR rows and what's below


def test_ir_rows_below_the_range() -> None:
    below = [
        IRRow("IR", Player("Ike Injured", salary=5_000_000)),
        IRRow("IR", None),  # an empty IR slot
        IRRow("IR+", Player("Ula Longterm", salary="")),
    ]
    tab = one(TeamTab("Pumas", roster(2), below=below))
    assert tab.ir_rows == (
        SheetRow(9, "Ike Injured", "C", "TBL", 5_000_000, counted=False, ir="IR"),
        SheetRow(11, "Ula Longterm", "C", "TBL", None, counted=False, ir="IR+"),
    )
    assert tab.counted_total == tab.payroll  # IR salaries are not counted


def test_unlabelled_rows_below_the_range_are_ignored() -> None:
    below = [Player("Nat Notcounted"), IRRow("ir", Player("Lower Case")), IRRow("IR-", Player("X"))]
    tab = one(TeamTab("Quail", roster(1), below=below))
    assert [r.name for r in tab.rows] == ["Skater 1"]


# ---------------------------------------------------------------- unrecognized tabs


@pytest.mark.parametrize(
    ("formula", "reason"),
    [
        ("=SUM(F7:F8,F10:F12)", 'PAYROLL formula "=SUM(F7:F8,F10:F12)" is not a SUM of one range'),
        ("=SUM(F7:G9)", "PAYROLL sums F7:G9, which spans more than one column"),
        ("='Other tab'!C3", "PAYROLL formula \"='Other tab'!C3\" is not a SUM of one range"),
        ("=F7+F8", 'PAYROLL formula "=F7+F8" is not a SUM of one range'),
        ("=C20", "PAYROLL leads to C20, which has no formula"),
    ],
)
def test_formulas_that_are_not_one_single_column_sum_are_unrecognized(
    formula: str, reason: str
) -> None:
    tab = one(TeamTab("Rams", roster(2), payroll_formula=formula))
    assert (tab.status, tab.reason) == ("unrecognized", reason)
    assert (tab.payroll, tab.rows, tab.salary_column) == (None, (), None)
    assert tab.cap == CAP  # the CAP check still works for an unrecognized tab


def test_more_than_two_references_is_unrecognized() -> None:
    team = TeamTab("Seals", roster(1), payroll_formula="=C20")
    tab = team.tab()
    cells = dict(tab.cells)
    cells[(20, 3)] = Cell(1, "=SUM(C21)")
    cells[(21, 3)] = Cell(1, "=C22")
    cells[(22, 3)] = Cell(1, "=SUM(F7:F7)")
    [parsed] = parse_sheet(grid(Tab(tab.title, cells))).tabs
    assert parsed.reason == "PAYROLL follows more than 2 cell references"


def test_two_references_then_a_non_sum_is_unrecognized() -> None:
    tab = TeamTab("Toads", roster(1), payroll_formula="=C20").tab()
    cells = {**tab.cells, (20, 3): Cell(1, "=SUM(C21)"), (21, 3): Cell(1, "=F7*2")}
    [parsed] = parse_sheet(grid(Tab(tab.title, cells))).tabs
    assert parsed.reason == "PAYROLL leads to a formula that is not a SUM of one range"


def test_a_payroll_label_with_no_formula_is_unrecognized() -> None:
    tab = TeamTab("Urchins", roster(1), payroll_formula="").tab()
    cells = {k: v for k, v in tab.cells.items() if k != (3, 3)}
    cells[(3, 3)] = Cell(123_000)  # a typed-in number, not a formula
    [parsed] = parse_sheet(grid(Tab(tab.title, cells))).tabs
    assert parsed.reason == "the PAYROLL label at A3 has no formula beside it"
    cells.pop((3, 3))
    [parsed] = parse_sheet(grid(Tab(tab.title, cells))).tabs
    assert parsed.reason == "the PAYROLL label at A3 has no formula beside it"


def test_a_payroll_with_no_computed_value_is_unrecognized() -> None:
    tab = one(TeamTab("Voles", roster(1), payroll_value=None))
    assert tab.reason == "PAYROLL has no computed value (was the file recalculated?)"


# ---------------------------------------------------------------- tabs and the cap


def test_tabs_without_payroll_are_other_tabs_and_the_cap_comes_from_the_summary() -> None:
    parsed = parse_sheet(grid(TeamTab("Wasps", roster(1)), notes_tab(), TeamTab("Yaks", roster(2))))
    assert [t.name for t in parsed.tabs] == ["Wasps", "Yaks"]
    assert parsed.other_tabs == (SUMMARY, "Rule ideas")
    assert (parsed.cap, parsed.cap_source) == (CAP, f"'{SUMMARY}'!B3")
    assert parsed.cap_mismatches == ()
    assert parsed.tab("Yaks") is parsed.tabs[1]
    assert parsed.tab("Nope") is None


def test_a_tab_whose_cap_differs_is_flagged() -> None:
    stale = TeamTab("Zebus", roster(1), cap_value=100_000_000)
    parsed = parse_sheet(grid(TeamTab("Wasps", roster(1)), stale))
    assert [t.name for t in parsed.cap_mismatches] == ["Zebus"]


def test_the_most_common_cap_reference_wins() -> None:
    odd = TeamTab("Odd", roster(1), cap_formula="=Summary!B4", cap_value=1)
    parsed = parse_sheet(grid(TeamTab("A", roster(1)), TeamTab("B", roster(1)), odd))
    assert parsed.cap_source == f"'{SUMMARY}'!B3"
    assert parsed.cap == CAP


def test_an_even_split_between_cap_references_gives_no_cap() -> None:
    odd = TeamTab("Odd", roster(1), cap_formula="=Summary!B4", cap_value=1)
    parsed = parse_sheet(grid(TeamTab("A", roster(1)), odd))
    assert (parsed.cap, parsed.cap_source) == (None, None)


def test_no_cap_formula_means_no_cap() -> None:
    parsed = parse_sheet(grid(TeamTab("A", roster(1), cap_formula=None, cap_value=None)))
    assert (parsed.cap, parsed.cap_source, parsed.tabs[0].cap) == (None, None, None)
    assert parsed.cap_mismatches == ()


def test_a_typed_in_cap_is_the_tabs_cap_but_not_the_sheets() -> None:
    parsed = parse_sheet(grid(TeamTab("A", roster(1), cap_formula=None, cap_value=CAP)))
    assert (parsed.cap, parsed.tabs[0].cap) == (None, CAP)


def test_a_cap_formula_that_references_no_tab_is_the_tabs_cap_only() -> None:
    team = TeamTab("A", roster(1), cap_formula="=100000000+19600000", cap_value=CAP)
    parsed = parse_sheet(grid(team))
    assert (parsed.cap, parsed.cap_source, parsed.tabs[0].cap) == (None, None, CAP)


def test_a_cap_label_with_nothing_beside_it_is_no_cap() -> None:
    tab = TeamTab("A", roster(1), cap_formula=None, cap_value=None).tab()
    assert tab.at("A2").value == "CAP:"
    [parsed] = parse_sheet(grid(tab)).tabs
    assert parsed.cap is None


def test_a_range_at_the_top_of_the_tab_looks_no_higher_than_row_1() -> None:
    tab = Tab.from_rows(
        "Top",
        [
            ["PAYROLL", None, (5, "=SUM(F2:F3)")],
            [None, None, None, None, None, 2],
            [None] * 5 + [3],
        ],
    )
    [parsed] = parse_sheet(Grid((tab,))).tabs
    assert parsed.reason == "no NAME header in rows 1-2 above the payroll range"


def test_a_cap_reference_to_a_missing_tab_gives_no_cap() -> None:
    parsed = parse_sheet(grid(TeamTab("A", roster(1), cap_formula="='Gone'!B3")))
    assert (parsed.cap, parsed.cap_source) == (None, "'Gone'!B3")


def test_a_summary_cap_that_isnt_a_number_is_none() -> None:
    parsed = parse_sheet(grid(TeamTab("A", roster(1)), summary=summary_tab(cap=None)))
    assert parsed.cap is None


def test_an_empty_sheet() -> None:
    assert parse_sheet(Grid(())) == ParsedSheet(None, None, ())


# ---------------------------------------------------------------- hygiene (SPEC §4a)


def test_no_contact_detail_reaches_the_parsed_sheet() -> None:
    teams = [
        TeamTab("Aces", roster(3), below=[IRRow("IR", Player("Ike Injured"))]),
        TeamTab("Bees", roster(2), start=6, header="in_range", payroll="sum_ref"),
        TeamTab("Cats", roster(2), payroll="cell_ref"),
        TeamTab("Dogs", roster(2), payroll_formula="=SUM(F7:G9)"),  # unrecognized
        TeamTab("Eels", roster(2), start=12, header=5),  # unrecognized: no header
    ]
    parsed = parse_sheet(grid(*teams, notes_tab()))
    assert_no_contact(parsed)
    assert all(t.reason is None or not any(c in t.reason for c in CONTACT) for t in parsed.tabs)
    assert {t.status for t in parsed.tabs} == {"ok", "unrecognized"}


def test_the_grid_never_prints_its_cells() -> None:
    g = grid(TeamTab("Aces", roster(1)))
    assert_no_contact(g)
    assert repr(g.tabs[1]) == f"Tab('Aces', {len(g.tabs[1].cells)} cells)"


def test_contact_details_in_the_name_column_below_the_range_stay_out() -> None:
    # The contact block's rows sit in the name column, like a player row would.
    tab = one(TeamTab("Aces", roster(2), below=[Player(CONTACT[2])]))
    assert_no_contact(tab)
    assert [r.name for r in tab.rows] == ["Skater 1", "Skater 2"]


def test_a_range_far_past_the_last_row_reads_only_the_rows_there() -> None:
    # Finishes at once (the loop stops at the tab's last row). The range really
    # does cover every row below, so they are all read as counted rows.
    tab = one(TeamTab("Vast", roster(2), payroll_formula="=SUM(F7:F99999999999)"))
    assert tab.status == "ok"
    assert [r.name for r in tab.counted][:2] == ["Skater 1", "Skater 2"]


def test_an_infinite_salary_is_no_salary() -> None:
    tab = one(TeamTab("Inf", [*roster(1), Player("Ada Big", salary=float("inf"))], payroll_value=1))
    assert tab.status == "ok"
    assert [r.salary for r in tab.counted] == [1_000_000, None]


def test_a_summary_tab_titled_with_an_apostrophe_still_gives_the_cap() -> None:
    title = "Commish's Summary"
    summary = Tab.from_rows(title, [[], [], ["Salary cap", CAP]])
    aces = TeamTab("Aces", roster(1), cap_formula="='Commish''s Summary'!B3")
    parsed = parse_sheet(grid(aces, summary=summary))
    assert parsed.cap == CAP
    assert parsed.cap_source == "'Commish''s Summary'!B3"


def test_an_unrecognized_reason_quotes_the_formula_as_written() -> None:
    tab = one(TeamTab("Rams", roster(1), payroll_formula="""=IF('Pat''s Team'!B3="",0,1)"""))
    assert (
        tab.reason == """PAYROLL formula "=IF('Pat''s Team'!B3="",0,1)" is not a SUM of one range"""
    )
