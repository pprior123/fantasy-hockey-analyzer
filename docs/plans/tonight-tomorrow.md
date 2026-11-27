# Plan: tonight and tomorrow

Status: **approved**, 2026-11-27, after quality-mgr's plan review (TT-P1: ready with changes, 11 findings, all
accepted; triage in §6). The owner needs no review of plans (2026-11-27).

The owner's request (2026-11-27, relayed by the operator): "can you add another option to be 'tonight and
tomorrow?'". It extends PR #30 (`docs/plans/plays-tonight.md`): a second option beside Tonight on Players and
Rosters. The owner thinks of it as a quick add, so this plan reuses PR #30's machinery and changes no domain code.
Team-lead decides the details (the owner asked that design choices not wait on him).

**Revised the same day** (operator relay `relay-2026-11-27-schedule-and-suggester.md`, which replaces the first
relay): the options are **Tonight** and **Tonight + tomorrow**, on Players and Rosters, **with each player's game
count for the chosen option**. No day-of-week picker and no "next 3 days" (the owner: "a team NEVER plays 3 days in
a row in the nhl"). A weekly player suggester is a separate, later plan and PR.

## 0. Decisions

| Question | Decision |
|---|---|
| "Tomorrow" | The Eastern date of "tonight" plus one calendar day (`date + timedelta(days=1)`, not +24 h), so it follows PR #30's US/Eastern rule. Times stay in ET. |
| Parameter | The existing `tonight` parameter takes a second value: absent (no filter), `1` (Tonight), `2` (Tonight + tomorrow). Anything else is still a 400 on Players (the "`tonight=2` is a 400" test moves to `tonight=3`). Every link and the Apply hidden-field list already carry `tonight`, so nothing new is threaded. `PlayersQuery.tonight: bool` becomes `nights: int` (0, 1 or 2). |
| Players chips | Two mutually exclusive chips in the Schedule row: **Tonight** and **Tonight + tomorrow**. Tapping the lit chip clears the parameter; tapping the other switches to it. A new search and Clear drop it, as now. A bare `?tonight=2` keeps PR #28's Free Agents default, as `tonight=1` does. |
| Filter | Tonight + tomorrow keeps players whose team (through `team_code`) has a regular-season or playoff game tonight **or** tomorrow: the union of the two slates' team codes, passed as `playing` to the existing `select` and `zero_gp_hidden` (they take a collection of codes). The count, the 0-GP note and paging follow it, as for Tonight. |
| Mixed states | The filter applies only when **both** days are available (fresh or stale). If either day is unavailable, the page shows the normal list for its other filters, leaves the chip unlit and says which day is unavailable (PR #30's rule: an outage never filters to an empty list, and a one-day filter would hide players who play only on the missing day). An off day is available: tonight with no games and tomorrow with games filters to tomorrow's teams. Stale days show their own age note. |
| Marks | Tonight's mark is unchanged (`@BOS 7:00p`). With the two-day option on, tomorrow's mark follows on its own line, prefixed with the weekday: `Sat vs NYR 1:00p`. A team playing both nights (a back-to-back) shows both marks. Tomorrow's marks appear only when the two-day option is on, so the default pages look as they do today. Each mark is a block line (`.game-mark { display: block }`); the longest, `Sat vs NYR 10:00p` (17 characters at .75rem), fits under the Team code at phone width and widens the column slightly. |
| Game count | A narrow numeric column **Gm**: the player's team's games in the chosen option (0 or 1 for Tonight; 0, 1 or 2 for Tonight + tomorrow), counted from the same slates as the marks (regular season and playoffs). **Players:** shown whenever `tonight` is set (`nights > 0`), after Team, so the header, `colspan`, the reload rows and the row partial depend only on the query; its header is plain text with `title="team games"` (not a sort link: Gm is not a `SortKey`, and no link may carry `sort=gm`). **Rosters:** always shown (one of its chips is always lit), after Slot in the skater table and after Team in the goalie table, with the same `title`. Goalies: the count is the team's games, not starts (the `title` says so). **If any needed day is unavailable, every Gm cell shows a dash** (never a partial count), matching the unlit chip and the hidden Rosters line. `goalie_table` adds the column only when given game counts, so Matchup's goalie tables are unchanged. Not sortable (the filter already drops zero-game players; sorting by games is a later add if the owner wants it). |
| Notes | Tonight's notes are unchanged with the option off. With it on, each day gets its own note when it is stale ("Schedule for Sat from 7 h ago."), unavailable ("Tomorrow's (Sat) schedule is unavailable.") or empty ("No games Sat."), and the "(times ET)" line names only the days that have games: "Tonight's and Sat's games (times ET)", "Sat's games (times ET)" or, as today, "Tonight's games (times ET)". The Show more reload row reads "The schedule changed." when the option is on (it may be tomorrow that changed). The weekday comes from tomorrow's `date`, through a fixed name tuple (`("Mon", …)`), not `strftime` (locale) and not the start time. |
| Rosters | Rosters has marks and a count but no chip today. It gets the same two chips above the Skaters table: **Tonight** (the default, lit when `tonight` is absent or `1`) and **Tonight + tomorrow** (`tonight=2`). They don't filter Rosters (a roster is short; the marks are what matter); with `tonight=2` the skater and goalie tables show tomorrow's marks too, and the count line becomes "Tonight + tomorrow: 16 games for 12 of 25 players" (players outside IR slots, as today; shown only when both days are available, else the unavailable note). The team picker form and the season and view toggles carry `tonight` (the picker's hidden-field list gains it; the toggles already pass `params`). Rosters stays lenient about values, as it is for its other parameters: anything but `2` means Tonight. |
| Show more | The Show more link keeps tonight's date (`night`), so a new Eastern night still asks for a reload. The `slate` fingerprint covers both days when the option is on: the hash of tonight's labels (or `off`) and tomorrow's labels (or `off`). When the option is off it is computed exactly as today, so existing links and tests are unchanged. A change in either day returns the "schedule changed" reload row. |
| Cost | Tomorrow's slate comes from a second `tonight(..., night=day + 1)` call. It reuses `schedule/<date>`, the 6-hour freshness, the 5-minute `schedule_failures` backoff and the per-date lock unchanged. It is called **only** when `tonight=2`, and the two calls run concurrently (`asyncio.gather`; the locks are per date, so they don't block each other), so the worst case stays one 3 s budget. A warm cache costs nothing extra. |
| Demo | `DemoScheduleSource` returns the same three games every day, so the new option would look the same as Tonight. It varies the slate by date (two fixed sets alternating by the date's ordinal), still with no NHL call. Both sets start at `time(23)` UTC or later (the existing demo test checks the start falls on the Eastern date), each covers enough teams that demo free agents still match on both days, and the sets share one team, so the demo shows a back-to-back (Gm 2). |
| Scope | Two named days, shown on demand, and their game count. No day picker, no third day, no "games this week" or matchup game counts (those come with the weekly suggester, its own plan), no start/sit advice. |

## 1. Strategy critique

**A. The same parameter with a second value, a second cached call (chosen).** No domain change, no new storage,
no new route; every existing link carries the option already.

**B. A separate `tomorrow=1` parameter.** Rejected: two parameters that must stay mutually exclusive, threaded
through every link and form a second time.

**C. Read tomorrow from the same NHL response (its `gameWeek` holds the week).** Rejected for now: it changes the
source seam, the cache document and the parser to save one cached call that happens only with the option on. The weekly suggester (the next plan) needs the rest
of the matchup week and will decide then whether to read the week from one response.

**D. Always show tomorrow's marks.** Rejected: it doubles the marks on every page for a question the owner asks
only sometimes; he asked for an option.

## 2. What changes

- `services/players_table.py`: `PlayersQuery.nights` parsed from `tonight` (absent, `1`, `2`; else
  `QueryError`); `_filtered` tests `query.nights and playing is not None`.
- `services/schedule.py`: a small helper for the two-day load (`tomorrow`'s date, the concurrent calls). No change
  to the cache, lock or backoff.
- `web/format.py`: tomorrow's mark text (weekday prefix from a fixed tuple) beside `game_label`.
- The Gm column: Players (`players.py` header list and `colspan`, `players.html`, `_player_rows.html`, the plain
  header), Rosters (`rosters.html` skater table after Slot and its "No skaters" colspan), `goalie_table` in
  `_macros.html` (only when given counts; Matchup passes none), `_players_reload.html` wording.
- `web/routes/players.py`: load tomorrow when `nights == 2`; the union for `playing`; the chip state (lit only
  when every needed day is available); per-player labels as a list (tonight's, then tomorrow's); the two-day
  fingerprint.
- `web/routes/rosters.py`: read `tonight`; load tomorrow when `2`; the labels and the per-day counts.
- Templates: the second chip (`players.html`); the chip pair, the picker's hidden field and the count line
  (`rosters.html`); `game_mark` renders a list of labels; the tomorrow note in `schedule_note`.
- `sources/nhl/demo.py`: the date-varied slate.
- SPEC §1 (the Tonight line, and Gm as a two-day games count brought forward at the owner's request from the
  "games-this-week counts" Phase 2 candidate), §7.1, §7.2; DECISIONS: a dated entry (the option, the parameter,
  mixed states, Gm and its dash rule, Rosters chips, the cost, Gm brought forward from "Later").

## 3. Phases

One commit per phase, gates green. No `domain/` change, so no mutmut run.

1. **Service, routes and screens.** Tests:
   - First, before changing `players.py`: pin the one-day fingerprint to a literal (the hash of a known one-game
     slate computed on current `main`), so the existing `tonight` absent or `1` fingerprint can't drift.
   - Parameter: `tonight=2` parses; `tonight=3` is a 400; a bare `?tonight=2` is Free Agents; the two chips are
     exclusive and each toggles off; Apply, Team "Any", sort, toggles and both paging paths carry `tonight=2`; a new
     search and Clear drop it.
   - Filter: a player whose team plays only tomorrow is kept with `tonight=2` and dropped with `tonight=1`; only
     tonight, kept with both; neither, dropped; the count and 0-GP note follow; off day tonight with games tomorrow.
   - Mixed states: tonight fresh and tomorrow unavailable, and the reverse: the normal list, the chip unlit, the
     right "unavailable" note; tomorrow stale shows its age note and filters.
   - Dates: tomorrow is the Eastern date plus one across Eastern midnight and across a DST change (the day before
     the change and the change day); the source is asked for that date.
   - Cost: tomorrow's source call happens only with `tonight=2`, on Players and Rosters; never on
     `/rosters/replace`, League, Matchup, Admin; the two calls run concurrently (a fake whose `games_on` waits on an
     `asyncio.Event` set once both dates have been requested: a sequential implementation fails by timeout, with no
     wall-clock assertion).
   - Marks: tomorrow's text with the weekday prefix; a back-to-back shows both marks in order; no tomorrow mark
     without the option; goalies too.
   - Show more: with `tonight=2`, a change only in tomorrow's labels returns the reload row; a change in
     tomorrow's availability does too; the fingerprint for `tonight` absent or `1` is unchanged from today (a
     fixed expected value from the existing test).
   - Game count: the **Gm** column with 0, 1 and 2 (a back-to-back); absent on Players without `tonight`; shown
     with `tonight=1` or `2` even when the chip is unlit; every cell a dash when any needed day is unavailable, on
     Players and Rosters; goalies too, with the "team games" title; the Players Gm header is not a link and no
     link carries `sort=gm`; Matchup's goalie tables have no Gm column; colspans of the "No players match" row,
     the reload rows and Show more follow the extra column.
   - Notes: an off night tonight with games tomorrow shows "No games tonight." and "Sat's games (times ET)", not
     "Tonight's and Sat's"; the reload row says "The schedule changed." with the option on; the weekday comes from
     the date (a test across a week boundary).
   - Rosters: the chip pair (Tonight lit by default and with `1`); `tonight=2` adds tomorrow's marks and the
     "16 games for 12 of 25 players" line (IR-slot rule as tonight's); the line hidden (the unavailable note instead) when either day is unavailable; the picker
     carries `tonight`.
   - Demo: Players and Rosters with `tonight=2` on two consecutive dates, with no NHL call; the two days' slates
     differ, and they share one team (a Gm 2 in the demo); the existing
     `test_demo_games_follow_the_date_without_using_http` still passes.
2. **Docs.** SPEC, DECISIONS, this plan's status.

## 4. Risks

| Risk | Mitigation |
|---|---|
| A cold cache doubles the schedule calls | Only with the option on; concurrent; same 3 s budget, cache and backoff. |
| An outage of one day read as "these players don't play" | The filter needs both days; the note names the missing day. |
| Show more mixes rows from different slates | The fingerprint covers tomorrow when the option is on. |
| Marks crowd the Team column on a phone | Tomorrow's marks only on demand; one short line each. |

## 5. Open questions

None (team-lead's decisions above).

## 6. Review

### TT-P1 (quality-mgr, at `9d6bd8a`): ready with changes, 11 findings (1 Medium, 7 Low, 3 Nit)

The report is posted verbatim on PR #31. Each finding was checked against the code; all accepted.

| # | Sev | Finding | Triage |
|---|---|---|---|
| 1 | Medium | Gm shows a partial count when one day is unavailable (reaches Rosters) | **Accepted.** Any needed day unavailable: every Gm cell a dash, on Players and Rosters; the test replaced |
| 2 | Low | §2 and the docs list leave out Gm; Gm overlaps SPEC "Later" games-this-week | **Accepted.** Gm files in §2; SPEC §1 and DECISIONS record Gm as brought forward at the owner's request |
| 3 | Low | `goalie_table` is shared with Matchup | **Accepted.** Checked: `matchup.html:47,49` pass no labels. The column only when given counts; a Matchup test |
| 4 | Low | Off-night notes contradict; reload row says "Tonight's" | **Accepted.** The ET line names only days with games; "The schedule changed." with the option on |
| 5 | Low | No pinned fingerprint test exists | **Accepted.** Checked: `test_tonight.py:126` only matches the format. Pin a literal first |
| 6 | Low | A wall-clock concurrency test is flaky | **Accepted.** An `asyncio.Event` barrier fake |
| 7 | Low | Goalie Gm reads as starts | **Accepted.** `title="team games"` on the Gm headers |
| 8 | Nit | Weekday source and locale; the label is 17 characters | **Accepted.** From tomorrow's date via a fixed tuple; width corrected |
| 9 | Nit | Rosters test disagrees with the decision | **Accepted.** The test follows the one-line rule |
| 10 | Nit | Demo alternation must keep the existing test true | **Accepted.** Both sets at 23:00 UTC or later, wide enough, sharing one team |
| 11 | Low | Gm placement undefined on Rosters; a sort link would 400; colspan computed before the slate | **Accepted.** Checked: `rosters.html` has no Team column; `header` macro links every column (`players.html:51-55`); `SortKey` has no `gm`. Rosters after Slot; Players header plain text; Players shows Gm whenever `nights > 0` |

The plan is approved after this triage (team-lead, 2026-11-27).
