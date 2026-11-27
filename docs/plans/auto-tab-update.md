# Plan: update the owner's tab automatically

Status: draft, 2026-11-27, awaiting quality-mgr's plan review (task AU-P1). The owner needs no
review of plans (2026-11-27); team-lead approves after the review's triage.

The owner's request (2026-11-27, relayed by the operator): update the owner's tab in the league
sheet automatically, instead of only on a tap on Admin → Update my tab.

## 0. What the owner decided, and what team-lead decided

**This reverses an owner decision.** On 2026-10-30 the owner answered "write only on a tap (never
in the background)" (`docs/plans/league-sheet-update.md` §0, the answers table; SPEC §1 "Added after
Phase 1"; SPEC §4a "Writing"; DECISIONS 2026-10-30, "League sheet: updating the owner's tab"). On
2026-11-27 the owner asked for the automatic update. DECISIONS records the reversal (Phase 3).

The owner decided (2026-11-27):

| Question | Answer |
|---|---|
| When | Whenever the app actually fetches Yahoo: the Refresh tap, or opening the app after the 30-min cache has expired. Not on every page view. |
| Switch | An Admin switch "Auto-update my tab", **on** by default. `FHA_SHEET_WRITE=1` still gates all writing. |
| Record | A log of the automatic updates. |

The operator proposed, and team-lead decides (the owner asked that design choices not wait on him):

| Question | Decision |
|---|---|
| Where it runs | In a separate request the page makes after it shows (htmx, `hx-trigger="load"`), with its own 60 s budget (Vercel `maxDuration`), never inside the page request that fetched Yahoo. The result is a banner on that page. |
| Cost when nothing changed | A pre-check against the **stored** sheet first (no Google call). Google is read only when the stored tab differs from the Yahoo roster. |
| What it writes | Only a clean plan: no refusals, at least one edit, no ticks. Anything needing a decision (over the cap, a row awaiting match review, a locked tab, other tabs unprotected, an unknown cap hit, ...) is skipped with a warning banner, logged, and left to the manual Update page, which stays. |
| Settle delay | None. It writes what Yahoo shows; a mid-move state (a drop before its add) is written as it is, and the next fetch writes the rest. The league asks for the tab within 24 h; a half-done move costs nothing and a delay would need a timer the app doesn't have. |
| Log contents | Every automatic write, skip and failure, not the no-ops. A skip repeated with the same reason is not logged again. Shown on Admin. |
| Log storage and retention | Firestore, one document per entry in collection `sheet_auto_log`; the newest 50 are kept (older ones deleted after each new entry). |
| Banner wording | §3, "Banner". |
| SPEC "no background jobs" | Still true: everything runs in a request a browser makes. SPEC §2's wording is sharpened (Phase 3). |

## 1. Strategy critique

**A. A follow-up request per Yahoo fetch, keyed by the snapshot (chosen).** The page that serves a
snapshot nobody has checked yet carries an htmx element that POSTs to `/sheet/auto-update` once it
has loaded. That request checks the stored tab, reads the sheet fresh only if it differs, writes a
clean plan through the existing `write_update`, logs, and returns a banner.
*For:* the page shows at once; the write gets its own time budget; it reuses the reviewed write
path (digest, protections, guard, lost-answer handling, read-back) unchanged; a Refresh, whose POST
redirects to a page, triggers it the same way as an expired cache.
*Against:* if the page is closed before the htmx request leaves, nothing runs until a page is
opened while that snapshot is still fresh (see "Freshness" below); a nightly move is caught at the
next fetch after the owner opens the app. Acceptable: the league allows 24 h.

**B. Write inside the request that fetched Yahoo.** Rejected: a full Yahoo refresh already takes
much of the time limit, and a sheet read, write and read-back (10–20 s locally, DECISIONS
2026-10-30 rehearsal) on top risks the 60 s limit and makes the page slow.

**C. A scheduled job (Vercel cron).** Rejected: the owner chose "when the app fetches Yahoo", SPEC
§2 has no background jobs, and a cron would fetch Yahoo when nobody looks.

**D. Run on every page view (no per-fetch key).** Rejected: the owner said not on every page view,
and each would cost a stored-sheet check and a POST.

## 2. What it means precisely

### Trigger ("unchecked snapshot")

`page_data` (every rated page, Admin, the update page) decides whether its response carries the
trigger. It does when all hold:

- writing is on (`svc.sheet_writer` and `svc.sheet` are set; never in the demo, which refuses
  `FHA_SHEET_WRITE=1`);
- the switch is on (`config/sheet_auto`, field `enabled`; a missing document is on);
- the served snapshot is fresh (`RefreshService.is_fresh`) and has no `refresh_error` (no failed
  refresh, no "not saved");
- its `fetched_at` is not the one stored as last checked (`sheet_auto_state/last`, field
  `fetched_at`).

Cost per page view while writing is on: two small Firestore reads (the switch and the last-checked
document). While writing is off: none.

