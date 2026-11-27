# Plan: update the owner's tab automatically

Status: **approved by team-lead**, 2026-11-27, after three plan reviews: AU-P1 (not ready, 12
findings), AU-P2 (not ready, 8), AU-P3 (ready with changes, 6; all accepted). Triage in §6. **§2a
amends §2 and §3 and wins where they differ.** The owner needs no review of plans (2026-11-27).

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
| Cost when nothing changed | A pre-check against the **stored** sheet first (no Google call). Google is read when the stored tab differs from the Yahoo roster, when the stored sheet is over 24 h old (the league's window), or after an attempt whose outcome isn't known (§2). A live edit to the tab since the app last read it is seen within 24 h. |
| What it writes | Only a clean plan: no refusals, at least one edit, no ticks, and **every drop identified**: the row's own stored binding names a player no longer on the roster, and no two drops share a binding. A plan with something to change but anything else (a refusal such as over the cap, a row awaiting review or an unknown cap hit; an unidentified drop; a locked tab; other tabs unprotected) is skipped with a warning banner, logged, and left to the manual Update page, which stays. A plan with refusals and nothing to change is silent: nothing to write. |
| Settle delay | None. It writes what Yahoo shows; a mid-move state (a drop before its add) is written as it is, and the next fetch writes the rest. The league asks for the tab within 24 h; a half-done move costs nothing and a delay would need a timer the app doesn't have. |
| Log contents | Every automatic write, skip and failure, not the no-ops. A skip repeated with the same reason right after the same skip is not logged again. Shown on Admin. |
| Log storage and retention | Firestore, one document per entry in collection `sheet_auto_log`; the newest 50 are kept (older ones deleted after each new entry). |
| Banner wording | §2, "Banner". |
| SPEC "no background jobs" | Still true: everything runs in a request a browser makes. SPEC §2's wording is sharpened (Phase 3). |

## 1. Strategy critique

**A. A follow-up request per Yahoo fetch, keyed by the snapshot (chosen).** The page that serves a
snapshot nobody has checked yet carries an htmx element that POSTs to `/sheet/auto-update` once it
has loaded. That request checks the stored tab, reads the sheet fresh only if it differs, writes a
clean, identified plan through the existing `write_update`, logs, and returns a banner.
*For:* the page shows at once; the write gets its own time budget; it reuses the reviewed write
path (digest, protections, guard, lost-answer handling, read-back) with one optional hook; a
Refresh, whose POST redirects to a page, triggers it the same way as an expired cache.
*Against:* if the page is closed before the htmx request leaves, nothing runs until a page is
opened while that snapshot is still within the TTL; a nightly move is caught at the next fetch
after the owner opens the app. Acceptable: the league allows 24 h.

**B. Write inside the request that fetched Yahoo.** Rejected: a full Yahoo refresh already takes
much of the time limit, and a sheet read, write and read-back (10–20 s locally, DECISIONS
2026-10-30 rehearsal) on top risks the 60 s limit and makes the page slow.

**C. A scheduled job (Vercel cron).** Rejected: the owner chose "when the app fetches Yahoo", SPEC
§2 has no background jobs, and a cron would fetch Yahoo when nobody looks.

**D. Run on every page view (no per-fetch key).** Rejected: the owner said not on every page view,
and each would cost a stored-sheet check and a POST.

**Not done: a cross-instance lock** (AU-P1 finding 1, AU-P2 finding 1). `Repository` has no atomic
create or conditional write; a lease with safe expiry needs both, in three backends (Firestore REST,
the dev file, the in-memory store), for a single-user app. Instead, **every divergence is detected
and repaired** (§2 "Attempts"): each batch is announced by its own attempt record before it is sent
and removed only when its outcome is definite. A lost answer or a killed request keeps its record,
and any kept record makes the next run read Google afresh instead of trusting the stored sheet.
Two answered overlapping batches are usually caught by a read-back (the cells hold the other's
values: kept as unknown); the remaining orders (each read-back passes, and the stored copy ends up
newer than the tab) are caught by the 24-hour fresh read. Two runs on one snapshot write the same
values. Bounded at 24 h, for an overlap that needs two fetches seconds apart: accepted (§4).

## 2. What it means precisely

### Trigger ("unchecked snapshot")

The contract: **the first full page served after a Yahoo fetch, while that snapshot is the saved
one and within the TTL, runs the update for it.** In practice that is every fetch: a Refresh tap
redirects to a full page, and opening the app after the TTL renders one. Not covered (AU-P2
finding 4, accepted as a narrowed contract): a fetch made by a fragment (`/players/rows`, "Show
more") or by the Update page itself (its GET fetches Yahoo when the cache has expired), when no
other full page follows within the TTL; the next fetch runs it. On the Update page the owner is
updating by hand anyway.

`page_data` decides whether the page carries the trigger, for full pages only: every rated page
and Admin; not the Update page (`/admin/sheet/update`; the owner is updating by hand there) and not
fragments. The page carries it when all hold:

- writing is on (`svc.sheet_writer` and `svc.sheet` are set; never in the demo, which refuses
  `FHA_SHEET_WRITE=1`);
- the switch is on (`config/sheet_auto`, field `enabled`; a missing document is on);
- the served snapshot is within the TTL and has no `refresh_error`;
- its `fetched_at` is not the one stored as checked (`sheet_auto_state/last`, field `fetched_at`).

The manual confirm marks its forced snapshot as checked before it writes, so a manual update never
hands a fetch to the automatic one.

Cost per full page view while writing is on: two small Firestore reads (the switch and
`sheet_auto_state/last`). While writing is off: none.

**First activation and re-enabling:** a snapshot fetched before the deploy, or while the switch
was off, is unchecked; if it is still the saved one within the TTL, the next full page runs the
update for it.

The trigger is one element in `base.html`, above `<main>`: an empty `div` with
`hx-post="/sheet/auto-update"`, `hx-trigger="load"`, `hx-vals` holding the snapshot's `fetched_at`,
and `hx-swap="outerHTML"`. `render()` passes it from `request.state`, where `page_data` set it.

### Freshness (`eligible`)

A snapshot is **eligible** when the saved generation is that snapshot: the stored cache's meta
document (`stats_cache/meta`, read directly, not the process's memory) has `fetched_at` equal to
the posted value, that value is within the TTL, and this process knows no failed refresh after it
(`_recent_failure`). Reading the saved meta, not `_stored()`, rules out the two cases AU-P2 finding
3 names: a newer generation that failed to decode (the meta names the newer one: mismatch) and a
fetch whose save hasn't finished (the meta still names the older one: mismatch). The snapshot
itself comes from `RefreshService.current()` without force only after `eligible` holds, so no
Yahoo call can follow (it is fresh). A new `RefreshService.saved_generation()` returns the meta's
`fetched_at`; `eligible(posted)` combines it with the TTL and the failure check.

### Attempts (write-ahead records)

Collection `sheet_write_attempts`, one document per attempt, ID a random 16-hex token, holding
`at`, `deadline` (`at` + 600 s), `source` (`auto` | `manual`), `cells` (the batch size). No other
content. Each attempt:

- **puts** its record right before the batch (the last step before `writer.write`);
- **deletes** its own record when the outcome is definite: verified written, or definitely not
  applied (nothing left the process, or Google answered 4xx);
- **keeps** it otherwise: a lost answer, a 5xx, written but the read-back or storage afterwards
  failed, or the request killed.

No read-modify-write on a shared document, so one attempt can't erase another's record. Both
routes use it: the automatic path through the hook, the manual confirm around its `write_update`
call (in its route).

### The request (`POST /sheet/auto-update`)

404 while writing is off or in the demo; 403 unless the request is same-origin (`Origin` equal to
the app's own origin, or, without `Origin`, `Sec-Fetch-Site: same-origin`; finding 8). The session
is required as on every route (an expired session answers htmx with 401 and `HX-Redirect`). The
whole request body below runs inside one `try` that turns any `RepositoryError` into **failed**
("the app's storage failed"), never a 500. With the posted `fetched_at` and the request's start time:

1. **Eligible** (above), else silent (no banner, no log).
2. **Switch** off: silent.
3. **Claim.** `sheet_auto_state/last` already holds this `fetched_at`: silent. Else store it.
   Best-effort; safety rests on the attempts, the re-read, the digest and the hook.
4. **Earlier attempts.** `all(sheet_write_attempts)`. A record whose `deadline` is in the future:
   skip with "an earlier write's outcome isn't known yet". Records past their deadline: each is
   logged once as **unknown** ("an earlier write's outcome is unknown: check the tab", with its
   `source` and `cells`), deleted, and this run **forces a fresh read** (skips step 5).
5. **Pre-check (stored sheet, no Google)**, unless forced (step 4) or the stored sheet is over 24 h
   old. None stored: skip ("no league sheet has been read yet"). `prepare_update(repo, snapshot,
   stored)`: a `problem`: skip with its code; no `changes`: silent; else go on.
6. **Fresh read.** `read_league_sheet`. A `LeagueSheetError`: **failed** ("the sheet couldn't be
   read").
7. **Plan.** `prepare_update` on the fresh sheet, no ticks. A `problem`: skip. No changes: silent.
   Refusals: skip with the first refusal.
8. **Write.** `write_update(repo, clock, source, writer, snapshot, expected=update.digest,
   before_write=hook)`. `before_write` is a new optional parameter (default none: the manual
   confirm's behaviour is unchanged), awaited after the guard, given the **final** recomputed
   `SheetUpdate` and the row bindings it loads, returning `None` (go) or a reason code (nothing is
   sent, result `refused` with that code). The automatic hook, in order:
   - **identified drops** on the final plan (finding 2): load `row_bindings` for this tab; every
     dropped row's own key must have a binding whose `player_id` is not on the snapshot's roster,
     and no two dropped rows may share a key or a bound `player_id`; else `unidentified_drop`
     (row number only);
   - `eligible(posted)` again and the switch again (`superseded`, `switched_off`);
   - attempts again: any record with a future deadline: `earlier_unknown`;
   - the deadline: more than 25 s since the request started: `out_of_time`;
   - then **put the attempt record** and return `None`.
   The send itself has the writer's own limits (token and post, 10 s each, `httpx` client), so the
   worst case before an answer is about 45 s, leaving the read-back and log 15 s. A request killed
   after the put keeps its record (step 4 reconciles it next time).
9. **Log and banner** from the outcome; delete the attempt record if the outcome is definite.

### Outcomes

`WriteResult` gains, set where each result is made (finding 6):

- `kind`: `written` | `up_to_date` | `refused` | `unreadable` | `rejected` | `unknown`;
- `code`: a reason code from the allowlist below (none for `written`, `up_to_date`);
- `attempted`: cells in a batch that left the process (0 if none left);
- `applied`: `"yes"` (verified, or found written after a lost answer), `"no"` (nothing left, or
  Google answered 4xx), `"maybe"` (lost answer or 5xx, unresolved, or applied but not verified);
- `payroll`: the PAYROLL read back when verified, else none.

| `kind` | `applied` | When |
|---|---|---|
| `written` | yes | Written and read back as expected |
| `up_to_date` | no | Nothing to write |
| `refused` | no | Nothing sent, by a check: the digest changed (`tab_changed`), a refusal (`refusal`), protections (`others_unprotected`, `protections_unknown`), own tab locked (`own_tab_locked`), the guard (`guard`), a problem after the final read (`problem_*`), the hook (its code) |
| `unreadable` | no | The sheet couldn't be read before writing (`sheet_unreadable`), including a `RepositoryError` while storing it, and a `RepositoryError` while preparing (`storage_failed`) |
| `rejected` | no | Google answered 4xx (`google_refused`), or the batch never left (`not_sent`) |
| `unknown` | maybe / yes | A lost answer unresolved (`lost_answer`); found written after a lost answer, or acknowledged, but the read-back or the storage afterwards failed (`unverified`) |

Every lost-answer branch sets `attempted` to the batch size (today they omit the count). The manual
confirm keeps using `ok` and `message`; its tests gain `kind`, `code`, `attempted`, `applied`
assertions.

Auto outcomes: **written**, **skipped** (steps 4–5, 7; `refused`), **failed** (step 6; `unreadable`;
`rejected`; storage), **unknown** (`unknown`; a reconciled attempt). Silent: steps 1–3, nothing to
change.

### Reasons (hygiene, SPEC §4a)

The log stores and the banner shows **only** a fixed sentence per reason code, in one table in
`services/sheet_auto.py` (finding 5), plus, for `refusal`, the plan's first refusal (the domain's
own wording: parsed rows inside the ranges; out-of-block IR rows by number only). `problem_*` codes
(`problem_no_team`, `problem_unbound`, `problem_unreadable_tab`, `problem_old_sheet`) map to fixed
sentences: the unreadable-tab sentence never quotes the parser's `reason`, which can hold the
PAYROLL formula as written. Never a `message`, never an exception's text.

**Changes** in a written entry: `"<action> <name>"` from the plan's changes (parsed rows inside the
payroll range or IR block, which SPEC §4a lets the app persist and render; Yahoo names for adds).

### Log entry

One document per entry in `sheet_auto_log`, ID `<at in ms, 13 digits>-<8 random hex>`, holding only
`at`, `outcome` (`written` | `skipped` | `failed` | `unknown`), `code`, `source` (`auto`, or the
reconciled attempt's), `changes` (written only), `cells` (`attempted`), `payroll_after` (verified
only), `reason` (the fixed sentence, or the refusal). Listed by ID descending. A skip with the same
`code` and `reason` as the newest entry, when that entry is a skip, is not logged again (the banner
still shows). After each new entry, entries beyond the newest 50 are deleted. A Firestore failure
while logging is logged by type only.

### Banner

The fragment that replaces the trigger, in the existing flash styles, escaped, fixed text only:

- **written** (ok): "Your tab was updated: drop A, add B (8 cells). PAYROLL is now $X." with a link
  "Log" to Admin's log.
- **skipped** / **failed** (error): "Your tab wasn't updated automatically: <reason>." with a link
  "Update my tab".
- **unknown** (error): "The automatic update of your tab may not have gone through: <reason>.
  Check it on Update my tab." with the link.
- silent: an empty fragment.

### Admin

- In the League sheet card, next to "Update my tab", only while writing is on: a checkbox
  "Auto-update my tab" and Save (`POST /admin/sheet/auto`, same-origin check as above; flash as
  Admin's other forms).
- "Automatic updates": the log, newest first, or "None yet." Only while writing is on.

### What doesn't change

The manual Update page's preview and confirm (apart from the checked-snapshot mark and the attempt
record around its batch), the domain planner, the protections, the digest, the 10-minute confirm,
the forced Yahoo refresh on the manual confirm, Yahoo read-only, own tab only, values only.

## 2a. Amendments after AU-P3 (these override §2 and §3 where they differ)

1. **Recovery keeps its evidence until repaired** (finding 1). An expired attempt record is
   deleted only after a fresh read of the sheet has been **stored** (step 6 succeeded). Its
   "unknown" log entry has the ID `attempt-<attempt id>` (an idempotent `put`), so retries and
   concurrent recoveries log it once. The 24-hour rule is a cache-age threshold checked on the next
   eligible run whose dependencies work. A divergence the fresh read finds may need manual repair:
   e.g. a late batch added a player who has since left with no binding written (the request died
   before `bind_written_rows`): that drop is unidentified, so the run skips with the banner and the
   owner fixes it on the Update page. Tests: a forced read that fails, then a retry (the record
   stays until the retry's read is stored); a late batch that added an unbound, departed player
   (skip, no edits).
2. **The exact saved generation, cache-only** (finding 2). A new `RefreshService.load_saved(
   fetched_at) -> Cached | None` reads the stored cache only (meta and chunks), decodes it, and
   returns it only if its `fetched_at` is the one asked for and is within the TTL; it never calls
   `current()` or the Yahoo source, and ignores the process's memory. Undecodable or superseded:
   none, and the run is silent. The hook rechecks with the saved meta. The endpoint uses this, not
   `current()`. Tests through the real POST: A posted while B's save is stalled (A's roster is
   used, or nothing is sent; never B's); eligible meta with undecodable chunks (nothing sent); zero
   Yahoo calls in both.
3. **Attempt bookkeeping inside `write_update`, for both routes** (finding 3). `write_update` takes
   an optional `journal` (the attempts store and a `source`): right before sending it puts the
   record with the **final plan's** cell count, and after the outcome it deletes or keeps the
   record per §2 "Attempts". The manual confirm passes `journal` with `source="manual"`; the
   automatic path passes it with `source="auto"` plus its `before_write` hook (eligibility, drops,
   switch, unresolved attempts: auto only). A manual no-op, refusal or digest mismatch puts no
   record. **The manual confirm overrides an unresolved attempt**: the owner has just previewed the
   tab as read fresh, and the digest guarantees it is unchanged; the preview shows "An earlier
   write's outcome isn't known yet: check the tab before confirming" while one is unresolved.
   Tests: a manual no-op and a refusal put no record; a manual request killed after the put leaves
   a record with the correct cell count; an automatic run on a later eligible snapshot then skips
   until it expires.
4. **The deadline at the send boundary** (finding 4). `writer.write` takes `deadline` (monotonic
   seconds, from the clock) and checks it after awaiting the token and immediately before the
   HTTP post; past it, it raises `NotSentError`. The auto path's deadline is the request start +
   35 s; the manual path passes none. Before the boundary (deadline, token failure, cancellation),
   the record is deleted (`finally` around the pre-send part); after it, a lost or uncertain answer
   keeps it. Tests: a slow marker put and a slow token push past the deadline (nothing sent, no
   record left); cancellation before and after the boundary.
5. **Typed writer outcomes** (finding 5). The writer raises `NotSentError` (token failure, the
   deadline, a connection never established), `RejectedWriteError` (an HTTP 4xx: Google refused),
   and `UncertainWriteError` (a lost answer, a 5xx, **and any other non-200 status**:
   conservative). `attempted` is the batch size whenever the post was attempted (rejected or
   uncertain), else 0. `applied`: `yes` with `unverified` when the batch was acknowledged, or found
   written after a lost answer, but the read-back or storage afterwards failed (the existing
   "Written (N cells), but ..." stays); `maybe` with `lost_answer` when the tab is unchanged, mixed
   or unreadable after a lost answer. Both are kind `unknown`, and both keep the record. Tests: the
   real writer with respx: a token failure, a connect error, a 4xx, a 302 and a 5xx, a read
   timeout after sending.
6. **A known result survives journal failures** (finding 6). Only failures **before the outcome**
   become "failed". After `write_update` returns, a `RepositoryError` while logging or deleting the
   record doesn't change the banner (a verified write still says "Your tab was updated"); a failed
   delete keeps the record. Reconciling a record whose `attempt-<id>` log entry already says
   `written` deletes the record without logging "unknown" (and still forces the fresh read). Tests:
   a log failure and a delete failure after a verified write (the banner says updated); the next
   run's reconciliation of that kept record logs nothing new.
7. **Origin fallback** (AU-P3's disposition of AU-P2 finding 8). A mismatching `Origin` is
   rejected even when `Sec-Fetch-Site: same-origin` is present; `Sec-Fetch-Site` is used only when
   `Origin` is absent; neither header: rejected. Tests for each.

## 3. Phases

Each phase one commit, gates green. No `domain/` module changes, so no mutmut run (if one turns out
to be needed, stop and ask team-lead).

1. **Service.** `RefreshService.saved_generation()` and `eligible()`; `WriteResult`'s new fields in
   every return of `write_update`, and `before_write`; `services/sheet_auto.py` (switch, state,
   attempts, log, reason table, `auto_update`). Tests first, with the existing fakes:
   - `write_update`: every return's `kind`, `code`, `attempted`, `applied`, `payroll`, including
     each lost-answer branch, an acknowledged batch whose read-back fails, a `RepositoryError` while
     storing the pre-write read and while preparing (no exception escapes); the hook gets the final
     update; a hook code sends nothing; no hook: unchanged behaviour;
   - `eligible`: true for the saved generation within the TTL; false for a mismatch, outside the
     TTL, a recent failure in this process, an already-rendered trigger for A after a newer B is
     saved but undecodable, and B while B's save is stalled (the meta still names A); it never
     calls the Yahoo source;
   - each silent case writes and logs nothing; the stored tab matching means the sheet source is
     never called; a stored sheet over 24 h old is read afresh;
   - a clean, identified plan is written exactly, logged, its attempt record deleted;
   - each skip writes nothing and is logged with its code: a stored `problem`, over the cap, a
     review row, an unknown cap hit, unprotected tabs, own tab locked, an unexpired attempt;
   - identified drops on the final plan: a synthetic misspelled current player's row (no binding):
     no edits; two rows sharing a departed player's key or bound id: no edits; a binding removed
     between the preview plan and the final plan: no edits; a bound row whose player left: written;
   - the hook: superseded, switched off, out of time (a slow fake marker write or token), an
     attempt appearing during preparation: nothing sent, no record left;
   - attempts: a lost answer keeps the record; the next run within the deadline skips; after it,
     logs one unknown, deletes it and reads Google even though the stored sheet matches; a killed
     request (the record put, no outcome) is reconciled the same way; two attempts: one's success
     deletes only its own record; a manual attempt with a lost answer makes the automatic one skip;
   - hygiene: sentinel strings in a `LeagueSheetError` (each read, the write, the lost answer), a
     `RepositoryError`, and a synthetic unreadable PAYROLL formula containing a literal appear in
     neither the stored entry nor the banner; an out-of-block IR row's name appears in neither;
   - the log: dedupe only after the same skip; equal timestamps keep both; the 51st deletes the
     oldest.
2. **Web.** The trigger (`request.state`, `render`, `base.html`; the Update page and fragments opt
   out); the manual confirm's checked mark and attempt record; the endpoint; Admin's switch and log;
   the same-origin check. Tests:
   - the trigger on an unchecked eligible snapshot; none for checked, stale, errored, switch off,
     writing off, the demo, the Update page and `/players/rows`;
   - after `/refresh` (the button on Admin and the rated pages): the trigger for the new snapshot;
   - the Update page's GET after the TTL fetches Yahoo and carries no trigger, and the next full
     page within the TTL does (the narrowed contract, pinned);
   - first activation: a pre-existing unchecked eligible snapshot carries it;
   - the manual confirm: no trigger after its redirect, its snapshot marked checked, a lost answer
     there makes the next automatic POST skip (batch count stays 1);
   - the endpoint: 404 with writing off and in the demo; 403 for a cross-origin `Origin`, and for a
     same-site but different origin; allowed for the app's own origin; login redirect without a
     session, 401 with `HX-Redirect` for htmx; a `RepositoryError` anywhere gives a failed banner,
     not a 500;
   - each banner's text and link; a silent run returns an empty fragment;
   - Admin: the switch only while writing is on, saves, 403 cross-origin; the log newest first.
3. **Docs.** SPEC: §1 "Added after Phase 1"; §2 "Refresh model" ("no background jobs: nothing runs
   except in a request; the automatic tab update is a request the page makes after it loads"); §4a
   "Writing": the sentences "Before writing, ..." and "the one the owner saw" qualified as the manual
   path, and a paragraph "Automatic update" (trigger contract, eligibility, identified drops, the
   hook, attempts, the 24-hour read, the log); §7.5 Admin. DECISIONS: a dated entry recording the
   reversal of the 2026-10-30 answer, the owner's three decisions, team-lead's decisions with
   alternatives (the rejected lock with its 24-hour bound; the same-origin check instead of a
   token), and what replaces the owner's tap as the check on a silent mistake: identified drops,
   the same guards and read-back, attempt records, the banner and the log.
   `docs/plans/league-sheet-update.md`: a dated note at its top, under the answers table and on the
   risk row "A silent mistake", each pointing to the new entry. This plan's status.

## 4. Risks

| Risk | Mitigation |
|---|---|
| A write the owner didn't look at | Only a clean plan whose drops are identified on the final plan is written, through the same checks as a tap; banner and log; Sheets version history; the switch. |
| A misspelled row dropped and its player re-added | Identified drops on the final plan (§2 step 8). |
| A write from an old roster | `eligible` (the saved generation, within the TTL) at admission and in the hook. |
| Overlapping batches (two fetches seconds apart, two instances, manual and automatic) | Attempt records: an unresolved batch blocks the next and forces a fresh read once it expires; read-backs catch most orders; the 24-hour fresh read bounds the rest. No lock (§1). |
| A request killed after sending | Its attempt record stays; the next run logs it as unknown and reads Google afresh. |
| A half-done Yahoo move written | Accepted (no settle delay); the next fetch completes it. |
| A live edit to the tab missed by the stored pre-check | Seen within 24 h (fresh read), or at once on the manual page. |
| A fetch with no full page after it | The narrowed trigger contract (§2); the next fetch runs it. |
| The log or banner leaks a Google, Firestore or formula text | Reason codes with fixed sentences; sentinel tests. |
| A same-site page posting to the app | Same-origin check on both new POSTs. |
| Merge turns it on in production | On by default, by the owner's decision: if `FHA_SHEET_WRITE=1` is set there, the first full page while the saved snapshot is unchecked and within the TTL (possibly the one cached at deploy) writes a clean, identified difference. Said in the PR. |

## 5. Open questions

None. The decisions above are team-lead's (the owner asked not to be asked).

## 6. Review

### AU-P1 (quality-mgr, gpt-6.2-sol, at `aa61ab8`): not ready, 12 findings (3 High, 7 Medium, 2 Low)

The report is posted verbatim on PR #29. Each finding was checked against the code.

| # | Sev | Finding | Triage |
|---|---|---|---|
| 1 | High | Concurrent runs can both write; an older roster's batch can land last | **Accepted in part, as Medium.** Checked: `Repository.put` is create-or-replace, no atomic op (`repository.py:59`). Two runs on one snapshot write identical values (same tab, same roster), so that case is harmless. Added: the `before_write` hook rechecks the snapshot is still the newest right before the batch, and `uncertain_until` blocks a batch while another may land (§2 step 9). Rejected: a cross-instance lock (a new atomic primitive in three backends for a single-user app); the residual seconds-wide overlap is self-correcting at the next fetch (§1, §4) |
| 2 | High | An unmatched row can be dropped automatically without evidence the player left | **Accepted.** Checked: a row bound to a departed player falls through to `match()` (`league_sheet.py:296-303`), as does a misspelled current player's row; the planner drops any named unmatched row (`domain/sheet_update.py:172-176`). Auto writes only identified drops: a stored binding to a player not on the roster (§2 step 8); test with a synthetic misspelling |
| 3 | High | A manual write with a lost answer can trigger a second automatic write | **Accepted.** The Update page and its confirm carry no trigger; the confirm marks its snapshot checked; `uncertain_until` is set before every batch, manual or automatic, kept on unknown (§2) |
| 4 | Medium | `peek()` doesn't establish freshness across instances or during preparation | **Accepted in part.** Freshness is rechecked right before the batch (hook). Kept: a successful Yahoo read within the TTL is the rule; a failed refresh known only to another instance doesn't make that read wrong (it's what every page shows). Tests: unreadable newer snapshot, TTL passing during preparation |
| 5 | Medium | Fragments miss fetches; a cached fetch at activation runs without a new fetch | **Accepted.** Checked: `/players/rows` calls `page_data` and renders `_player_rows.html` (`players.py:82,105`). Fragments carry no trigger; the next full page within the TTL picks the snapshot up. First activation and re-enabling stated and tested; the rollout risk row corrected |
| 6 | Medium | The stored pre-check hides blockers and can claim a false no-op | **Accepted in part.** A `problem` is now a skip (banner, logged once). A plan with refusals and no changes stays silent: nothing to write, and Admin shows review rows as today. The missed live edit is stated as deliberate (§2 step 6) |
| 7 | Medium | Messages forward exception text; change names come from Sheets | **Accepted** for exception text: allowlisted reasons per kind for both log and banner, sentinel tests on every read and the lost answer (checked: `sheet_update.py:241` interpolates `{e}`). **Rejected** for change names: drop and IR names are parsed rows inside the payroll range or IR block, which SPEC §4a lets the app persist and render (the preview shows them; the stored sheet holds them); out-of-block IR rows never appear in changes (DECISIONS 2026-11-16 finding 3). A test pins the out-of-block case |
| 8 | Medium | The outcome contract lacks payroll and sent state; pre-write storage errors escape | **Accepted.** `WriteResult` gains `kind`, `sent`, `payroll`; a `RepositoryError` in the pre-write read becomes `unreadable` (checked: only `LeagueSheetError` is caught, `sheet_update.py:240`); the tests listed in Phase 1 |
| 9 | Medium | No reserve in the 60 s budget | **Accepted.** The hook refuses after 40 s, before sending; a kill after sending leaves the marker; a slow-dependency test |
| 10 | Medium | The new POSTs need a request-origin rule | **Accepted in part.** The htmx 401 + `HX-Redirect` and login-redirect tests are added. **Rejected:** a CSRF token. Checked: every POST in the app relies on the `SameSite=Lax` session cookie (`app.py:257`), which a cross-site POST doesn't carry; the automatic endpoint takes no content but a compared timestamp and writes only what the server computes. Recorded in DECISIONS |
| 11 | Low | Time-only log IDs collide; dedupe can hide a skip after a failure | **Accepted.** ID with a random suffix; dedupe only against a previous skip; tests |
| 12 | Low | Docs must qualify the manual-only sentences | **Accepted.** SPEC §4a's "Before writing" and "the one the owner saw" qualified; a dated note at the top of the old plan (Phase 3) |

### AU-P2 (quality-mgr, gpt-6.2-sol, at `b4f08ba`): not ready, 8 findings (1 High, 7 Medium)

The report is posted verbatim on PR #29. Each finding was checked against the code. quality-mgr
accepted AU-P1's answers to findings 4 (the TTL contract), 7 (change names), 11 and 12.

| # | Sev | Finding | Triage |
|---|---|---|---|
| 1 | High | The shared marker permits overlapping batches, can be cleared by another attempt, and a late older batch after a newer stored read-back makes the pre-check skip Google indefinitely | **Accepted.** Checked: `put` is create-or-replace (`repository.py:59`); the read-back stores the sheet (`league_sheet.py:49`); the pre-check is silent on no changes. Replaced the marker with write-ahead attempt records, one document per attempt, deleted only by their own attempt and only on a definite outcome; an expired record is logged as unknown and forces a fresh read; a 24-hour fresh read bounds the remaining orders (§1, §2 "Attempts"). **Rejected in part:** a conditional-acquisition lock (no atomic primitive in `Repository`; the divergence it prevents is now detected and repaired, bounded at 24 h) |
| 2 | Medium | The drop proof uses a reusable key and applies only to the preliminary plan | **Accepted.** Checked: `row_key` is name + position group (`free_agents.py:45`); bindings persist after a row stops matching (`league_sheet.py:355`). The check moves into the hook, on the final recomputed plan; no two drops may share a key or a bound player; tests for reuse and a binding removed between plans |
| 3 | Medium | A matching posted timestamp doesn't prove the saved generation | **Accepted.** Checked: `_stored()` serves memory when the newer stored generation is unreadable (`refresh.py:197-201`), and memory is set before the save (`refresh.py:145-147`). `eligible` reads the saved meta directly; tests for both cases |
| 4 | Medium | A fetch by the Update page, or by a fragment, has no follow-up | **Accepted as a narrowed contract.** quality-mgr corrected the finding after the report: the Refresh button is on Admin (and the rated pages); the gap is the Update page's own GET, which fetches Yahoo after the TTL (`sheet_update.py:81`, `data.py:128`). The contract is now "the first full page after a fetch, while it is the saved one within the TTL"; both gaps are stated and the Update-page case is pinned by a test; the next fetch runs it |
| 5 | Medium | `problem` text can carry the PAYROLL formula; `refused` has no reason discriminator | **Accepted.** Checked: `parse.py:175` quotes the formula; `sheet_update.py:104` interpolates `tab.reason`. Reason codes with fixed sentences for every kind; the unreadable-tab sentence never quotes the reason; a sentinel formula test |
| 6 | Medium | Transmission and application are conflated; preparation storage failures escape | **Accepted.** Checked: a 4xx is raised after the post (`writer.py:118-131`); the lost-answer returns omit the count (`sheet_update.py:295-315`). `WriteResult` gains `attempted` and `applied` (yes/no/maybe) with a table; `RepositoryError` during preparation becomes `unreadable`/`storage_failed`; the endpoint turns any `RepositoryError` into a failed banner |
| 7 | Medium | The cutoff precedes awaited work; a killed request leaves no record | **Accepted.** Checked: `writer.write` awaits `_token()` before posting (`writer.py:113`). The cutoff is 25 s and comes before the record's put; the writer's own 10 s limits bound the rest (about 45 s worst case); a killed request leaves its attempt record, which the next run reconciles into the log |
| 8 | Medium | The Admin switch has no same-site origin protection | **Accepted.** A same-origin check (`Origin`, else `Sec-Fetch-Site`) on both new POSTs; tests for cross-origin and same-site different-origin requests |

AU-P1's finding 6 note (§0's "anything else" against the silent refusal-only plan) is fixed in §0.

### AU-P3 (quality-mgr, gpt-6.2-sol, at `31848da`): ready with changes, 6 findings (6 Medium)

The report is posted verbatim on PR #29. Each finding was checked against the code; all accepted, in
§2a.

| # | Sev | Finding | Triage |
|---|---|---|---|
| 1 | Medium | Recovery deletes its evidence before a fresh read is stored; a found divergence may be unrepairable automatically | **Accepted** (§2a.1). Checked: the read awaits the source, then stores (`league_sheet.py:48-49`); `bind_written_rows` runs only after the write (`sheet_update.py:326`) |
| 2 | Medium | `current()` after an eligible check can return a different roster or fetch Yahoo | **Accepted** (§2a.2). Checked: memory is published before the save (`refresh.py:145-147`); an undecodable cache returns None (`refresh.py:60`) and `current()` then fetches (`refresh.py:137`) |
| 3 | Medium | The manual route can't record the final batch size | **Accepted** (§2a.3). Checked: the route has only the digest (`routes/sheet_update.py:113`); the plan is computed inside `write_update` (`sheet_update.py:242`). The journal moves into `write_update`; the manual confirm overrides an unresolved attempt, with a warning on the preview |
| 4 | Medium | The 25 s check doesn't bound the send | **Accepted** (§2a.4). Checked: `writer.write` awaits the token (`writer.py:113`) behind a lock (`google_auth.py:78`). The deadline is checked in the writer right before the post |
| 5 | Medium | The writer can't distinguish not-sent, rejected and other statuses | **Accepted** (§2a.5). Checked: one `LeagueSheetError` for the token failure, the connection and every non-200 below 500 (`writer.py:115,122,128-131`), caught together (`sheet_update.py:284`) |
| 6 | Medium | A journal failure after a verified write could say "wasn't updated" | **Accepted** (§2a.6). Checked: `_read_back` returns a verified `WriteResult(True, ...)` (`sheet_update.py:439-444`) |

The plan is approved after this triage (team-lead, 2026-11-27).
