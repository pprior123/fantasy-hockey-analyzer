"""The owner's-tab update plan (SPEC §4a "Writing"): every edit, every refusal,
and a property that no edit ever leaves the allowed cells."""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fha.domain.sheet_update import (
    CellEdit,
    Change,
    GuardError,
    Member,
    TabLayout,
    TabRow,
    allowed_rows,
    guard,
    plan_update,
    sheet_name,
)

CAP = 10_000_000
# Header on row 6, the PAYROLL range F7:F12, IR rows 14-16 below it.
LAYOUT = TabLayout(
    name_column="C",
    position_column="D",
    team_column="E",
    salary_column="F",
    first_row=7,
    last_row=12,
    header_row=6,
    blank_rows=(11, 12),
    empty_ir_rows=(15, 16),
)


def row(n: int, name: str, salary: int | None = 1_000_000, **kw: object) -> TabRow:
    counted = n <= LAYOUT.last_row
    pid = kw.pop("player_id", name.lower())
    status = kw.pop("status", "matched")
    return TabRow(n, name, "C", "TOR", salary, counted, status, pid, **kw)  # type: ignore[arg-type]


def member(pid: str, *, in_ir: bool = False, aav: int | None = 2_000_000, **kw: str) -> Member:
    first, last = kw.get("first", pid.title()), kw.get("last", "Last")
    return Member(
        pid,
        first,
        last,
        kw.get("full", f"{first} {last}"),
        kw.get("pos", "LW"),
        kw.get("team", "TB"),
        in_ir,
        aav,
    )


# Rows 7-10 counted (4 x $1M), row 14 an IR row.
ROWS = (
    row(7, "Ann"),
    row(8, "Bo"),
    row(9, "Cy"),
    row(10, "Di"),
    row(14, "Ike", salary=3_000_000),
)
ROSTER = (*(member(p) for p in ("ann", "bo", "cy", "di")), member("ike", in_ir=True))


def plan(
    rows: tuple[TabRow, ...] = ROWS,
    roster: tuple[Member, ...] = ROSTER,
    *,
    payroll: int | None = None,
    cap: int | None = CAP,
    tab_cap: int | None = CAP,
    layout: TabLayout = LAYOUT,
    ticked: tuple[int, ...] = (),
    active: int = 25,
    ir: int = 3,
):  # type: ignore[no-untyped-def]
    if payroll is None:
        payroll = sum(r.salary or 0 for r in rows if r.counted)
    return plan_update(
        rows,
        layout,
        roster,
        payroll=payroll,
        tab_cap=tab_cap,
        cap=cap,
        active_slots=active,
        ir_slots=ir,
        ticked=ticked,
    )


def cells(edits: tuple[CellEdit, ...]) -> dict[str, object]:
    return {f"{e.column}{e.row}": e.new for e in edits}


# ---------------------------------------------------------------- nothing to do


def test_a_tab_that_matches_the_roster_is_up_to_date() -> None:
    p = plan()
    assert p.up_to_date
    assert (p.edits, p.changes, p.refusals) == ((), (), ())
    assert p.payroll_after == p.payroll == 4_000_000
    assert p.room_after == CAP - 4_000_000


# ---------------------------------------------------------------- the four edits


def test_a_drop_clears_the_row_and_an_add_reuses_it() -> None:
    rows = (*ROWS[:1], replace(ROWS[1], status="unmatched", player_id=None), *ROWS[2:])
    roster = (
        ROSTER[0],
        *ROSTER[2:],
        member("ed", aav=1_500_000, first="Ed", last="Eagle", pos="C,LW"),
    )
    p = plan(rows, roster)
    assert p.ok
    assert p.changes == (Change("drop", "Bo", 8, None), Change("add", "Eagle, Ed", None, 8))
    assert cells(p.edits) == {"C8": "Eagle, Ed", "D8": "C,LW", "E8": "TB", "F8": 1_500_000}
    assert [(e.old, e.new) for e in p.edits if e.column == "C"] == [("Bo", "Eagle, Ed")]
    assert p.added == ((8, "ed"),)
    assert p.payroll_after == 4_000_000 - 1_000_000 + 1_500_000


def test_an_add_without_a_drop_takes_the_first_blank_row() -> None:
    p = plan(ROWS, (*ROSTER, member("ed")))
    assert p.changes == (Change("add", "Last, Ed", None, 11),)
    assert {e.row for e in p.edits} == {11}
    assert all(e.old is None for e in p.edits)


