# Plan: update the owner's tab in the league sheet

Status: **implemented** on `feature/league-sheet-update` (2026-10-30), phases 0-5; the
rehearsal (Phase 6) waits for the merge. Deviations are in DECISIONS, "League sheet:
updating the owner's tab", implementation notes.
Base: `main` at `a626288`. One independent review has been triaged (§5).

## 0. The feature, and what the owner decided

When the owner changes their Yahoo roster, the app proposes the matching edits
to **the owner's own tab** in the shared league salary sheet. It shows a
preview, validates the result, writes only after the owner taps, and reads the
tab back to confirm the payroll.

The owner's answers (2026-10-30):

| Question | Answer |
|---|---|
| Approach | Detect automatically, preview, write only on a tap (never in the background), validate, own tab only, read back. |
| Rules | The salary cap, at most **25** rostered players outside IR, and up to **3** IR spots. Nothing else. |
| Access | Not yet. The owner will give the service account Editor and protect the other tabs, without asking the commissioner first. First, a rehearsal on a private copy (Phase 6). Writing ships **off**. |
| Placement | An add reuses a dropped player's row, otherwise the first blank row in the PAYROLL range. A drop clears its row. Only values change: rows are never inserted or deleted. |
| New row | Name `Last, First`; position and NHL team as Yahoo codes (`C,LW`, `TB`); the cap hit in the salary column. |
| IR moves | Yes, both ways: a counted row moves to an empty IR row below the range, and back. The owner will give the tab 3 IR rows. |

The recorded league settings (`tests/fixtures/yahoo/001-league-477-l-8076-settings.json`)
agree with the owner's numbers: C3 LW3 RW3 D6 G1 + BN9 = 25 outside IR, and
**IR+ 3** (there is no plain IR slot). Yahoo already enforces both limits, so
only the cap is a real check here (§3, Phase 2).

## 1. Strategy critique

Four approaches were compared.

**A. Value-only cell edits inside the parsed ranges, confirmed by recomputing the plan.**
The app writes cell values (name, position, team, salary) only in rows the
parser has already resolved: the PAYROLL range and the IR-labelled rows below
it. Row structure is never touched, so the SUM range, the formulas and the
contact details below the range cannot shift.
*For:* the smallest blast radius, and it fits the §4a hygiene boundary as it stands.
*Against:* it needs blank rows to exist, and when none do it refuses.

**B. Structural edits (insert or delete rows).**
*Rejected.* Inserting or deleting a row shifts everything below it, including
the IR rows and the managers' contact details. The SUM range might stretch or
it might not, depending on where the row goes. The app could no longer reason
about its own guard, and a mistake would be hard to undo.

**C. Preview plus a copy-paste list, with no write.**
No scope change and no risk to the shared sheet. But the owner still edits the
sheet by hand, which is the chore this feature exists to remove.

**D. The writer re-reads the raw grid itself to find its target cells.**
*Rejected.* That brings a grid, contact details included, into the write path.
The parser should instead hand over the few extra facts the writer needs
(Phase 1), so the grid stays discarded right after parsing.

**Decision: A, built so that its first half is C.** Diffing, validation and the
preview do all the thinking and need no new access, so they ship first and are
useful at once: the owner can apply the listed edits by hand. Writing is then a
thin, guarded layer over a plan that has already been reviewed, switched on
only after the rehearsal on a copy.

## 2. What changes and what stays

Stays the same:
- The read path (`read_sheets_api`, the `spreadsheets.readonly` token), the
  matching (`match_tab`, `tab_reports`), the cap predicates and every number
  shown today.
- The hygiene rule. The new facts all come from rows the parser already reads;
  the only new reads are blank checks on IR rows, which keep no contents.

Changes:
- The parser exposes the empty slots and data columns a writer needs (Phase 1).
- A new pure domain module, `domain/sheet_update.py`, computes and validates the
  edits (Phase 2).
- A preview page (Phase 3).
- A `LeagueSheetWriter` seam, a write-scoped token, a protection check, a
  guarded confirm, and a read-back (Phase 4).
- An optional separate key for the sheet, so that a live sheet can be used
  with local storage (Phase 4b).