The trigger is one element in `base.html`, above `<main>`: an empty `div` with
`hx-post="/sheet/auto-update"`, `hx-trigger="load"`, `hx-vals` holding the snapshot's `fetched_at`,
and `hx-swap="outerHTML"`. `render()` passes it from `request.state`, where `page_data` set it, so
no route changes.

### The request (`POST /sheet/auto-update`)

404 while writing is off or in the demo. Otherwise, with the posted `fetched_at`:

1. **Freshness (server side).** Re-resolve the snapshot without fetching Yahoo (a new
   `RefreshService.peek()`: the stored or in-memory snapshot plus any recent failure note, never a
   Yahoo call). Return nothing (no banner, no log) unless it is fresh, has no `refresh_error`, and
   its `fetched_at` equals the posted one. So a write always follows a roster read from Yahoo
   within the TTL with no error, as the manual confirm's forced refresh does (DECISIONS
   2026-11-16, finding 1). The posted value needs no signature: it is only compared, never
   trusted.
2. **Switch.** Off: nothing.
3. **Claim.** If `sheet_auto_state/last` already holds this `fetched_at`, nothing; else store it.
   `Repository` has no create-if-absent, so the claim is best-effort: two requests at the same
   moment can both pass. Safety does not rest on the claim: each run re-reads the sheet and
   `write_update` recomputes the plan and writes only if its digest matches, so a second run finds
   nothing left to change, or a changed tab, and writes nothing wrong (it may log a skip, §2
   "Outcomes").
4. **Pre-check (stored sheet, no Google).** `load_league_sheet`; none stored: skip ("no league
   sheet has been read yet"). `prepare_update(repo, snapshot, stored)`: a `problem`, or a plan with
   no `changes`, means nothing to do: return nothing, log nothing. (A plan with refusals and no
   changes has nothing to write either.)
5. **Fresh read.** `read_league_sheet` (stores it, as every read does). An error: a failure,
   logged without the error's text (§2 "Log entry").
6. **Plan.** `prepare_update` on the fresh sheet, no ticks. A `problem`: skip with it. No changes:
   nothing (the tab was already updated by hand). Refusals: skip with the first refusal.
7. **Write.** `write_update(repo, clock, source, writer, snapshot, expected=update.digest)`,
   unchanged. It reads the sheet once more, checks the digest, the protections and the guard,
   writes, binds the new rows and reads the tab back, with the reviewed lost-answer handling.
   Cost: one extra sheet read against a refactor of the reviewed path; three sheet reads, one
   write, no Yahoo call: well inside 60 s.
8. **Log and banner** from the outcome.

### Outcomes

`WriteResult` gains a `kind`, set where each message is made, so the auto path can tell outcomes
apart without parsing text:

| `kind` | When |
|---|---|
| `written` | Written and read back as expected |
| `up_to_date` | Nothing to write |
| `refused` | Nothing written, by a check: the digest changed, a refusal, protections, a locked tab, the guard, the sheet unreadable before writing |
| `failed` | The write was refused by Google or failed before leaving: nothing written |
| `unknown` | A lost answer not resolved, or written but the read-back or the storage afterwards failed |

The manual confirm keeps using `ok` and `message`; its behaviour and its tests don't change.

Auto outcomes: **written**, **skipped** (a problem, a refusal, a `refused` result), **failed** (the
fresh read failed, or `failed`), **unknown** (`unknown`). Nothing to do, a stale snapshot, the
switch off or an already-claimed snapshot are silent: no banner, no log.

### Log entry (hygiene, SPEC §4a)

One document per entry in `sheet_auto_log`, ID from the time (sortable), holding only:

- `at`: epoch seconds;
- `outcome`: `written` | `skipped` | `failed` | `unknown`;
- `changes`: for a write, the plan's changes as `"<action> <player name>"` (Yahoo player names,
  as the preview shows them); else empty;
- `cells`: cells written (0 unless written);
- `payroll_after`: for a write, the PAYROLL read back; else none;
- `reason`: for a skip, the `problem` or the plan's first refusal (the domain's own wording, which
  names out-of-block IR rows by number only, DECISIONS 2026-10-30 round 3); for a refused result,
  its message; for **failed** and **unknown**, a fixed sentence per case
  ("the sheet couldn't be read", "Google refused the write", "the write's outcome is unknown: check
  the tab"), **never** a `LeagueSheetError`'s or `RepositoryError`'s text, which can quote Google
  or Firestore.

A skip whose `reason` equals the newest entry's `reason` is not logged again (an over-the-cap tab
would otherwise fill the log every 30 min and push the writes out); its banner still shows. After
each new entry, entries beyond the newest 50 are deleted. Firestore's own errors while logging are
logged by type only and don't change the banner.

### Banner

Returned as the fragment that replaces the trigger; the page's existing flash styles (`ok`,
`error`). Text is escaped by Jinja as usual.

- **written** (ok): "Your tab was updated: drop A, add B (8 cells). PAYROLL is now $X." with a link
  "Log" to Admin's log.
- **skipped** (error): "Your tab wasn't updated automatically: <reason>." with a link "Update my
  tab" (`/admin/sheet/update`).
- **failed** (error): "Your tab wasn't updated automatically: <the write's message>." with the same
  link. The banner may show the full message, as the manual flash does; the log doesn't.