def test_a_plain_drop_clears_all_four_cells() -> None:
    rows = (*ROWS[:3], replace(ROWS[3], status="unmatched", player_id=None), ROWS[4])
    p = plan(rows, ROSTER[:3] + ROSTER[4:])
    assert cells(p.edits) == dict.fromkeys(("C10", "D10", "E10", "F10"))
    assert p.payroll_after == 3_000_000


def test_a_move_to_ir_copies_the_row_verbatim_and_frees_its_cap_hit() -> None:
    roster = (member("ann", in_ir=True), *ROSTER[1:])
    p = plan(ROWS, roster)
    assert p.changes == (Change("to_ir", "Ann", 7, 15),)
    assert cells(p.edits) == {
        "C7": None,
        "D7": None,
        "E7": None,
        "F7": None,
        "C15": "Ann",
        "D15": "C",
        "E15": "TOR",  # verbatim: the sheet's own team text, not Yahoo's code
        "F15": 1_000_000,
    }
    assert p.payroll_after == 3_000_000


def test_back_from_ir_takes_a_freed_counted_row_first() -> None:
    rows = (*ROWS[:1], replace(ROWS[1], status="unmatched", player_id=None), *ROWS[2:])
    roster = (ROSTER[0], *ROSTER[2:4], member("ike"))  # Ike leaves IR, Bo dropped
    p = plan(rows, roster)
    assert Change("back", "Ike", 14, 8) in p.changes
    assert cells(p.edits)["C8"] == "Ike"
    assert cells(p.edits)["F8"] == 3_000_000
    assert cells(p.edits)["C14"] is None
    assert p.payroll_after == 4_000_000 - 1_000_000 + 3_000_000


def test_a_swap_with_every_ir_row_full_reuses_the_row_it_frees() -> None:
    layout = replace(LAYOUT, empty_ir_rows=())
    roster = (member("ann", in_ir=True), *ROSTER[1:4], member("ike"))  # Ann in, Ike out
    p = plan(ROWS, roster, layout=layout)
    assert p.ok
    assert set(p.changes) == {Change("to_ir", "Ann", 7, 14), Change("back", "Ike", 14, 7)}
    assert cells(p.edits) == {"C7": "Ike", "F7": 3_000_000, "C14": "Ann", "F14": 1_000_000}


def test_an_add_into_an_ir_slot_goes_to_an_empty_ir_row() -> None:
    p = plan(ROWS, (*ROSTER, member("ed", in_ir=True, aav=4_000_000)))
    assert p.changes == (Change("add", "Last, Ed", None, 15),)
    assert p.payroll_after == p.payroll  # IR rows aren't counted


def test_a_ticked_review_row_is_dropped() -> None:
    rows = (*ROWS[:3], replace(ROWS[3], status="review", player_id=None), ROWS[4])
    roster = ROSTER[:3] + ROSTER[4:]
    assert not plan(rows, roster).ok
    p = plan(rows, roster, ticked=(10,))
    assert p.ok
    assert p.changes == (Change("drop", "Di", 10, None),)


def test_a_cell_cleared_and_refilled_with_its_own_value_is_no_edit() -> None:
    rows = (*ROWS[:3], replace(ROWS[3], status="unmatched", player_id=None), ROWS[4])
    roster = (*ROSTER[:3], ROSTER[4], member("di2", first="Di", last="", full="Di", pos="C"))
    p = plan(rows, roster)
    # Same name and position as the dropped row: only the team and salary change.
    assert cells(p.edits) == {"E10": "TB", "F10": 2_000_000}


# ---------------------------------------------------------------- the name


@pytest.mark.parametrize(
    ("first", "last", "full", "expected"),
    [
        ("Ann", "Able", "Ann Able", "Able, Ann"),
        ("James", "van Riemsdyk", "James van Riemsdyk", "van Riemsdyk, James"),
        ("", "", "James van Riemsdyk", "van Riemsdyk, James"),
        ("", "", "Madonna", "Madonna"),
        ("Cher", "", "", "Cher"),
    ],
)
def test_sheet_name_is_last_comma_first(first: str, last: str, full: str, expected: str) -> None:
    assert sheet_name(Member("1", first, last, full, "C", "TB", False, 1)) == expected


