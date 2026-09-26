"""The free-agent salary CSV (DECISIONS: "M3: free-agent salary CSV format").

Every CSV here is built in code from made-up players (SPEC §8: no .csv is
committed).
"""

import pytest

from fha.sources.puckpedia import (
    CsvSalarySource,
    FakeSalarySource,
    SalaryCsvError,
    SalaryRow,
    SalarySource,
    parse_salary_csv,
)

# The header row the owner adds above PuckPedia's copied table.
OWNER_HEADER = (
    "Rank,Player,Picture,Age,Pos,Contract Year,Years,Cap Hit,Clause,Start,Signing Status,"
    "End,Expiry Status,Agent,GM,GP,G,A,P,X1,X2,X3,X4,X5,X6"
)
NBSP = "\N{NO-BREAK SPACE}"


def puckpedia_row(rank: int, last: str, first: str, pos: str, cap: str) -> str:
    """A 25-column row shaped like PuckPedia's paste (name with a non-breaking space)."""
    return (
        f'{rank},"{last},{NBSP}{first}",Picture,24,{pos},YR{NBSP}1{NBSP}OF{NBSP}5,5,"{cap} ",,'
        "2026-27,RFA,2030-31,UFA,An Agent,A General Manager,70,29,38,67,$269k,0.96,55,4,1.1,2.02"
    )


