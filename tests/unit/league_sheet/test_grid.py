"""The grid model: A1 addressing and hand-built tabs."""

import pytest

from fha.sources.league_sheet.grid import (
    EMPTY,
    Cell,
    Grid,
    Tab,
    a1,
    column_letters,
    column_number,
    parse_a1,
)


@pytest.mark.parametrize(
    ("letters", "number"), [("A", 1), ("Z", 26), ("AA", 27), ("AZ", 52), ("BA", 53), ("ZZ", 702)]
)
def test_column_letters_and_numbers(letters: str, number: int) -> None:
    assert column_number(letters) == number
    assert column_number(letters.lower()) == number
    assert column_letters(number) == letters


def test_column_numbers_start_at_one() -> None:
    with pytest.raises(ValueError, match="start at 1, got 0"):
        column_letters(0)


def test_a1_references() -> None:
    assert parse_a1("F7") == (7, 6)
    assert parse_a1(" $F$7 ") == (7, 6)
    assert parse_a1("aa10") == (10, 27)
    assert a1(7, 6) == "F7"
    for bad in ("7F", "F", "", "F7:F8", "ABCD1"):
        with pytest.raises(ValueError, match="not an A1 cell reference"):
            parse_a1(bad)


def test_cell_views() -> None:
    assert Cell("  Ann  ").text == "Ann"
    assert Cell(5).text == ""
    assert Cell(5).number == 5.0
    assert Cell(2.5).number == 2.5
    assert Cell(True).number is None
    assert Cell("5").number is None
    assert Cell(None).is_blank
    assert Cell("  ").is_blank
    assert not Cell(0).is_blank
    assert not Cell("x").is_blank


def test_tab_from_rows_takes_values_formulas_pairs_and_cells() -> None:
    tab = Tab.from_rows(
        "T",
        [
            ["a", None, "", 3],
            ["=SUM(A1:A2)", (9, "=A1*2"), Cell("c", "=x")],
        ],
        start_row=4,
    )
    assert dict(tab.cells) == {
        (4, 1): Cell("a"),
        (4, 4): Cell(3),
        (5, 1): Cell(None, "=SUM(A1:A2)"),
        (5, 2): Cell(9, "=A1*2"),
        (5, 3): Cell("c", "=x"),
    }
    assert (tab.max_row, tab.max_col) == (5, 4)
    assert tab.at("B5") == Cell(9, "=A1*2")
    assert tab.cell(1, 1) is EMPTY


def test_an_empty_tab_and_grid_lookup() -> None:
    empty = Tab("E")
    assert (empty.max_row, empty.max_col) == (0, 0)
    g = Grid((empty, Tab("F")))
    assert g.tab("F") is g.tabs[1]
    assert g.tab("missing") is None
    assert [t.title for t in g] == ["E", "F"]


def test_a_tab_never_prints_its_cells() -> None:
    tab = Tab.from_rows("T", [["secret@example.com"]])
    assert repr(tab) == "Tab('T', 1 cells)"
    assert "secret" not in repr(Grid((tab,)))
