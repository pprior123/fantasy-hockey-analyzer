"""scripts/check_league_sheet.py: numbers and row numbers only, never names or contacts."""

from pathlib import Path

import pytest

from scripts import check_league_sheet as check
from tests.unit.league_sheet.sheets import (
    CONTACT,
    IRRow,
    Player,
    TeamTab,
    grid,
    notes_tab,
    roster,
)
from tests.unit.league_sheet.xlsx_build import to_xlsx


def write(tmp_path: Path, *tabs: TeamTab) -> Path:
    path = tmp_path / "sheet.xlsx"
    path.write_bytes(to_xlsx(grid(*tabs, notes_tab())))
    return path


def run(
    capsys: pytest.CaptureFixture[str], argv: list[str], env: dict[str, str]
) -> tuple[int, str, str]:
    code = check.main(argv, env)
    out, err = capsys.readouterr()
    return code, out, err


def test_a_clean_sheet_reports_every_tab_and_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(
        tmp_path,
        TeamTab("Aces", roster(3), below=[IRRow("IR", Player("Ike Injured", salary="?"))]),
        TeamTab("Bees", [*roster(2), Player("Ben Baker", salary="???")], payroll="sum_ref"),
    )
    code, out, _ = run(capsys, [str(path)], {})
    assert code == 0
    assert out.splitlines() == [
        "Cap: 119,600,000 from '<tab>'!B3",
        "Team tabs: 2; other tabs: 2",
        "tab 1: ok; range F7:F9 (salary column F); 3 counted rows, 1 IR rows",
        "  payroll 3,750,000 vs parsed salaries 3,750,000: match",
        "  IR rows with no numeric salary: [10]",
        "  cap: 119,600,000",
        "tab 2: ok; range F7:F9 (salary column F); 3 counted rows, 0 IR rows",
        "  payroll 2,250,000 vs parsed salaries 2,250,000: match",
        "  counted rows with no numeric salary: [9]",
        "  cap: 119,600,000",
        "All team tabs parse and add up.",
    ]
    for private in ("Skater", "Ike", "Ben Baker", "Aces", "Bees", "Summary", *CONTACT):
        assert private not in out


def test_names_shows_tab_titles_for_the_owner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(tmp_path, TeamTab("Aces", roster(1)))
    code, out, _ = run(capsys, ["--names", str(path)], {})
    assert code == 0
    assert out.splitlines()[0] == "Cap: 119,600,000 from 'Summary'!B3"
    assert out.splitlines()[2].startswith("Aces: ok;")


def test_whole_dollar_rounding_still_adds_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [Player("A A", salary=1_000_000.6), Player("B B", salary=2_000_000.6)]
    path = write(tmp_path, TeamTab("Aces", rows, payroll_value=3_000_001))
    code, out, _ = run(capsys, [str(path)], {})
    assert "vs parsed salaries 3,000,002: match" in out
    assert code == 0


def test_redact_hides_quoted_and_bare_tab_titles() -> None:
    assert check.redact("='Pat''s Team'!B3 and 'x'!C1") == "='<tab>'!B3 and '<tab>'!C1"
    assert check.redact("='O''Brien'!B3") == "='<tab>'!B3"
    assert check.redact("=SUM(Aces!F7:F9)+Bees!C1") == "=SUM('<tab>'!F7:F9)+'<tab>'!C1"
    assert check.redact("=SUM(F7:F9)") == "=SUM(F7:F9)"


def test_an_unrecognized_tabs_reason_is_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(tmp_path, TeamTab("Aces", roster(1), payroll_formula="=SUM(Bees!F7:F9)"))
    _, out, _ = run(capsys, [str(path)], {})
    assert "tab 1: UNRECOGNIZED" in out
    assert "Bees" not in out
    assert "Aces" not in out


def test_problems_fail_the_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(
        tmp_path,
        TeamTab("Aces", roster(2), payroll_value=9),  # the tab's PAYROLL disagrees
        TeamTab("Bees", roster(1), payroll_formula="=SUM(F7:G9)"),
        TeamTab("Cats", roster(1), cap_value=1),
        TeamTab("Dogs", [Player("", salary=5)]),
    )
    code, out, _ = run(capsys, [], {check.ENV: str(path)})
    assert code == 1
    assert "  payroll 9 vs parsed salaries 2,250,000: MISMATCH (off by 2,249,991)" in out
    assert "tab 2: UNRECOGNIZED: PAYROLL sums F7:G9, which spans more than one column" in out
    assert "  cap: 1 DIFFERS from the summary cap 119,600,000" in out
    assert "  counted rows with a salary but no name: [7]" in out
    assert out.splitlines()[-1] == "PROBLEMS: see above."


def test_no_cap_and_no_cap_cell(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, TeamTab("Aces", roster(1), cap_formula=None, cap_value=None))
    code, out, _ = run(capsys, [str(path)], {})
    assert code == 1
    assert out.splitlines()[0] == "Cap: NOT FOUND"
    assert "  cap: no CAP cell" in out


def test_no_team_tabs_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path)
    code, out, _ = run(capsys, [str(path)], {})
    assert code == 1
    assert "Team tabs: 0; other tabs: 2" in out


def test_no_path_asks_for_one(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, [], {})
    assert code == 2
    assert check.ENV in err


def test_an_unreadable_file_fails_in_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"nope")
    code, _, err = run(capsys, [str(bad)], {})
    assert code == 1
    assert err == "Failed: not a readable .xlsx file (BadZipFile)\n"