- SPEC and DECISIONS (Phases 0 and 5), and a rehearsal (Phase 6).

## 3. Phases

Each phase is one commit that builds and passes every gate (ruff, mypy
--strict, pytest with the coverage gates, and mutmut on `domain/`). The work
goes on its own branch and PR. The pre-change state is tagged
`pre-sheet-update`.

### Phase 0: scope and decision (docs only)
- SPEC §1: a scope entry that brings "update the owner's own tab" into scope,
  with the guard rules from this plan. Write access stays limited to that one
  tab; Yahoo stays read-only.
- SPEC §10: update the "Later" item about exporting the owner's roster.
- SPEC §3: add the `LeagueSheetWriter` seam.
- SPEC §4a: add a "Writing" paragraph. "Shared view-only" becomes "Editor,
  with every other tab protected". The reading rule widens, and the
  `parse.py` docstring changes to match: today column A is the only cell read
  in other rows below the range. Now an IR-labelled row with no name also has
  its four target cells checked, for blankness only.
- DECISIONS: the owner's answers above, approach A over B, C and D, and the
  rehearsal-first order.

### Phase 1: the parser exposes what a writer needs (additive)
In `sources/league_sheet/grid.py`, `models.py`, `parse.py` and `sheets_api.py`:
- `ParsedTab.sheet_id: int | None`. This is Google's numeric tab ID, which
  `spreadsheets.batchUpdate` needs. In `FIELDS`, `properties(title)` becomes
  `properties(title,sheetId)`, and `grid.Tab` gains a `sheet_id` field (it has
  none today). The xlsx reader leaves it `None`.
- `ParsedTab.name_column`, `position_column`, `team_column`: column letters.
  Position and team may be `None`; a tab without them can't take a new row,
  and the plan refuses.
- `ParsedTab.header_row`.
- `ParsedTab.blank_rows: tuple[int, ...]`: the rows inside the PAYROLL range
  (below the header) whose **four** target cells (name, position, team and
  salary) are all blank and hold no formula. The scan runs to the range's last
  row, not to `tab.max_row`, so trailing blank rows count. (Today
  `_counted_rows` stops at `min(last, tab.max_row)` and skips blanks.)
- `ParsedTab.empty_ir_rows: tuple[tuple[int, IRLabel], ...]`: the IR-labelled
  rows whose four target cells are all blank and hold no formula. Today
  `_ir_rows` reads only column A in a nameless row. The new check reads that
  row's four target cells only to test that they are blank, and keeps no
  contents.
- `SheetRow.has_formula: bool`: true if any of the row's four target cells
  holds a formula.
- `SheetRow.salary_kind: "number" | "text" | "blank" | "negative"`. SUM adds a
  number, including a negative one, and ignores text such as `???`. The plan
  needs this to know each row's real contribution to PAYROLL.
- `encode_sheet` and `decode_sheet` round-trip the new fields. `decode_sheet`
  supplies defaults, because production Firestore holds a sheet stored by the
  old encoder. A sheet without the new fields yields "re-read the sheet
  first", never a guess.

Tests: synthetic tabs for each new field, an old-format stored document that
still decodes, and a check that no cell outside the boundary reaches a result.
No existing number moves.

### Phase 2: the edit plan and validation (pure, `domain/sheet_update.py`, test-first)
Inputs are plain data. The services layer builds them from:
- the owner's `TabReport`;
- the `is_mine` team's roster;
- `LeagueSettings.roster_slots`;
- the summary cap;
- the incoming players' cap hits from the free-agent salaries (the CSV row,
  which a Cap hit edit override replaces; `free_agents.load_free_agent_salaries`).

**The target state.** Every Yahoo roster entry is on the tab. It is counted if
it is outside an IR slot, and in an IR row if it is in one.

**The edits, in order:**
1. **Drop.** A row whose match status is `UNMATCHED` is cleared. A `REVIEW` or
   `AMBIGUOUS` row is shown in the preview with its candidates and a
   **"drop this row"** tick. The owner either ticks it (the row is then
   treated as a drop) or confirms the match in Admin. An unsettled row blocks
   the write. Without the tick, a dropped player whose name fuzzy-matches
   someone still on the roster would stay in review forever.
