# Plan: the automatic tab update reads the roster fresh before writing

Status: **draft**, 2026-11-28, for quality-mgr's plan review. Priority fix. The owner needs no review of plans.

## The bug

The owner (2026-11-28): "if I move a player off ir the app does not update the spreadsheet. In fact if I manually
update the spreadsheet to take the player off ir the app moves him back to ir." In league 8076, Yahoo applies
roster moves at once.

**Root cause.** The automatic update (PR #29, `services/sheet_auto.py`) plans and writes from the **cached** Yahoo
snapshot, while it reads the sheet **live**.
- It runs for any saved generation that is still inside the 30-minute cache window and hasn't been checked yet
  (`web/data.py:164`, `RefreshService.eligible`).
- A generation stays unchecked when a fragment request did the fetch (`suppress_sheet_auto`: Show more, the
  Update page), or when a page was closed before its htmx check ran. The next full page within 30 minutes then
  checks it.
- So the plan can come from a roster up to 30 minutes old. Any Yahoo move or hand edit to the tab made after
  that fetch is "corrected" back to the old roster: the player moved off IR is written back into an IR row.
- The manual confirm doesn't have this problem: it forces a fresh Yahoo read before writing
  (`routes/sheet_update.py`, `page_data(request, force=True)`, "the write follows today's roster"). The automatic
  path never got that guard.

"Doesn't update" is the other side of the same window: a Yahoo move isn't seen until the app's next fetch (the
Refresh button, or opening the app after 30 minutes). That part is by design (PR #29: the update runs when the app
actually fetches Yahoo). The fix keeps it, and the reply to the owner says so.

## 0. Decisions

| Question | Decision |
|---|---|
| The guard | Before the automatic path plans against the live sheet, it reads the rosters fresh from Yahoo and plans from the snapshot with `teams` replaced by them. The manual path already forces a full fresh read and is unchanged. |
| Which Yahoo call | Only the rosters: the same request and parser the full refresh uses (`league/{league_key}/teams/roster`, `parse.parse_teams_rosters`, recorded fixture `009-...-teams-roster.json`), through a new `YahooSource.fetch_rosters(league_key) -> tuple[Team, ...]` (and the fake). That is one call, with no player pages and no stats, so it fits the automatic path's budget. A full forced refresh (as the manual confirm does) costs dozens of calls; rejected for a background write. |
| Where | In `_run_update`, after the stored-sheet pre-check (which still decides cheaply whether to go on) and before `read_league_sheet` / `prepare_update`. The fresh snapshot is what `prepare_update` and `write_update` get. The pre-check keeps using the cached snapshot: at worst it lets a check through, and the fresh plan then finds nothing to do. |
| The roster read fails | Skip, with nothing written: a new reason `roster_unreadable`, "your Yahoo roster couldn't be checked just now". Never fall back to the cached roster. |
| My team missing from the fresh read | Skip, as today (`problem_no_team`). |
| A player new since the snapshot | His cap hit comes from the free-agent salaries by player ID, as today. If it's unknown, the plan refuses (the existing refusal) and nothing is written. |
| The cache | Not updated by this read: the generation stays as fetched, and only the write uses the fresh rosters. |
| SPEC / DECISIONS | SPEC §4a "Writing" (the automatic path reads the rosters fresh before writing); DECISIONS: a dated entry (the bug, the cause, the guard, the alternatives). |

## 1. Alternatives

- **A. Read the rosters fresh before writing (chosen).** It closes the window to seconds whatever left the
  generation unchecked, at the cost of one Yahoo call, and only when the pre-check finds work.
- **B. Only write within N seconds of the fetch.** Rejected: a fetch made by a fragment still leaves a window, and
  N is arbitrary.
- **C. Skip when the tab changed after the snapshot.** Rejected: Sheets has no per-tab modified time, and other
  managers edit the file all day.
- **D. A full forced refresh, as the manual confirm does.** Rejected for the automatic path: dozens of calls inside
  a 35 s send budget.

## 2. Phases

1. **Fix, test-first** (one commit): the failing test first, the owner's case through `auto_update`. The cached
   snapshot has the player in `IR+`, the live tab has him in a counted row (a hand fix), and the fresh rosters have
   him on the bench. Today it writes him back to an IR row; after the fix it writes nothing ("up to date").
   Then:
   - the reverse case (a fresh move to IR is written);
   - a failed roster read skips with `roster_unreadable` and writes nothing;
   - my team missing from the fresh read;
   - a player added since the snapshot with no known cap hit is refused;
   - the fresh read happens only after the pre-check finds work (no Yahoo call when there's nothing to do);
   - `fetch_rosters` parses the recorded `009` fixture through respx.

   Existing tests that assume the cached roster get a fake `fetch_rosters` returning the snapshot's teams.
2. **Docs**: SPEC §4a, DECISIONS, this plan's status.

## 3. Stop-gap until it ships

Switch "Auto-update my tab" off in Admin, or tap Refresh after any Yahoo roster move and before editing the tab by
hand. The owner decides.