# ---------------------------------------------------------------- refusals


def refused(p) -> str:  # type: ignore[no-untyped-def]
    assert not p.ok
    assert p.edits == ()
    return " | ".join(p.refusals)


def test_over_the_cap_is_refused_with_the_amount() -> None:
    p = plan(ROWS, (*ROSTER, member("ed", aav=7_000_000)))
    assert "over the cap by $1,000,000" in refused(p)
    assert p.added == ()
    assert p.room_after == -1_000_000


def test_exactly_at_the_cap_is_allowed() -> None:
    assert plan(ROWS, (*ROSTER, member("ed", aav=6_000_000))).ok


def test_an_unknown_cap_hit_for_an_incoming_player_is_refused() -> None:
    p = plan(ROWS, (*ROSTER, member("ed", aav=None)))
    assert "Ed Last's cap hit is unknown" in refused(p)


def test_an_unknown_cap_hit_makes_the_payroll_after_unknown() -> None:
    p = plan(ROWS, (*ROSTER, member("ed", aav=None)))
    assert (p.payroll_after, p.room_after) == (None, None)
    # His full cap hit must fit when he's acquired, even into an IR slot (cap.py).
    p = plan(ROWS, (*ROSTER, member("ed", in_ir=True, aav=None)))
    assert p.payroll_after is None
    assert "Ed Last's cap hit is unknown" in refused(p)


def test_no_room_after_without_a_cap() -> None:
    assert plan(cap=None).room_after is None


def test_a_tab_without_position_and_team_columns_moves_names_and_salaries_only() -> None:
    layout = replace(LAYOUT, position_column=None, team_column=None)
    roster = (member("ann", in_ir=True), *ROSTER[1:])
    p = plan(ROWS, roster, layout=layout)
    assert cells(p.edits) == {"C7": None, "F7": None, "C15": "Ann", "F15": 1_000_000}
    p = plan(ROWS, (*ROSTER, member("ed")), layout=layout)
    assert "no position or NHL team column" in refused(p)


def test_an_unsettled_review_row_is_refused() -> None:
    rows = (*ROWS[:3], replace(ROWS[3], status="review", player_id=None), ROWS[4])
    assert "row 10 (Di) awaits match review" in refused(plan(rows, ROSTER[:3] + ROSTER[4:]))


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"payroll": 4_000_003}, "PAYROLL isn't the sum of its own counted rows"),
        ({"tab_cap": CAP + 1}, "CAP differs from the league cap"),
        ({"tab_cap": None}, "CAP differs from the league cap"),
        ({"cap": None}, "the league cap is unknown"),
    ],
)
def test_a_tab_that_disagrees_with_itself_is_refused(kwargs: dict[str, int], reason: str) -> None:
    assert reason in refused(plan(**kwargs))


def test_rounding_within_half_a_dollar_a_row_is_not_a_disagreement() -> None:
    # Four summed rows: up to (4 + 1) / 2 = $2.50 apart is rounding.
    assert plan(payroll=4_000_002).ok
    assert plan(payroll=3_999_998).ok
    assert not plan(payroll=4_000_003).ok


def test_a_negative_salary_anywhere_on_the_tab_is_refused() -> None:
    rows = (*ROWS[:4], replace(ROWS[4], salary=None, negative_salary=True))
    assert "row 14 (Ike) has a negative salary" in refused(plan(rows))


def test_formulas_block_a_drop_and_a_move() -> None:
    rows = (
        *ROWS[:3],
        replace(ROWS[3], status="unmatched", player_id=None, has_formula=True),
        ROWS[4],
    )
    assert "row 10 (Di) holds a formula" in refused(plan(rows, ROSTER[:3] + ROSTER[4:]))
    rows = (replace(ROWS[0], has_formula=True), *ROWS[1:])
    roster = (member("ann", in_ir=True), *ROSTER[1:])
    assert "row 7 (Ann) holds a formula: move it by hand" in refused(plan(rows, roster))


def test_a_move_needs_a_salary_number() -> None:
    rows = (*ROWS[:4], replace(ROWS[4], salary=None))
    roster = (*ROSTER[:4], member("ike"))
    assert "row 14 (Ike) has no salary number" in refused(plan(rows, roster))