2. **Move to IR.** A matched counted row whose player now sits in an IR slot.
   Its four target cells are **copied verbatim**, `???` included, into a free
   IR row, and the row is cleared. A verbatim copy keeps the row key
   `(name, position)`, so the owner's confirmed binding survives, and no AAV
   lookup is needed.
3. **Back from IR.** The reverse, verbatim, into a free counted row.
4. **Add.** A missing player gets a new row (name `Last, First`, position,
   team, cap hit) in a free counted row, or in a free IR row if he sits in an
   IR slot.

**Two free-row pools.** Each pool takes rows freed in this plan first, in row
order, then the parser's blank rows:
- counted: rows freed by drops and by moves to IR, then `blank_rows`;
- IR: rows freed by moves back and by IR drops, then `empty_ir_rows`.

So a same-day swap with all 3 IR rows full works. When a cell is cleared and
then refilled in the same plan, the two become one net edit. Yahoo's snapshot
doesn't pair adds with drops, so "reuse the drop's row" means "dropped rows
are used first". Column A is never written, so IR labels stay as the owner
set them, and any empty IR or IR+ row may take an IR player.

**The name.** The Yahoo player resource has `name.first` and `name.last`. The
Yahoo parser stores them on `Player`, with a fallback that splits at the first
space, so "James van Riemsdyk" is written as "van Riemsdyk, James". The
preview shows the exact text that will be written. This touches the Yahoo
parser, `snapshot_codec` and the demo. Snapshots already stored in production
Firestore lack the new fields, so decoding supplies empty defaults and the
name falls back to the split.

**Other columns** in a reused or cleared row (notes and the like) are left
alone and never read. The preview says so.

**A cell edit** is `(row, column, old value, new value)`. Old values come from
the parsed tab, for the preview and for the guard.

**The plan refuses** (no edits, a reason shown) when:
- the tab is not `ok`, or is not bound to the `is_mine` team;
- a `REVIEW` or `AMBIGUOUS` row is neither ticked nor confirmed;
- the tab is inconsistent with itself: its PAYROLL differs from
  `ParsedTab.counted_total` beyond the rounding tolerance, or its CAP differs
  from the summary cap. This is deliberately not `TabReport.payroll_differs`,
  which compares only *matched* rows, so every pending drop would trip it.
- a target cell holds a formula;
- a row to be cleared or moved has a `negative` salary (SUM adds it, so the
  arithmetic would be wrong);
- a pool has no free row;
- the tab has no position or team column;
- an incoming player's cap hit is unknown. The owner is pointed to Cap hit edit.

**Validation of the resulting tab:**
- Expected payroll = the tab's PAYROLL + each changed counted row's SUM
  contribution (a `number` or `negative` salary adds its value; `text` and
  `blank` add 0).
- The rule is `cap_room(sheet.cap, expected) >= 0` (`cap.py`).
- If a `???` row moves back into the counted range, the cap check shows
  "unknown (row N has no number)", and the owner decides.
- Starting from the tab's own PAYROLL, rather than summing the matched rows,
  means a small rounding difference that was already there can't make the
  read-back fail.
- Counted rows ≤ the non-IR slots (25) and IR rows ≤ the IR slots (3), both
  from `roster_slots`. These are **invariants**, not rules the owner sees.
  After the plan, the tab mirrors a roster Yahoo has already limited, so a
  failure here is a bug and is reported as one.

Tests: example tests for each edit and each refusal, plus a hypothesis
property test. For any generated tab and roster, every edit lies in the
`{name, position, team, salary}` columns × (the counted rows ∪ `blank_rows` ∪
the named IR rows ∪ `empty_ir_rows`). It is never in column A, never in the
header row, and never in a row below the range that isn't IR-labelled.
Surviving mutants are killed or justified.

### Phase 3: preview (read-only; useful before any write access)
- `services/sheet_update.py`: builds the inputs and calls the domain.
- Route `GET /admin/sheet/update`. The page shows:
  - a table of edits (row, cell, before → after);
  - the "drop this row" ticks;
  - cap room before and after;
  - refusal reasons;
  - the Yahoo data's age (the owner taps Refresh for a fresh roster; there is
    no per-team Yahoo call).