def csv(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


# ---------------------------------------------------------------- the owner's format


def test_a_puckpedia_paste_with_the_owners_header() -> None:
    data = csv(
        OWNER_HEADER,
        puckpedia_row(1, "Testman", "Ada", "C", "$18,000,000"),
        puckpedia_row(2, "Sample", "Bo", "D", "$875,000"),
        puckpedia_row(3, "Keeper", "Cy", "G", "$1,500,000"),
    )
    assert parse_salary_csv(data) == [
        SalaryRow("Testman, Ada", "C", None, 18_000_000, 2),
        SalaryRow("Sample, Bo", "D", None, 875_000, 3),
        SalaryRow("Keeper, Cy", "G", None, 1_500_000, 4),
    ]


def test_mac_roman_bytes_with_non_breaking_spaces() -> None:
    """What the owner's spreadsheet app saved: 0xCA is Mac Roman's non-breaking space."""
    text = "\n".join((OWNER_HEADER, puckpedia_row(1, "Müller", "Åke", "L", "$950,000"))) + "\n"
    data = text.encode("mac_roman")
    assert b"\xca" in data
    with pytest.raises(UnicodeDecodeError):
        data.decode("utf-8")
    assert parse_salary_csv(data) == [SalaryRow("Müller, Åke", "L", None, 950_000, 2)]


def test_utf8_with_a_byte_order_mark() -> None:
    data = '﻿Player,Pos,Cap Hit\nZoë Tester,R,"$2,000,000"\n'.encode()
    assert parse_salary_csv(data) == [SalaryRow("Zoë Tester", "R", None, 2_000_000, 2)]


def test_headers_are_matched_case_insensitively_and_trimmed() -> None:
    data = csv(" player ,POS,  cap HIT", "Ada Testman,C,1000000")
    assert parse_salary_csv(data) == [SalaryRow("Ada Testman", "C", None, 1_000_000, 2)]


def test_the_optional_team_column() -> None:
    data = csv("Player,Team,Pos,Cap Hit", 'Ada Testman,TBL,C,"$1,000"', 'Bo Sample, ,D,"$2,000"')
    assert parse_salary_csv(data) == [
        SalaryRow("Ada Testman", "C", "TBL", 1_000, 2),
        SalaryRow("Bo Sample", "D", None, 2_000, 3),
    ]


def test_the_optional_cap_hit_with_bonuses_column() -> None:
    data = csv(
        "Player,Pos,Cap Hit,cap hit with bonuses",
        'Ada Testman,C,"$950,000","$3,450,000"',
        'Bo Sample,D,"$2,000",',
        "Cy Short,G,$1",
    )
    assert parse_salary_csv(data) == [
        SalaryRow("Ada Testman", "C", None, 950_000, 2, 3_450_000),
        SalaryRow("Bo Sample", "D", None, 2_000, 3, None),
        SalaryRow("Cy Short", "G", None, 1, 4, None),  # the row is short
    ]
    with pytest.raises(SalaryCsvError, match="line 2: Cap Hit With Bonuses 'lots'"):
        parse_salary_csv(csv("Player,Pos,Cap Hit,Cap Hit With Bonuses", "A B,C,$1,lots"))
    assert parse_salary_csv(csv("Player,Pos,Cap Hit", "A B,C,$1"))[0].cap_hit_with_bonuses is None


def test_other_columns_are_ignored_whatever_their_headers() -> None:
    data = csv(",Player,,Notes,Notes,Pos,Cap Hit,GM", "x,Ada Testman,y,a,b,C,$5,Someone")
    assert parse_salary_csv(data) == [SalaryRow("Ada Testman", "C", None, 5, 2)]


def test_blank_lines_are_skipped_and_lines_keep_their_file_numbers() -> None:
    data = csv("", "Player,Pos,Cap Hit", "", "Ada Testman,C,$5", ",,", "Bo Sample,D,$6", "")
    assert [(r.name, r.line) for r in parse_salary_csv(data)] == [
        ("Ada Testman", 4),
        ("Bo Sample", 6),
    ]


def test_names_are_tidied_but_not_normalized() -> None:
    data = csv("Player,Pos,Cap Hit", f'"  Stützle,{NBSP}{NBSP}Tim ",c,$5')
    assert parse_salary_csv(data) == [SalaryRow("Stützle, Tim", "C", None, 5, 2)]


def test_a_header_row_with_no_players_is_empty() -> None:
    assert parse_salary_csv(csv("Player,Pos,Cap Hit")) == []


@pytest.mark.parametrize(
    ("cap", "aav"),
    [("$18,000,000", 18_000_000), ("18000000", 18_000_000), (" $ 775,000 ", 775_000), ("$0", 0)],
)
def test_cap_hits(cap: str, aav: int) -> None:
    data = csv("Player,Pos,Cap Hit", f'Ada Testman,C,"{cap}"')
    assert parse_salary_csv(data)[0].aav == aav


@pytest.mark.parametrize("pos", ["C", "L", "R", "D", "G", "LW", "RW", "C/L", "d"])
def test_positions_any_group_recognizes(pos: str) -> None:
    data = csv("Player,Pos,Cap Hit", f'Ada Testman,"{pos}",$5')
    assert parse_salary_csv(data)[0].position == pos.upper()


# ---------------------------------------------------------------- rejected, whole file


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ((), "^the file is empty$"),
        (("", "  ", ",,"), "^the file is empty$"),
        (("Player,Cap Hit", "A,$5"), "^line 1: missing column: Pos \\(the header row needs "),
        (("Name,Position,Salary", "A,C,$5"), "^line 1: missing columns: Player, Pos, Cap Hit"),
        (("Player,Pos,Cap Hit,player", "A,C,$5,B"), "^line 1: two 'Player' columns$"),
        (("Player,Pos,Cap Hit", "Ada,C"), "^line 2: no Cap Hit value \\(the row is short\\)$"),
        (("Player,Pos,Cap Hit", " ,C,$5"), "^line 2: Player is blank$"),
        (("Player,Pos,Cap Hit", "Ada,,$5"), "^line 2: Pos '' is not a position$"),
        (("Player,Pos,Cap Hit", "Ada,C/D,$5"), "^line 2: Pos 'C/D' is not a position$"),
        (("Player,Pos,Cap Hit", "Ada,X,$5"), "^line 2: Pos 'X' is not a position$"),
        (
            ("Player,Pos,Cap Hit", "Ada,C,???"),
            "^line 2: Cap Hit '\\?\\?\\?' is not a dollar amount$",
        ),
        (("Player,Pos,Cap Hit", "Ada,C,"), "^line 2: Cap Hit '' is not a dollar amount$"),
        (("Player,Pos,Cap Hit", "Ada,C,$1.5M"), "Cap Hit '\\$1.5M' is not a dollar amount"),
        (("Player,Pos,Cap Hit", "Ada,C,-$5"), "Cap Hit '-\\$5' is not a dollar amount"),
        (("Player,Pos,Cap Hit", 'Ada,C,"$18,00,000"'), "is not a dollar amount"),
        (("Player,Pos,Cap Hit", "Ada,C,$5", "Bo,C,$x"), "^line 3: Cap Hit '\\$x'"),
    ],
)
def test_a_bad_file_is_rejected_naming_the_line(lines: tuple[str, ...], message: str) -> None:
    with pytest.raises(SalaryCsvError, match=message):
        parse_salary_csv(csv(*lines) if lines else b"")


def test_a_missing_column_error_names_what_the_header_needs() -> None:
    with pytest.raises(SalaryCsvError, match="needs Player, Pos and Cap Hit"):
        parse_salary_csv(csv("Player,Cap Hit", "A,$5"))


# ---------------------------------------------------------------- SalarySource


async def test_csv_salary_source_parses_its_bytes() -> None:
    source: SalarySource = CsvSalarySource(csv("Player,Pos,Cap Hit", "Ada Testman,G,$5"))
    assert await source.rows() == [SalaryRow("Ada Testman", "G", None, 5, 2)]


async def test_csv_salary_source_raises_on_a_bad_file() -> None:
    with pytest.raises(SalaryCsvError, match="missing column"):
        await CsvSalarySource(csv("Player,Pos", "A,C")).rows()


async def test_fake_salary_source_returns_its_rows() -> None:
    rows = [SalaryRow("Ada Testman", "C", "TBL", 5, 2)]
    source: SalarySource = FakeSalarySource(rows)
    assert await source.rows() == rows
    assert await source.rows() is not rows  # a copy
