# Plan: update the owner's tab automatically

Status: revised after quality-mgr's plan review (AU-P1, not ready, 12 findings: 10 accepted in
full or in part, 2 rejected in part; triage in §6), 2026-11-27. Second review: AU-P2. The owner
needs no review of plans (2026-11-27); team-lead approves after the review's triage.

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
| Cost when nothing changed | A pre-check against the **stored** sheet first (no Google call). Google is read only when the stored tab differs from the Yahoo roster. Deliberate: a live edit to the tab since the app last read it is not seen until the next read (§2, step 6). |
| What it writes | Only a clean plan: no refusals, at least one edit, no ticks, and **every drop identified**: the row is bound to a player (a stored row binding) who is no longer on the roster. Anything else (over the cap, a row awaiting review, an unidentified row, a locked tab, other tabs unprotected, an unknown cap hit, ...) is skipped with a warning banner, logged, and left to the manual Update page, which stays. |
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

**Not done: cross-instance serialization of writes** (AU-P1 finding 1). `Repository` has no atomic
create or transaction; adding one to three backends (Firestore REST, the dev file, the in-memory
store) for a single-user app buys little: two runs on one snapshot compute the same edits from the
same tab and write the same values, and the overlap of runs on two snapshots is narrowed to the
batch itself by the pre-write check (§2, step 9) and corrected by the next fetch. Recorded as a
residual risk (§4).

## 2. What it means precisely

### Trigger ("unchecked snapshot")

`page_data` decides whether the page carries the trigger, for full pages only: every rated page
and Admin. **Not** the Update page (`/admin/sheet/update`, GET and the confirm's redirect): the
owner is updating by hand there. **Not** fragments (`/players/rows`): a fetch made by "Show more"
is picked up by the next full page while that snapshot is within the TTL. The page carries it when
all hold:

- writing is on (`svc.sheet_writer` and `svc.sheet` are set; never in the demo, which refuses
  `FHA_SHEET_WRITE=1`);
- the switch is on (`config/sheet_auto`, field `enabled`; a missing document is on);
- the served snapshot is within the TTL (`RefreshService.is_fresh`) and has no `refresh_error`
  (no failed refresh, no "not saved");
- its `fetched_at` is not the one stored as checked (`sheet_auto_state/last`, field `fetched_at`).

The manual confirm marks its forced snapshot as checked (it writes `sheet_auto_state/last` with
that `fetched_at` before it writes), so a manual update never hands a fetch to the automatic one.

Cost per full page view while writing is on: two small Firestore reads (the switch and
`sheet_auto_state/last`). While writing is off: none.

**First activation and re-enabling:** a snapshot fetched before the deploy, or while the switch
was off, is unchecked; if it is still within the TTL, the next full page runs the update for it.
That is a successful Yahoo read under 30 minutes old, the same data every page shows; the
freshness rule below holds for it.

The trigger is one element in `base.html`, above `<main>`: an empty `div` with
`hx-post="/sheet/auto-update"`, `hx-trigger="load"`, `hx-vals` holding the snapshot's `fetched_at`,
and `hx-swap="outerHTML"`. `render()` passes it from `request.state`, where `page_data` set it, so
no route changes except the Update page opting out.

### The request (`POST /sheet/auto-update`)

404 while writing is off or in the demo. The session is required as on every route (an expired
session answers htmx with 401 and `HX-Redirect`, `app.py`). Otherwise, with the posted
`fetched_at`, and the request's start time from the clock:

1. **Freshness.** Re-resolve the snapshot without fetching Yahoo (a new `RefreshService.peek()`:
   `_stored()` plus `_with_recent_failure()`, never a Yahoo call). Silent (no banner, no log)
   unless it is within the TTL, has no `refresh_error`, and its `fetched_at` equals the posted one.
   So a write follows a successful Yahoo read within the TTL. The posted value needs no signature:
   it is only compared, never trusted. A failed refresh known only to another instance doesn't make
   that read wrong; it is the data every page shows. Rechecked before the batch (step 9).
2. **Switch.** Off: silent.
3. **Claim.** `sheet_auto_state/last` already holds this `fetched_at`: silent. Else store it, keeping
   the document's other fields (step 9's marker). Best-effort (no atomic create): safety rests on
   the re-read, the digest and step 9, not on the claim.
4. **Unresolved earlier write.** If `sheet_auto_state/last` holds `uncertain_until` later than now
   (step 9): skip with "an earlier write's outcome isn't known yet". Nothing is sent while a batch
   may still land.
5. **Pre-check (stored sheet, no Google).** `load_league_sheet`; none stored: skip ("no league
   sheet has been read yet"). `prepare_update(repo, snapshot, stored)`: a `problem` (no tab bound,
   an unreadable tab, ...): skip with it, since the owner turned the update on and it can't work.
   A plan with no `changes`: silent (nothing to write; a row awaiting review with nothing to change
   shows on Admin as today). Else go on.