- A Rosters badge on the owner's team, "Sheet out of date: review the update",
  when the plan has edits. The Admin discrepancy report links to the same page.
- With writing off (the default), the page says "apply these edits by hand",
  with no button.
- Works at phone width. Only parsed fields are rendered, as Admin does today.

### Phase 4: the writer, the confirm, the read-back
Google API behaviour is checked against Google's documentation at the start of
this phase: `batchUpdate` atomicity, clearing with `fields: userEnteredValue`,
the scope, and `requestingUserCanEdit`. The reviewer couldn't verify these
offline.

- **Seam** (`sources/league_sheet/source.py`): a `LeagueSheetWriter` Protocol
  with `async def write(self, tab: ParsedTab, edits: Sequence[CellEdit]) -> None`.
  It has two implementations: `SheetsApiLeagueSheetWriter` and
  `FakeLeagueSheetWriter`, which records calls and is used in tests and the
  demo. There is no xlsx writer: dev uses the fake.
- **Guard** (pure, in the domain module): `guard(edits, tab, bound_tab_name)`
  raises unless every edit targets the bound tab's `sheet_id` and title, a row
  from the allowed set, and one of the four data columns. The writer calls it
  again before building its request, as defence in depth.
- **Protection check:** the pre-write read also asks for
  `sheets.protectedRanges`. If Google reports for each protected range whether
  the caller can edit it (`requestingUserCanEdit`), the write is refused
  unless every tab except the bound one is protected against the service
  account. A missing or removed protection then blocks the write instead of
  relying on the owner's memory. A tab only passes if its protection covers
  the **whole** tab: the range names only the `sheetId`, and there are no
  `unprotectedRanges`. If the field doesn't behave that way, this check is
  dropped and the reason recorded.
- **The HTTP call:** a single `spreadsheets.batchUpdate` with one
  `updateCells` request per cell, `fields: "userEnteredValue"`, and the
  `GridRange` by `sheetId` (indices start at 0 and the end is exclusive; the
  exact-body test pins this). It is chosen over `values.batchUpdate` because a
  `batchUpdate` is **atomic** (all or nothing), a clear is a true clear rather
  than an empty string, and formatting stays. Strings are always
  `stringValue`, so a name can never become a formula, and salaries are
  `numberValue`. As on the read path, errors never carry the sheet ID or the
  response body.
- **Token:** a second `ServiceAccountTokens` with the `spreadsheets` scope,
  built only when `FHA_SHEET_WRITE=1` and `LEAGUE_SHEET_ID` are set, and used
  only by the writer. Reads keep `spreadsheets.readonly`. With the flag off,
  no writer exists, and the confirm route answers 404.
- **Confirm without stored state:** the preview form carries a signed SHA-256
  (itsdangerous, its own salt, `max_age` 10 min) of the tab's parsed state,
  the edits and the owner's ticks. Only the hash goes in the form, never the
  state itself. `POST /admin/sheet/update`:
  1. Read the live sheet again (and store it, as Read sheet does).
  2. Recompute the plan.
  3. If the hash differs, write nothing, show the new preview, and say "the
     tab or the roster changed".
  4. Otherwise run the protection check and the guard, then write.
- **Bind what was written:** after a successful write, and before the
  read-back, store a row binding (`player_id`, `how: "written"`) in
  `ROW_BINDINGS` for every added row. Otherwise the app's own new row (a new
  `(name, position)` key) might fuzzy-match only to REVIEW, and then the
  read-back would fail on a correct write and the row would wait in Admin
  review. Moved rows keep their key and binding.
- **Read-back:** one more read, then check:
  - PAYROLL equals the expected payroll, within the existing rounding tolerance;
  - the recomputed plan is empty;
  - each written cell holds its new value.

  The result is shown as a flash. A mismatch is reported with the cells
  involved and is never auto-corrected. The edits are logged as a one-line
  audit (cell addresses and player names only; no other cell contents and no
  sheet ID).
- **Cost and race:** a confirm makes two full reads and one write; the preview
  is a separate GET. Latency is measured in the rehearsal. As far as is known,
  the Sheets API has no precondition on `batchUpdate`, so a window of seconds
  remains between the re-read and the write. Only the owner edits this tab,
  and a repeated identical write is harmless.

