"""Check that every tab of a downloaded league sheet parses (SPEC §4a, M3).

    uv run python -m scripts.check_league_sheet [path/to/sheet.xlsx]

The path defaults to ``$LEAGUE_SHEET_XLSX``. For each tab it prints the parse
status, row counts, the tab's own PAYROLL against the sum of the parsed
salaries, and the CAP check. It prints numbers and row numbers only: no
player names and no other cell values (the sheet holds managers' contact
details). Exit status 1 if any team tab is unrecognized or doesn't add up.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from fha.sources.league_sheet.models import LeagueSheetError, ParsedSheet, ParsedTab
from fha.sources.league_sheet.parse import parse_sheet
from fha.sources.league_sheet.xlsx import read_xlsx

ENV = "LEAGUE_SHEET_XLSX"


def tab_lines(tab: ParsedTab, cap: int | None) -> tuple[list[str], bool]:
    """Report lines for one tab, and whether it is fine."""
    if tab.status != "ok":
        return [f"{tab.name}: UNRECOGNIZED: {tab.reason}"], False
    counted, ir = tab.counted, tab.ir_rows
    no_salary = [r.row for r in counted if r.salary is None]
    no_name = [r.row for r in counted if not r.name]
    total = tab.counted_total
    adds_up = total == tab.payroll
    lines = [
        f"{tab.name}: ok; range {tab.payroll_range} (salary column {tab.salary_column}); "
        f"{len(counted)} counted rows, {len(ir)} IR rows",
        f"  payroll {tab.payroll:,} vs parsed salaries {total:,}: "
        + ("match" if adds_up else f"MISMATCH (off by {total - (tab.payroll or 0):,})"),
    ]
    if no_salary:
        lines.append(f"  counted rows with no numeric salary: {no_salary}")
    if no_name:
        lines.append(f"  counted rows with a salary but no name: {no_name}")
    ir_no_salary = [r.row for r in ir if r.salary is None]
    if ir_no_salary:
        lines.append(f"  IR rows with no numeric salary: {ir_no_salary}")
    cap_ok = tab.cap is None or cap is None or tab.cap == cap
    lines.append(
        "  cap: "
        + ("no CAP cell" if tab.cap is None else f"{tab.cap:,}")
        + ("" if cap_ok else f" DIFFERS from the summary cap {cap:,}")
    )
    return lines, adds_up and cap_ok


def report(sheet: ParsedSheet) -> tuple[list[str], bool]:
    lines = [
        f"Cap: {sheet.cap:,} from {sheet.cap_source}"
        if sheet.cap is not None
        else "Cap: NOT FOUND",
        f"Team tabs: {len(sheet.tabs)}; other tabs: {len(sheet.other_tabs)}",
    ]
    ok = sheet.cap is not None and bool(sheet.tabs)
    for tab in sheet.tabs:
        tab_report, tab_ok = tab_lines(tab, sheet.cap)
        lines.extend(tab_report)
        ok = ok and tab_ok
    lines.append("All team tabs parse and add up." if ok else "PROBLEMS: see above.")
    return lines, ok


def main(argv: Sequence[str], environ: dict[str, str] | os._Environ[str]) -> int:
    path = argv[0] if argv else environ.get(ENV)
    if not path:
        print(f"Give the .xlsx path, or set {ENV}.", file=sys.stderr)
        return 2
    try:
        sheet = parse_sheet(read_xlsx(path))
    except LeagueSheetError as e:
        print(f"Failed: {e}", file=sys.stderr)
        return 1
    lines, ok = report(sheet)
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