- **unknown** (error): "The automatic update of your tab may not have gone through: <message>.
  Check it on Update my tab." with the link.

### Admin

- In the League sheet card, next to the "Update my tab" link, shown only while writing is on: a
  form with a checkbox "Auto-update my tab" and Save (`POST /admin/sheet/auto`), flash as Admin's
  other forms.
- Below it, "Automatic updates": the log, newest first (time ago, outcome, changes or reason), or
  "None yet." Only while writing is on.

### What doesn't change

The manual Update page and its confirm, the write path, the domain planner, the protections, the
digest, the 10-minute confirm, the forced Yahoo refresh on the manual confirm, Yahoo read-only,
own tab only, values only.

## 3. Phases

Each phase one commit, gates green. No `domain/` module changes, so no mutmut run (if one turns out
to be needed, stop and ask team-lead).

1. **Service.** `RefreshService.peek()`; `WriteResult.kind` set in every branch of `write_update`
   (existing tests keep passing; each kind pinned by a new assertion in the existing branch tests);
   a new `services/sheet_auto.py` with the switch (load/save), the claim, the log (append with
   dedupe and pruning, load newest first) and `auto_update(...)` returning an outcome (kind, banner
   text, log entry or none). Tests first, with the existing fakes (in-memory repository, fake sheet
   source, recording writer, fixed clock):
   - each silent case writes nothing and logs nothing: stale snapshot, `refresh_error` (failed
     refresh and "not saved"), posted `fetched_at` not the served one, switch off, already claimed,
     stored tab already matches, fresh tab already matches;
   - the stored tab matches: the sheet source is never called;
   - a clean plan is written (the recording writer gets exactly the plan's edits), logged with its
     changes, cells and PAYROLL;
   - each skip writes nothing: over the cap, a row awaiting review, an unknown cap hit, other tabs
     unprotected, own tab locked; each logged with its reason;
   - a fresh read that fails, a Google 4xx and a lost answer: logged with the fixed sentences, and
     the error's text appears nowhere in the stored entry (a sentinel string in the fake error);
   - the same skip twice: one entry; a different reason: two; 51 entries: the oldest is gone;
   - two runs on one snapshot (the claim bypassed): the second writes nothing.
2. **Web.** `page_data` sets the trigger in `request.state`; `render` passes it; `base.html` emits
   the element; `POST /sheet/auto-update` returns the banner fragment (an empty fragment when
   silent); Admin's switch form and log. Tests:
   - a page with an unchecked fresh snapshot carries the trigger with its `fetched_at`; a checked
     one, a stale one, one with `refresh_error`, the switch off, writing off and the demo carry none;
   - after `/refresh` (POST, redirect), the page carries the trigger for the new snapshot;
   - the endpoint answers 404 with writing off and in the demo, and requires the session (redirect
     to login without it) like every other route;
   - each banner's text and link; a skip links to Update my tab;
   - Admin: the switch shows only while writing is on, saves, and the log lists entries newest first.
3. **Docs.** SPEC: §1 "Added after Phase 1" (writes on a tap, or automatically when the app fetches
   Yahoo, if the switch is on); §2 "Refresh model" ("no background jobs: nothing runs except in a
   request; the automatic tab update is a request the page makes after it loads"); §4a "Writing" (a
   paragraph "Automatic update": §2 of this plan, short); §7.5 Admin (the switch and the log).
   DECISIONS: a dated entry recording the reversal of the 2026-10-30 answer, the owner's three
   decisions, team-lead's decisions with alternatives, and what replaces the owner's tap as the
   check on a silent mistake: the same guards and read-back, the banner on the page that triggered
   it, and the log on Admin. `docs/plans/league-sheet-update.md`: a note under the answers table and
   on the risk row "A silent mistake" pointing to the new entry (the history stays as written).
   This plan's status.

## 4. Risks

| Risk | Mitigation |
|---|---|
| A write the owner didn't look at | Only a clean plan is written, through the same checks as a tap (digest, protections, guard, read-back); the banner says what was written; the log keeps it; Sheets version history allows copying old cells back. The switch turns it off. |
| A write from an old roster | Server-side freshness check: fresh, no error, the posted `fetched_at` (§2 step 1). |
| A half-done Yahoo move written | Accepted (no settle delay); the next fetch completes it. |
| Two runs at once | Best-effort claim; the re-read and digest stop a wrong write. |
| The log leaks a Google or Firestore error's text | Fixed sentences for failures; a test with a sentinel. |
| A Yahoo name or a refusal in the log | Only parsed fields (Yahoo names, refusal wording that already avoids contact rows), as the update page already shows. |
| Merge turns it on in production | On by default, by the owner's decision: if `FHA_SHEET_WRITE=1` is set there, the first page after the next Yahoo fetch writes a clean difference. Said in the PR, so the owner knows when he OKs the merge. |

## 5. Open questions

None. The decisions above are team-lead's (the owner asked not to be asked); the review may
challenge them.

## 6. Review

(quality-mgr, task AU-P1: pending.)