Tests, all respx and all synthetic:
- the exact request body;
- no write when validation fails, when the hash is stale, when a protection is
  missing, or when the flag is off;
- the guard rejects a wrong tab, a wrong `sheet_id`, column A, a row outside the
  allowed set, and the header row;
- the write-scope token is never used by `read_sheets_api`;
- read-back pass and fail paths;
- an HTTP error during the write;
- no live network.

### Phase 4b: a live sheet with local storage
Today a local run can't read a live sheet with local storage: the sheet reader
needs `FIRESTORE_SERVICE_ACCOUNT_JSON` (`web/context.py:145-147`), and
`storage/factory.py:41-42` refuses that variable together with
`FHA_LOCAL_REPOSITORY`. Add `LEAGUE_SHEET_SERVICE_ACCOUNT_JSON`. If it is set,
the sheet reader and writer use it; otherwise they fall back to the Firestore
key. The rehearsal can then run locally against a test copy without touching
production Firestore, and a separate service account for the sheet becomes
possible later.

### Phase 5: docs match the code
SPEC §3, §4a and §7 (the page), the DECISIONS entry finalised, the CLAUDE.md
commands if any are new, and `.env.example` gains `FHA_SHEET_WRITE` and
`LEAGUE_SHEET_SERVICE_ACCOUNT_JSON`.

### Phase 6: rehearsal on a private copy (the owner, guided one step at a time)
After the PR is merged, and before anything touches the real sheet:
1. File → Make a copy of the league sheet, in the owner's Drive. It contains
   contact details, so it is shared with no one but the service account.
2. Share the copy with the service account as Editor, protect its other tabs
   (Custom, with the service account unticked), and add 3 `IR+` rows plus some
   blank rows to the owner's tab.
3. Run the app locally: `LEAGUE_SHEET_ID` set to the copy (the owner sets it,
   since it lives in `.env`), `FHA_LOCAL_REPOSITORY`, `FHA_SHEET_WRITE=1`, and
   the sheet key from Phase 4b.
4. Try each case:
   - an add with a drop;
   - a move to IR and back;
   - an over-cap change, which must be refused;
   - a stale preview (edit the copy by hand between the preview and the tap);
   - a removed protection on another tab, which must be refused by the
     protection check;
   - a forced write to another tab from a test script, which Google must reject.
5. Record the latency and the results in DECISIONS.

Only then the real sheet: Editor, the protections, the tab's IR+ rows, and
`FHA_SHEET_WRITE=1` in Vercel, one step at a time.

## 4. Risks to the other managers' data

| Risk | Mitigation |
|---|---|
| A write to another tab | The pure guard and the writer's own check (title and `sheetId` of the tab bound to the `is_mine` team); Google's protections on every other tab and `GM s`; the protection check before every write. |
| A write outside the player rows | Allowed rows and columns come from the parser; column A and the header row are never written; there are no structural edits. |
| A write on stale data | Re-read, recompute, and compare hashes just before writing. The remaining window is seconds, and only the owner edits this tab. |
| A partial write | `batchUpdate` is atomic (to be confirmed in Phase 4). |
| A broader scope leaking into reads | A separate token; the read path keeps `readonly`. |
| One key that can write both Firestore and the sheet | Accepted for now and recorded. Phase 4b allows a separate account. |
| Formula injection through a name | `stringValue` only. |
| A silent mistake | The read-back check, the audit log line, and the owner's tap on every write. Sheets version history allows recovery: copy the old cells back rather than restoring the whole sheet, which would undo the other managers' edits too. |
| A bug found only against the real Google API | The rehearsal on a private copy (Phase 6). |

## 5. Review triage

There was one independent Claude reviewer (2026-10-30, HEAD `a626288`). Each
finding was checked against the code before being accepted.