def test_no_free_row_is_refused() -> None:
    layout = replace(LAYOUT, blank_rows=(), empty_ir_rows=())
    p = plan(ROWS, (*ROSTER, member("ed"), member("fay", in_ir=True)), layout=layout)
    reasons = refused(p)
    assert "no free blank row inside the PAYROLL range for Ed Last" in reasons
    assert "no free IR row below the PAYROLL range for Fay Last" in reasons


def test_a_tab_without_position_or_team_columns_cannot_take_a_new_row() -> None:
    layout = replace(LAYOUT, team_column=None)
    assert "no position or NHL team column" in refused(
        plan(ROWS, (*ROSTER, member("ed")), layout=layout)
    )
    # ...but drops and moves still work.
    roster = (member("ann", in_ir=True), *ROSTER[1:])
    assert plan(ROWS, roster, layout=layout).ok


def test_column_a_as_a_player_column_is_refused() -> None:
    assert "column A" in refused(plan(layout=replace(LAYOUT, name_column="A")))


@pytest.mark.parametrize(("active", "ir"), [(3, 3), (25, 0)])
def test_slot_counts_are_checked_as_invariants(active: int, ir: int) -> None:
    assert "more than the league's" in refused(plan(active=active, ir=ir))


def test_every_reason_is_listed_once() -> None:
    p = plan(ROWS, (*ROSTER, member("ed", aav=None), member("fay", aav=None)), cap=None)
    assert len(p.refusals) == len(set(p.refusals)) >= 3


# ---------------------------------------------------------------- the guard


def test_allowed_rows_are_the_range_and_the_ir_rows_never_the_header() -> None:
    assert allowed_rows(ROWS, LAYOUT) == frozenset({7, 8, 9, 10, 11, 12, 14, 15, 16})
    layout = replace(LAYOUT, first_row=6)  # a header inside the range
    assert 6 not in allowed_rows(ROWS, layout)


@pytest.mark.parametrize(
    "edit",
    [
        CellEdit(13, "C", None, "x"),  # between the range and the IR rows
        CellEdit(6, "C", "NAME", "x"),  # the header row
        CellEdit(20, "C", None, "x"),  # below everything: contact details
        CellEdit(7, "A", None, "IR"),  # column A
        CellEdit(7, "G", None, 1),  # another column
        CellEdit(7, "C", None, 1.5),  # type: ignore[arg-type]
    ],
)
def test_the_guard_rejects_edits_outside_the_allowed_cells(edit: CellEdit) -> None:
    with pytest.raises(GuardError):
        guard([edit], ROWS, LAYOUT)


def test_the_guard_accepts_every_planned_edit() -> None:
    p = plan(ROWS, (member("ann", in_ir=True), *ROSTER[1:], member("ed")))
    guard(p.edits, ROWS, LAYOUT)  # no error


# ---------------------------------------------------------------- the property


@st.composite
def scenarios(
    draw: st.DrawFn,
) -> tuple[tuple[TabRow, ...], TabLayout, tuple[Member, ...], tuple[int, ...]]:
    first = draw(st.integers(2, 8))
    size = draw(st.integers(1, 10))
    last = first + size - 1
    n_ir = draw(st.integers(0, 4))
    ir_rows = list(range(last + 2, last + 2 + n_ir))
    rows: list[TabRow] = []
    blank: list[int] = []
    roster: list[Member] = []
    ticked: list[int] = []
    for r in [*range(first, last + 1), *ir_rows]:
        kind = draw(st.sampled_from(["blank", "matched", "unmatched", "review"]))
        if kind == "blank":
            blank.append(r)
            continue
        pid = f"p{r}"
        status = "matched" if kind == "matched" else kind
        rows.append(
            TabRow(
                r,
                f"N{r}",
                "C",
                "T",
                draw(st.integers(0, 3)) * 1_000_000,
                r <= last,
                status,
                pid if kind == "matched" else None,
            )  # type: ignore[arg-type]
        )
        if kind == "matched":
            roster.append(Member(pid, "F", f"L{r}", f"F L{r}", "C", "T", draw(st.booleans()), 1))
        if kind == "review" and draw(st.booleans()):
            ticked.append(r)
    for i in range(draw(st.integers(0, 4))):
        roster.append(
            Member(f"new{i}", "F", f"New{i}", f"F New{i}", "C", "T", draw(st.booleans()), 1_000_000)
        )
    empty_ir = [r for r in ir_rows if r in blank]
    layout = TabLayout(
        "C",
        "D",
        "E",
        "F",
        first,
        last,
        first - 1,
        tuple(r for r in blank if r <= last),
        tuple(empty_ir),
    )
    return tuple(rows), layout, tuple(roster), tuple(ticked)


