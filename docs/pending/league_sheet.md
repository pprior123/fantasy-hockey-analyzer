# Pending DECISIONS entries: league sheet (M3 stream B)

To be folded into `docs/DECISIONS.md` by the lead.

## 2026-09-26 — M3: how the league-sheet parser reads a tab (SPEC §4a)
`fha.sources.league_sheet` has four parts:
- a grid model (`Grid` / `Tab` / `Cell`: each cell's value and formula);
- two readers, `.xlsx` (`read_xlsx`) and the Google Sheets API
  (`read_sheets_api`);
- one pure parser, `parse_sheet(grid) -> ParsedSheet`;
- `LeagueSheetSource`, implemented by `XlsxLeagueSheet`,
  `SheetsApiLeagueSheet` and `FakeLeagueSheet`.

Rules beyond SPEC §4a, and why:
- **Labels:** `PAYROLL` and `CAP` are found in a tab's first 10 rows, matched
  case-insensitively with a trailing `:` or `.` ignored. The formula is the
  first non-empty cell within 3 columns to the right of the label (`C3`
  beside `A3` on every real tab). A tab with no PAYROLL label is not a team
  tab: it is listed in `other_tabs` (the summary tab and the notes tabs).
- **Formula:** only `=SUM(X:Y)` over one column, reached directly or through
  at most two references (`=SUM(C36)`, `=C40`). References may be absolute
  (`$F$7`), in either letter case, with any spacing, and reversed ranges are
  allowed. Anything else leaves the tab *unrecognized*, with a reason:
  - several ranges;
  - a range over more than one column;
  - arithmetic;
  - a cross-tab reference;
  - a reference to a cell with no formula;
  - more than two references;
  - no formula beside the label;
  - no computed value, as in an `.xlsx` that was never recalculated.
  
  An unrecognized tab has payroll `None` (SPEC §5: its cap room is
  "unavailable") but keeps its CAP value.
- **Header row:** the range's first row is checked first, then up to three
  rows above it. The first row with a name header (`NAME`, `Names`, `Player`,
  `Players`, `Player name`) is the header. The position (`Position`, `Pos`,
  `Pos.`) and team (`NHL Team`, `Team`, `NHL`) columns come from the same
  row. They are optional, and read as "" when missing: matching works
  without them (SPEC §6). The name header is required: without it the tab
  is unrecognized.
- **Rows in the range:** a row with no name and a blank salary is skipped.
  A salary that isn't a number (`???`, text) reads as `None`, which is what
  `SUM` ignores, so the counted total still equals the tab's PAYROLL. A row
  with a salary but no name is kept, so the owner can see it. Salaries are
  rounded to whole dollars.
- **IR rows:** only rows below the range whose column A is exactly `IR` or
  `IR+`, trimmed but case-sensitive. A label with no name is an empty slot
  and is skipped. In every other row below the range, only column A is
  looked at. So the contact block, whose name cells sit in the same columns
  as players', is never read into a result.
- **Cap:** the summary cap is the cell referenced by most team tabs' `CAP`
  formulas (`='<tab>'!B3`). A tab whose CAP value differs is listed by
  `ParsedSheet.cap_mismatches`.
- **Hygiene:**
  - A `Tab`'s repr shows only its title and cell count, so a grid logged by
    accident leaks nothing.
  - Error reasons name cell addresses and the PAYROLL formula, never another
    cell's contents.
  - The sources hold only the parsed result. Their reprs hide the sheet ID
    and the file path.
  - Tests plant fake contact details on every synthetic tab, and on the
    summary tab, and assert they appear in no result, repr or reason.
- **Readers:**
  - `.xlsx`:
    - The workbook is opened twice, once for formulas and once for cached
      values.
    - openpyxl is now a runtime dependency, imported only inside
      `read_xlsx`. A subprocess test checks that importing the sources
      doesn't load it.
    - An unreadable file is a `LeagueSheetError`.
  - Sheets API:
    - A single `spreadsheets.get` with `includeGridData=true` and a `fields`
      mask for titles, `userEnteredValue` (formulas) and `effectiveValue`.
    - The token comes from an injected async callable. Stream A's
      service-account provider plugs in here, with scope
      `sheets_api.SCOPE`.
    - Values of the wrong JSON type are refused, naming the cell address.
    - HTTP and transport errors name the status or exception type only. They
      are raised `from None`, because httpx messages can carry the URL, and
      the URL holds the sheet ID (SPEC §8).

Checked against the owner's downloaded sheet (2025-26 contents) with
`scripts/check_league_sheet.py`, which prints only statuses, counts, sums and
row numbers. All 8 team tabs parse, each tab's PAYROLL equals the sum of its
parsed salaries exactly, and every CAP matches the summary cap. The summary
tab and two notes tabs are recognized as non-team tabs. Two findings are
flagged for the owner:
- One tab has a counted row whose salary is `???` (reads as None).
- Two IR rows on another tab have no salary; that's fine, since IR salaries
  aren't counted.

Alternatives considered:
- Guessing the header by column letters (rejected: the columns move between
  tabs).
- Treating any row below the range with a name as IR (rejected: unlabelled
  rows below are not IR, and the contact block would be read).
- Reading only the payroll range from the Sheets API (rejected: the range is
  known only after the PAYROLL formula is read, and two requests cost more
  than one).