| # | Sev | Finding | Verified | Resolution |
|---|---|---|---|---|
| 1 | high | The "payroll discrepancy" refusal would block every drop: `payroll_differs` compares PAYROLL with the *matched* counted rows only (`services/league_sheet.py:231`, `:296-297`). | Yes | Accepted. The refusal is now a self-consistency check, PAYROLL against `counted_total`. |
| 2 | high | A dropped player who fuzzy-matches a current one stays in REVIEW forever (`ROSTER_FUZZY = 75`, `matcher.py:64`, `:237`), and Admin can only bind a sheet row (`confirm_row`). | Yes | Accepted. The preview gets a "drop this row" tick, included in the hash. |
| 3 | medium | IR rows freed in the same plan weren't reusable, so a same-day swap with full IR rows was refused. | Yes (plan text) | Accepted. Two free-row pools, and net edits. |
| 4 | medium | "Use Cap hit edit" can't fix a moved `???` row: `_aavs` takes the sheet's `None` (`league_view.py:243-245`). Rewriting the row would also change its key. | Yes | Accepted. Moves copy the four cells verbatim. |
| 5 | medium | "Blank" rows were tested on the name only, so they could hold content in other cells; the scan stops at `max_row`. | Yes (`parse.py:170`, `:186-187`) | Accepted. All four cells must be blank with no formula; the scan runs to the range's last row. |
| 6 | medium | Other columns of a reused row carry over. | Inferred | Accepted as a preview note. Those columns stay untouched and unread (hygiene). |
| 7 | low | A negative salary parses as `None`, but SUM adds it. | Yes (`parse.py:263-266`) | Accepted. `salary_kind`, and a refusal. |
| 8 | low | The slot checks can't fail; which cap was unspecified. | Yes (fixture) | Accepted. The slot checks become invariants, the cap is the summary cap, and a tab whose CAP differs is refused. |
| 9 | low | "Last, First" from a full name is lossy. | Yes (`yahoo/models.py:55`) | Accepted. Parse Yahoo's `name.first` / `name.last`; the preview shows the text. |
| 10 | low | The writer reuses the Firestore key. | Yes (`context.py:145-149`) | Accepted and recorded. Phase 4b allows a separate key. |
| 11 | low | Three full reads per confirm; a TOCTOU window. | Inferred | Accepted. Two reads per confirm, latency measured in Phase 6, the window documented. |
| 12 | nit | The `FIELDS` merge, and `grid.Tab` has no ID field. | Yes (`sheets_api.py:26`) | Accepted. |
| 13 | nit | The earlier text had the cap-hit order wrong. | Yes (`free_agents.py:229-230`) | Accepted; corrected. |
| 14 | nit | itsdangerous signs but doesn't encrypt. | n/a | Accepted. Only a hash goes in the form. |
| 15 | nit | The property test's allowed set was vague. | Yes | Accepted; spelled out. |

Added after the review: the protection check (Phase 4) and the rehearsal
(Phase 6), from the conversation with the owner; and, from a final
self-check, binding the rows the app writes, the §4a reading-rule change, the
whole-tab protection test, and stored-snapshot defaults for the new name fields.

## 6. Owner steps

All are given one step at a time, when due. First the rehearsal steps (Phase 6),
then the real sheet: Editor share, protections, the tab's IR+ rows, and the
Vercel flag.

## 7. Open questions (answered by the owner, 2026-10-30)

1. **A move that breaks the cap.** Yahoo has already made the move. Should the
   app block the write, or write the truthful tab with a warning?
   **Owner: block, and show the reason (2026-10-30).** The owner fixes the Yahoo roster,
   and the stale sheet is the visible prompt.
2. **A tab that is inconsistent with itself** (its PAYROLL is not the SUM of
   its own counted rows, or its CAP differs). **Owner: refuse until it is
   fixed by hand (2026-10-30).** Pending drops don't count as an inconsistency.
3. **Overwriting formula cells** in the target rows. **Owner: refuse (2026-10-30).**
   The owner's workflow: check a move in the app first (the Replace view's cap
   check), then make it in Yahoo, then confirm the sheet update. The block is
   the safety net.
4. **One service account or two.** **Owner: keep the one account (2026-10-30).**
   The protections keep it off the other tabs, and a second account is more
   setup for you. Phase 4b leaves the door open.
5. **The flag's name and where it lives:** `FHA_SHEET_WRITE` (an env var the
   owner sets in Vercel), or an owner setting in Admin. **Owner: the env var (2026-10-30).** Turning on writes should take a deliberate deploy-side step.