6. **Fresh read.** `read_league_sheet` (stores it, as every read does). A `LeagueSheetError` or a
   `RepositoryError`: failed, with a fixed sentence (§2 "Reasons"). The stored check in step 5 can
   miss a live edit to the tab since the app last read it (only the owner, or a commissioner, can
   edit it); the next manual preview or Admin read shows it. Deliberate, for the cost.
7. **Plan.** `prepare_update` on the fresh sheet, no ticks. A `problem`: skip with it. No changes:
   silent (the tab was already updated by hand). Refusals: skip with the first refusal.
8. **Identified drops.** Load the row bindings (`row_bindings`, this tab). Every row the plan
   drops must have a stored binding to a `player_id` not on the snapshot's roster. Any other drop
   (a row never matched, e.g. a misspelled name the matcher left unmatched): skip with "row N isn't
   matched to a player who left your roster: check it on Update my tab" (row number only).
   Changes that move a row into or out of IR, or fill rows, are identified by the plan already
   (matched rows, Yahoo players).
9. **Write.** `write_update(repo, clock, source, writer, snapshot, expected=update.digest,
   before_write=check)`. `before_write` is a new optional hook (default none: the manual confirm
   is unchanged), awaited after the guard and right before `writer.write`; when it returns a
   reason, `write_update` returns `refused` with it and nothing is sent. The auto path's `check`:
   - re-runs step 1 (still the newest snapshot, within the TTL, no error) and step 2 (switch on);
   - refuses when more than 40 s have passed since the request started ("ran out of time before
     writing"), leaving 20 s for the batch, read-back and log;
   - then stores `uncertain_until = now + 600 s` in `sheet_auto_state/last` and returns none.
   After the write, `uncertain_until` is cleared when the outcome is known (`written`, or
   `failed`: nothing sent or a 4xx), and kept when it is `unknown` (it expires after 10 minutes,
   as Google's "may still apply it" message suggests). A request killed after sending leaves the
   marker set, so the next run waits. The manual confirm sets and clears the same marker around its
   batch (in its route, not through the hook, so `write_update`'s manual behaviour is unchanged),
   so an automatic run never sends while a manual batch is unresolved.
   Cost: three sheet reads (steps 6, `write_update`'s own, the read-back), one write, no Yahoo
   call; each preparation loads aliases, bindings and free-agent salaries from Firestore.
10. **Log and banner** from the outcome.

### Outcomes

`WriteResult` gains three fields, set where each result is made, so callers can tell outcomes
apart without parsing text:

- `kind`:

  | `kind` | When |
  |---|---|
  | `written` | Written and read back as expected |
  | `up_to_date` | Nothing to write |
  | `refused` | Nothing sent, by a check: the digest changed, a refusal, protections, a locked tab, the guard, the `before_write` hook |
  | `unreadable` | Nothing sent: the sheet couldn't be read before writing |
  | `failed` | Google refused the batch or it failed before leaving: nothing written |
  | `unknown` | A lost answer not resolved; or sent (acknowledged, or found written after a lost answer) but the read-back, the storage afterwards, or the check failed |

- `sent`: true when the batch left (`written`, `unknown`);
- `payroll`: the PAYROLL read back when verified (`written`), else none.

`written` (cell count) keeps its meaning: the batch size when sent. The manual confirm keeps using
`ok` and `message`; its behaviour and its tests don't change (they gain `kind` assertions).

Auto outcomes: **written**, **skipped** (steps 4, 5, 7, 8; `refused`), **failed** (step 6;
`unreadable`; `failed`), **unknown** (`unknown`). Silent: steps 1–3, nothing to change.

### Reasons (hygiene, SPEC §4a)

What the log stores and the banner shows comes only from an allowlist:

- a fixed sentence per case: steps 4, 6, 8 and the hook's reasons above; `unreadable` → "the
  sheet couldn't be read"; `failed` → "Google refused the write, or it didn't leave; nothing was
  written"; `unknown` → "the write's outcome is unknown: check the tab"; a `refused` result from
  the digest → "the tab changed while updating; the next fetch tries again"; from protections or a
  locked tab → the same sentences as the manual page minus any exception text;
- a `problem` from `build_update` (the service's own sentences; the unreadable-tab one quotes the
  parser's `reason`, which names no cell);
- the plan's first refusal (the domain's wording: parsed rows within the ranges, out-of-block IR
  rows by number only, DECISIONS 2026-10-30 round 3).

Never a `LeagueSheetError`'s or `RepositoryError`'s text, which can quote Google, Firestore or sheet
metadata. Each `WriteResult` kind maps to its sentence, so the auto path never forwards
`result.message`.

**Changes** in a written entry: `"<action> <name>"` from the plan's changes. Drop and IR-move
names are parsed rows inside the payroll range or the IR block (the parsed ranges SPEC §4a allows
the app to persist and render; the stored sheet holds them already); a named IR row outside the
block never appears in changes (DECISIONS 2026-11-16, finding 3). Adds are Yahoo names.

### Log entry

One document per entry in `sheet_auto_log`, ID `<at in ms, 13 digits>-<8 random hex>` (sortable,
collision-safe), holding only `at` (epoch seconds), `outcome` (`written` | `skipped` | `failed` |
`unknown`), `changes` (list, written only), `cells` (`written`, the batch size when sent, else 0),
`payroll_after` (verified only, else none), `reason` (allowlisted, none for written). Listed by ID
descending. A skip whose `reason` equals the newest entry's, when that entry is also a skip, is not
logged again (its banner still shows). After each new entry, entries beyond the newest 50 are
deleted. A Firestore failure while logging is logged by type only and doesn't change the banner.

### Banner

The fragment that replaces the trigger, in the page's existing flash styles (`ok`, `error`),
escaped by Jinja, with allowlisted text only:

- **written** (ok): "Your tab was updated: drop A, add B (8 cells). PAYROLL is now $X." with a link
  "Log" to Admin's log.
- **skipped** / **failed** (error): "Your tab wasn't updated automatically: <reason>." with a link
  "Update my tab" (`/admin/sheet/update`).
- **unknown** (error): "The automatic update of your tab may not have gone through: <reason>.
  Check it on Update my tab." with the link.
- silent: an empty fragment.

### Admin

- In the League sheet card, next to the "Update my tab" link, shown only while writing is on: a
  form with a checkbox "Auto-update my tab" and Save (`POST /admin/sheet/auto`), flash as Admin's
  other forms.
- Below it, "Automatic updates": the log, newest first (time ago, outcome, changes or reason), or
  "None yet." Only while writing is on (one small collection read).

### Request origin

The new POSTs are protected as every POST in the app: the session cookie is `SameSite=Lax`, so a
cross-site form post carries no session and gets the login redirect. No CSRF token is added
(AU-P1 finding 10, rejected in part: §6). The automatic endpoint takes no content from the request
except the compared `fetched_at`; it writes only what the server computes.

### What doesn't change

The manual Update page's preview and confirm (apart from the checked-snapshot and uncertain marks
around its batch), the domain planner, the protections, the digest, the 10-minute confirm, the
forced Yahoo refresh on the manual confirm, Yahoo read-only, own tab only, values only.

## 3. Phases

Each phase one commit, gates green. No `domain/` module changes, so no mutmut run (if one turns out
to be needed, stop and ask team-lead).

1. **Service.** `RefreshService.peek()`; `WriteResult.kind`, `sent`, `payroll` in every return of
   `write_update`, and its `before_write` hook; a new `services/sheet_auto.py` with the switch
   (load/save), the state document (checked snapshot, `uncertain_until`), the log (append with
   dedupe and pruning, load newest first) and `auto_update(...)` returning an outcome (kind,
   banner text, log entry or none). Tests first, with the existing fakes (in-memory repository,
   fake sheet source, recording writer, fixed clock):
   - `write_update`: each return's `kind`, `sent` and `payroll`, including the lost-answer
     wrapper (found written; then storage fails, and read-back mismatch), an acknowledged batch
     whose read-back fails, and a `RepositoryError` while reading before the write (now
     `unreadable`, not an exception); the hook refusing sends nothing; no hook, unchanged
     behaviour;
   - `peek()` never calls the Yahoo source; it returns the recent-failure note;
   - each silent case writes nothing and logs nothing: outside the TTL, `refresh_error` (failed
     refresh and "not saved"), posted `fetched_at` not the served one, an unreadable newer stored
     snapshot (the older one is served: mismatch), switch off, already claimed, stored tab
     already matches, fresh tab already matches;
   - the stored tab matches: the sheet source is never called;
   - a clean, identified plan is written (the recording writer gets exactly the plan's edits),
     logged with its changes, cells and PAYROLL; `uncertain_until` is cleared;
   - each skip writes nothing and is logged with its reason: a `problem` on the stored sheet (no
     tab bound), over the cap, a row awaiting review, an unknown cap hit, other tabs unprotected,
     own tab locked, `uncertain_until` in the future;
   - **an unidentified drop**: a synthetic tab where a rostered player's row is misspelled past
     the matcher (unmatched, no binding) and the roster is unchanged: the writer receives no
     edits, the skip names the row by number; a bound row whose player left: written;
   - the hook: the snapshot superseded during preparation, the TTL passed during preparation, the
     switch turned off, more than 40 s elapsed (a clock the fake source advances): nothing sent;
   - a lost answer: `unknown`, `uncertain_until` kept; the next run within 10 minutes skips, after
     it runs;
   - hygiene: a sentinel string in the fake `LeagueSheetError` (on the step-6 read, on
     `write_update`'s own read, on a lost answer) and in a `RepositoryError` appears neither in the
     stored log entry nor in the banner text; an out-of-block IR row's name appears in neither;
   - the log: the same skip twice is one entry; a skip after a failed with the same sentence is
     logged; equal timestamps keep both entries in a stable order; the 51st entry deletes the
     oldest;
   - two runs on one snapshot (the claim bypassed): the second writes nothing (digest or no
     changes).
2. **Web.** `page_data` sets the trigger in `request.state` (full pages only); `render` passes
   it; `base.html` emits the element; the Update page opts out; the manual confirm marks the
   checked snapshot and sets and clears `uncertain_until` around its batch; `POST
   /sheet/auto-update` returns the banner fragment; Admin's switch form and log. Tests:
   - a page with an unchecked snapshot within the TTL carries the trigger with its `fetched_at`; a
     checked one, a stale one, one with `refresh_error`, the switch off, writing off and the demo
     carry none; the Update page and `/players/rows` never do;
   - after `/refresh` (POST, redirect), the page carries the trigger for the new snapshot;
   - a pre-existing unchecked snapshot within the TTL (first activation) carries it;
   - the manual confirm: its redirect carries no trigger, its snapshot is marked checked, and a
     lost answer there makes the next automatic POST skip with the earlier-write sentence (the
     writer's batch count stays 1);
   - the endpoint answers 404 with writing off and in the demo; without a session it redirects to
     login, and with `HX-Request` it answers 401 with `HX-Redirect`;
   - each banner's text and link; a skip links to Update my tab; a silent run returns an empty
     fragment;
   - Admin: the switch shows only while writing is on, saves, and the log lists entries newest
     first.
3. **Docs.** SPEC: §1 "Added after Phase 1" (writes on a tap, or automatically when the app fetches
   Yahoo, if the switch is on); §2 "Refresh model" ("no background jobs: nothing runs except in a
   request; the automatic tab update is a request the page makes after it loads"); §4a "Writing":
   the existing sentences "Before writing, ..." and "the one the owner saw" qualified as the manual
   path, and a paragraph "Automatic update" (§2 of this plan, short: trigger, freshness, identified
   drops, the hook, the marker, the log); §7.5 Admin (the switch and the log). DECISIONS: a dated
   entry recording the reversal of the 2026-10-30 answer, the owner's three decisions,
   team-lead's decisions with alternatives (including the rejected cross-instance lock and CSRF
   token), and what replaces the owner's tap as the check on a silent mistake: identified drops,
   the same guards and read-back, the banner on the page that triggered it, and the log on Admin.
   `docs/plans/league-sheet-update.md`: a dated note at its top ("writes only after the owner
   taps" is historical since 2026-11-27), under the answers table and on the risk row "A silent
   mistake", each pointing to the new entry (the history stays as written). This plan's status.

## 4. Risks

| Risk | Mitigation |
|---|---|
| A write the owner didn't look at | Only a clean plan whose drops are identified is written, through the same checks as a tap (digest, protections, guard, read-back); the banner says what was written; the log keeps it; Sheets version history allows copying old cells back. The switch turns it off. |
| A misspelled row dropped and its player re-added | Identified drops only (§2 step 8); an unidentified row goes to the manual page. |
| A write from an old roster | Freshness at admission and again right before the batch (§2 steps 1, 9). |
| Two runs on two snapshots overlapping (two Refresh taps seconds apart, two instances) | Residual. The hook narrows it to the batch itself (a superseded snapshot sends nothing); if the older batch still lands last, the next fetch corrects the tab. Two runs on one snapshot write the same values. No cross-instance lock (§1). |
| A second batch while one may still land | `uncertain_until`, set before every batch (manual and automatic), kept on an unknown outcome, 10 minutes. |
| A half-done Yahoo move written | Accepted (no settle delay); the next fetch completes it. |
| A live edit to the tab missed by the stored pre-check | Accepted for the cost; the manual page and Admin's read show it. |
| The time limit | The hook refuses after 40 s, before sending; three reads, one write, no Yahoo call. A kill after sending leaves the marker. |
| The log or banner leaks a Google or Firestore error's text | Allowlisted reasons; sentinel tests on every read and the lost answer. |
| Merge turns it on in production | On by default, by the owner's decision: if `FHA_SHEET_WRITE=1` is set there, the first full page while a fetched snapshot is unchecked and within the TTL (possibly the snapshot already cached at deploy) writes a clean, identified difference. Said in the PR, so the owner knows when he OKs the merge. |

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
