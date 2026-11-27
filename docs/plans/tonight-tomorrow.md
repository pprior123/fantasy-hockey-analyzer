# Plan: tonight and tomorrow

Status: **draft**, 2026-11-27, for quality-mgr's plan review. The owner needs no review of plans (2026-11-27).

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
| Marks | Tonight's mark is unchanged (`@BOS 7:00p`). With the two-day option on, tomorrow's mark follows on its own line, prefixed with the weekday: `Sat vs NYR 1:00p`. A team playing both nights (a back-to-back) shows both marks. Tomorrow's marks appear only when the two-day option is on, so the default pages look as they do today. Each mark is a block line (`.game-mark { display: block }`); the longest, about 15 characters at .75rem, fits under the Team code at phone width. |
| Game count | A narrow numeric column **Gm** after Team: the player's team's games in the chosen option (0 or 1 for Tonight; 0, 1 or 2 for Tonight + tomorrow), counted from the same slates as the marks (regular season and playoffs). Shown on Players when a schedule chip is lit, and on Rosters always (one of its chips is always lit), in the skater and goalie tables. A day that is unavailable contributes nothing and its note says so; when every needed day is unavailable the column shows a dash. Not sortable (the filter already drops zero-game players; sorting by games is a later add if the owner wants it). |
| Notes | Tonight's note is unchanged. With the two-day option on, a second note for tomorrow: "Tomorrow's (Sat) schedule is unavailable.", "Schedule for Sat from 7 h ago.", or "No games Sat." The "(times ET)" line reads "Tonight's and Sat's games (times ET)" when either day has games. |
| Rosters | Rosters has marks and a count but no chip today. It gets the same two chips above the Skaters table: **Tonight** (the default, lit when `tonight` is absent or `1`) and **Tonight + tomorrow** (`tonight=2`). They don't filter Rosters (a roster is short; the marks are what matter); with `tonight=2` the skater and goalie tables show tomorrow's marks too, and the count line becomes "Tonight + tomorrow: 16 games for 12 of 25 players" (players outside IR slots, as today; shown only when both days are available, else the unavailable note). The team picker form and the season and view toggles carry `tonight` (the picker's hidden-field list gains it; the toggles already pass `params`). Rosters stays lenient about values, as it is for its other parameters: anything but `2` means Tonight. |
| Show more | The Show more link keeps tonight's date (`night`), so a new Eastern night still asks for a reload. The `slate` fingerprint covers both days when the option is on: the hash of tonight's labels (or `off`) and tomorrow's labels (or `off`). When the option is off it is computed exactly as today, so existing links and tests are unchanged. A change in either day returns the "schedule changed" reload row. |
| Cost | Tomorrow's slate comes from a second `tonight(..., night=day + 1)` call. It reuses `schedule/<date>`, the 6-hour freshness, the 5-minute `schedule_failures` backoff and the per-date lock unchanged. It is called **only** when `tonight=2`, and the two calls run concurrently (`asyncio.gather`; the locks are per date, so they don't block each other), so the worst case stays one 3 s budget. A warm cache costs nothing extra. |
| Demo | `DemoScheduleSource` returns the same three games every day, so the new option would look the same as Tonight. It varies the slate by date (two fixed sets alternating by the date's ordinal), still with no NHL call. |
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
- `web/format.py`: tomorrow's mark text (weekday prefix) beside `game_label`.
- `web/routes/players.py`: load tomorrow when `nights == 2`; the union for `playing`; the chip state (lit only
  when every needed day is available); per-player labels as a list (tonight's, then tomorrow's); the two-day
  fingerprint.
- `web/routes/rosters.py`: read `tonight`; load tomorrow when `2`; the labels and the per-day counts.
- Templates: the second chip (`players.html`); the chip pair, the picker's hidden field and the count line
  (`rosters.html`); `game_mark` renders a list of labels; the tomorrow note in `schedule_note`.
- `sources/nhl/demo.py`: the date-varied slate.
- SPEC §1 (the Tonight line), §7.1, §7.2; DECISIONS: a dated entry (the option, the parameter, mixed states,
  Rosters chips, the cost, why it isn't "games this week").

## 3. Phases

One commit per phase, gates green. No `domain/` change, so no mutmut run.

1. **Service, routes and screens.** Tests:
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
     `/rosters/replace`, League, Matchup, Admin; the two calls run concurrently (one slow fake for each day finishes
     within one budget).
   - Marks: tomorrow's text with the weekday prefix; a back-to-back shows both marks in order; no tomorrow mark
     without the option; goalies too.
   - Show more: with `tonight=2`, a change only in tomorrow's labels returns the reload row; a change in
     tomorrow's availability does too; the fingerprint for `tonight` absent or `1` is unchanged from today (a
     fixed expected value from the existing test).
   - Game count: the **Gm** column with 0, 1 and 2 (a back-to-back); absent on Players with no chip lit; a dash
     when every needed day is unavailable; one unavailable day counts only the other; goalies too; colspans of
     the "No players match" row, the reload row and Show more follow the extra column.
   - Rosters: the chip pair (Tonight lit by default and with `1`); `tonight=2` adds tomorrow's marks and the
     "16 games for 12 of 25 players" line (IR-slot rule as tonight's); a day's count hidden when that day is unavailable; the picker
     carries `tonight`.
   - Demo: Players and Rosters with `tonight=2` on two consecutive dates, with no NHL call; the two days' slates
     differ.
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

(quality-mgr's plan review goes here, with team-lead's triage.)