@settings(max_examples=300)
@given(scenarios())
def test_no_planned_edit_ever_leaves_the_allowed_cells(scenario) -> None:  # type: ignore[no-untyped-def]
    rows, layout, roster, ticked = scenario
    payroll = sum(r.salary or 0 for r in rows if r.counted)
    p = plan_update(
        rows,
        layout,
        roster,
        payroll=payroll,
        tab_cap=10**12,
        cap=10**12,
        active_slots=99,
        ir_slots=99,
        ticked=ticked,
    )
    guard(p.edits, rows, layout)
    allowed = allowed_rows(rows, layout)
    for e in p.edits:
        assert e.row in allowed
        assert e.column in {"C", "D", "E", "F"}
        assert e.row != layout.header_row
    if p.ok:
        # Every roster player ends up named on the tab, in the right part.
        names = {r.row: r.name for r in rows}
        names.update({e.row: e.new for e in p.edits if e.column == "C"})  # type: ignore[misc]
        assert sum(1 for n in names.values() if n) == len(roster)


# ---------------------------------------------------------------- exact messages and boundaries


def test_an_unticked_review_row_is_neither_dropped_nor_written() -> None:
    rows = (*ROWS[:3], replace(ROWS[3], status="review", player_id=None), ROWS[4])
    p = plan(rows, ROSTER[:3] + ROSTER[4:])
    assert p.changes == ()
    assert p.refusals == (
        "row 10 (Di) awaits match review: confirm it in Admin, or tick “drop this row”",
    )


def test_a_stale_player_id_on_an_unmatched_row_is_not_a_match() -> None:
    rows = (*ROWS[:3], replace(ROWS[3], status="unmatched"), ROWS[4])  # still says "di"
    p = plan(rows, ROSTER)
    assert p.changes == (Change("drop", "Di", 10, None), Change("add", "Last, Di", None, 10))


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"cap": None}, "the league cap is unknown (the summary tab's cap cell)"),
        ({"tab_cap": 1}, "the tab's CAP differs from the league cap: fix the tab by hand first"),
        (
            {"payroll": 1},
            "the tab's PAYROLL isn't the sum of its own counted rows: fix the tab by hand first",
        ),
        (
            {"layout": replace(LAYOUT, name_column="A")},
            "the tab's player columns include column A, which holds the IR labels",
        ),
    ],
)
def test_refusal_messages(kwargs: dict[str, object], reason: str) -> None:
    assert plan(**kwargs).refusals == (reason,)  # type: ignore[arg-type]


def test_the_over_cap_and_no_column_messages() -> None:
    p = plan(ROWS, (*ROSTER, member("ed", aav=7_000_000)))
    assert p.refusals == (
        "over the cap by $1,000,000 after these changes: fix the Yahoo roster first",
    )
    p = plan(ROWS, (*ROSTER, member("ed")), layout=replace(LAYOUT, position_column=None))
    assert p.refusals == (
        "the tab has no position or NHL team column, so a new row can't be written",
    )


def test_a_move_to_ir_with_no_free_ir_row_says_ir() -> None:
    layout = replace(LAYOUT, empty_ir_rows=())
    p = plan(ROWS, (member("ann", in_ir=True), *ROSTER[1:]), layout=layout)
    assert p.refusals == (
        "no free IR row below the PAYROLL range for Ann Last: add one to the tab by hand",
    )


def test_payroll_tolerance_with_an_odd_number_of_rows() -> None:
    rows = (*ROWS[:3], ROWS[4])  # three counted rows: (3 + 1) / 2 = $2 is rounding
    roster = (*ROSTER[:3], ROSTER[4])
    assert plan(rows, roster, payroll=3_000_002).ok
    assert not plan(rows, roster, payroll=3_000_003).ok


def test_slot_counts_exactly_at_the_limits_are_fine() -> None:
    full = (*ROWS[:4], row(11, "Eve"), row(12, "Fay"), ROWS[4], row(15, "Gus"))
    roster = (*ROSTER, member("eve"), member("fay"), member("gus", in_ir=True))
    layout = replace(LAYOUT, blank_rows=(), empty_ir_rows=(16,))
    assert plan(full, roster, layout=layout, active=6, ir=2).ok
    assert not plan(full, roster, layout=layout, active=5, ir=2).ok
    assert not plan(full, roster, layout=layout, active=6, ir=1).ok


def test_adds_and_drops_count_toward_the_slot_limits() -> None:
    assert not plan(ROWS, (*ROSTER, member("ed")), active=4).ok  # 5 after the add
    rows = (*ROWS[:3], replace(ROWS[3], status="unmatched", player_id=None), ROWS[4])
    assert plan(rows, (*ROSTER[:3], ROSTER[4], member("ed")), active=4).ok  # 4 after


def test_an_ir_row_right_below_the_range_is_not_counted() -> None:
    layout = replace(LAYOUT, empty_ir_rows=(13,))
    p = plan(ROWS, (*ROSTER, member("ed", in_ir=True, aav=5_000_000)), layout=layout)
    assert p.changes == (Change("add", "Last, Ed", None, 13),)
    assert p.payroll_after == p.payroll


def test_an_add_in_the_last_row_of_the_range_is_counted() -> None:
    layout = replace(LAYOUT, blank_rows=(12,))
    p = plan(ROWS, (*ROSTER, member("ed", aav=5_000_000)), layout=layout)
    assert p.payroll_after == p.payroll + 5_000_000


def test_the_guard_refuses_column_a_even_when_it_is_a_player_column() -> None:
    layout = replace(LAYOUT, name_column="A")
    with pytest.raises(GuardError, match="edit outside the allowed cells: A7"):
        guard([CellEdit(7, "A", None, "x")], ROWS, layout)


def test_the_guard_names_the_cell_and_the_problem() -> None:
    with pytest.raises(GuardError, match="edit outside the allowed cells: C13"):
        guard([CellEdit(13, "C", None, "x")], ROWS, LAYOUT)
    with pytest.raises(GuardError, match="edit with an unwritable value at C7"):
        guard([CellEdit(7, "C", None, 1.5)], ROWS, LAYOUT)  # type: ignore[arg-type]


# ---------------------------------------------------------------- round 1 (PR #22)


def test_slot_messages_for_the_tab_as_read_and_after_the_edits() -> None:
    assert plan(active=3).refusals == (
        "the tab lists 4 players and 1 IR players, more than the league's 3 and 3: "
        "check what the PAYROLL range and the IR rows cover",
        "the tab would list 4 players outside IR, more than the league's 3: check the Yahoo roster",
    )
    assert plan(ir=0).refusals[1] == (
        "the tab would list 1 IR players, more than the league's 0: check the Yahoo roster"
    )
    # Fine as read, too many after: only the second message.
    assert plan(ROWS, (*ROSTER, member("ed")), active=4).refusals == (
        "the tab would list 5 players outside IR, more than the league's 4: check the Yahoo roster",
    )


def test_a_payroll_range_far_longer_than_a_roster_is_refused() -> None:
    long = replace(LAYOUT, last_row=7 + 2 * 25)  # 51 rows: twice 25, plus one
    assert plan(layout=long).refusals == (
        "the PAYROLL range covers 51 rows, more than twice the league's 25 roster spots: "
        "check that it covers only your player rows",
    )
    assert plan(layout=replace(LAYOUT, last_row=6 + 2 * 25)).ok  # exactly twice


def test_a_row_with_no_name_is_never_dropped() -> None:
    salary_line = row(11, "", salary=500_000, status="unmatched", player_id=None)
    rows = (*ROWS[:4], salary_line, ROWS[4])
    layout = replace(LAYOUT, blank_rows=(12,))
    p = plan(rows, ROSTER, layout=layout)
    assert p.up_to_date
    p = plan(rows, (*ROSTER, member("ed")), layout=layout)
    assert p.changes == (Change("add", "Last, Ed", None, 12),)  # not row 11


def test_free_rows_go_out_in_player_id_order_whatever_yahoo_lists_first() -> None:
    a, b = member("ed"), member("fay")
    assert plan(ROWS, (*ROSTER, a, b)).edits == plan(ROWS, (b, *ROSTER, a)).edits
