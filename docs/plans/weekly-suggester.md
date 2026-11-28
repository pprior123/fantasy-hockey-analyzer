# Plan: weekly add suggester

Status: **implemented**, 2026-11-27 (WS-1, WS-2). Approved that day after quality-mgr's plan review (WS-P1:
ready with changes, 12 findings, all accepted; triage in §6). The owner needs no review of plans (2026-11-27).

## The request

The owner (2026-11-27, relayed by the operator, `relay-2026-11-27-schedule-and-suggester.md`): "a player suggester
based on my current week with gaps for certain positions ... optimize using my statistic to balance games played
with TTLTST ... a player that has 10% lower ttltst but fits all my team's player game gaps for the week is better
than a player that would result in someone sitting on the bench."

The operator's framing, agreed by the owner: the value of an add for the rest of the matchup week is roughly the
lineup starts he would actually get times his TTLTST, minus what the best drop loses. Work out the best daily
lineup for each remaining day from position eligibility and the league's starting slots, with and without the
swap. Respect the weekly add limit. Show which empty slots and days a candidate fills. Build on the Replace view
where that fits.

Design defaults (owner chose "team decides", `relay-2026-11-27-suggester-defaults.md`): drops limited to players
below a TTLTST threshold; the rest of the current matchup week with a toggle for next week; plain TTLTST × starts
(weighting by the categories Matchup says the owner is losing is a later option, not v1).

**Depends on PR #31** (Tonight + tomorrow): its date-varied demo schedule is used here, and both PRs edit
`routes/rosters.py` and `rosters.html`. Phases 1 and 2 (the domain optimizer, the Yahoo fields) touch no file
PR #31 touches and may start before it merges; phases 3 to 5 start after PR #31 merges, on this branch rebased onto
`main`.

**Scope change.** `CLAUDE.md` ("no waiver *recommendation* ... screens") and SPEC §1 kept recommendations out of
Phase 1. The owner asked for this one, so it is in scope now: SPEC §1 "Added after Phase 1", `CLAUDE.md`'s
scope line and DECISIONS record it. **No projections** beyond "TTLTST × scheduled games the player would start":
no per-opponent adjustment, no injury-return guess, no minutes or line data.

## 0. Decisions

| Question | Decision |
|---|---|
| The value of a lineup on a day | The sum of TTLTST over the skaters who start: each skater whose NHL team has a game that day can fill one starting slot he is eligible for; the starting skater slots and their counts come from the league's settings (`roster_slots` with `is_starting`, positions C, LW, RW, D: today C3 LW3 RW3 D6). Goalies are left out (TTLTST doesn't rate them; their slot is untouched by a skater swap). The best lineup maximizes that sum: the owner sets his lineup daily (`roster_type: date`), so this assumes he starts the best eligible players. An unrated skater (too few GP) is worth 0 per start; ties go to more starts, then a fixed order by player id. **Values are integers inside the domain** (TTLTST × 10⁶, rounded), so sums, differences and ties are exact; the result is permutation-invariant. |
| Who can start | Skaters on the owner's roster outside IR slots, with a game that day. No status or `DTD`: he plays. **Any other status** (`IR`, `IR-LT`, `IR-NR`, `O`, `NA`, `SUSP`, or one the app doesn't know) means he doesn't play: the safe reading, no projection of a return. The candidate add follows the same rules. |
| The value of a swap | `gain(add, drop) = Σ over horizon days [best(roster − drop + add) − best(roster)]`. It counts the add's starts at his TTLTST, minus whoever he pushes to the bench, minus the drop's lost starts, all through the optimizer, so the owner's example holds: a slightly lower TTLTST who fills empty slots beats a higher one who would bench a starter. Shown as **Week +** (TTLTST-starts, 2 decimals). Only gains of at least 0.005 are suggested (what rounds to 0.00 isn't shown as a gain). |
| "Drop nobody" | When the owner's roster has an open spot (fewer players outside IR slots than the non-IR slots, BN included, from the settings), the add needs no drop and the suggestion says "open spot". |
| Drop candidates | Skaters on the owner's roster outside IR slots whose TTLTST is at or below the **median TTLTST of his rated skaters outside IR slots**. **Unrated skaters are never automatic drops**: unrated usually means "hasn't played yet" (a star back from IR+, a recalled prospect: the engine rates only skaters above 2% of the max GP), not "bad", and his zero loss would make him the proposed drop for every add. Replace still offers any drop. Never a goalie, never an IR-slot player (dropping one frees no active spot). **Why the median:** it scales with the roster, so the suggester never proposes dropping a star because his schedule is light this week, and leaves about half the skaters as options. The threshold is one named constant (`DROP_SHARE = 0.5`), recorded in DECISIONS. The Replace view still lets the owner pick any drop. |
| Add candidates | Available skaters (`owner_key is None`) eligible at C, LW, RW or D, not out (status rule above), with at least one game in the horizon. Yahoo's available pool includes players on waivers, who can't be added until they clear (2 days in this league); the recorded `status=A` responses carry no ownership type, so the page says so in one line. A later change could fetch Yahoo's `status=FA` or `status=W` lists (from Yahoo's API documentation, unverified here) and count only the days a waiver add can play; not v1. |
| The cap | A suggestion pairs an add with a drop; the existing `swap_ok` decides (`services/teams.py`), with the drop's cap hit from **one helper shared with Replace** (today built inline in `replace_view`, `teams.py:289-294`), so Suggest and its Replace link always agree. A swap that doesn't fit (`False`) is never suggested; an unknown fit is shown with a mark ("cap unknown"). "Open spot" uses the room as it is. |
| Horizon | **This week** (default): from tomorrow, the US/Eastern date after today, to the current week's `week_end` (Yahoo's scoreboard). Today is left out: today's lineup is already set and whether a free agent added today can still play today depends on a Yahoo setting the app doesn't read. **Next week** (`week=next`): from the later of the next scoreboard's `week_start` and tomorrow, to its `week_end` (in the snapshot as `next_scoreboard`), with the roster as it is now. If this week has no days left, the page says so and links to next week. The toggle is hidden when there is no next week. A missing `week_start` or `week_end` (`Scoreboard` allows None): that horizon is unavailable, with a one-line note. If Yahoo still reports the old week after its end (next week's `week_start` ≤ today), adds left for next week are "unknown", not 5. |
| The add limit | `max_weekly_adds` (settings, "5" in the recording) and the owner's adds used this week (`roster_adds` in the team metadata of the `teams/roster` response, parsed into `Team`; used only when `coverage_type` is `week` and `coverage_value`, an integer in the fixture, equals `settings.current_week` as an integer). Checked: every team in the recorded `009-...-teams-roster.json` carries it with `coverage_value` 5, the recording's current week; the scoreboards carry the same value whatever their week, so the scoreboard is not the source. The page says "Adds left: 3 of 5". With none left this week, it says so and shows no suggestions for this week (next week still works: the limit resets, so it shows "5 adds next week"). Either value missing: "Adds left: unknown", and suggestions still show. v1 ranks **single** swaps: after the owner makes one, the next Yahoo fetch recomputes. |
| Schedule | One `tonight(repo, source, clock, night=d)` call per horizon day (up to 7), run concurrently: the existing per-date cache (`schedule/<date>`, 6 h), per-date lock and per-date failure backoff, unchanged. A warm cache costs nothing; a cold one is up to 7 small NHL calls under one 3 s budget. If **any** horizon day is unavailable, the page shows no suggestions and says which days are missing (never a value from part of the week); stale days are used, with the age note. The unavailable note names the missing days and says the app tries again in a few minutes (the per-date 5-minute backoff). Reading the whole week from one NHL response would save calls and make the week succeed or fail together, but changes the source seam, the parser and the cache; v1 keeps per-date calls, and DECISIONS records the cost (up to 7 cold calls per horizon) as accepted. |
| Where it shows | **A new page, `/rosters/suggest`** ("Suggest adds", linked from Rosters on the owner's team): the horizon line, the adds-left line, a **Gaps** table (per horizon day, the starting slots left empty by the current best lineup, e.g. "C 1 · D 2", or "none"), and the **top 10 adds**, each with its best drop: Add (name, positions, team, TTLTST), Drop (name, TTLTST, or "open spot"), Week +, Starts (the add's starts of his games in the horizon, e.g. "3 of 4"), Fills (the days and slots he starts, e.g. "Sat C, Mon D"), and a cap mark when the fit is unknown. Each row with a drop links to the Replace view for that drop with the same horizon; an "open spot" row has no Replace link. The Drop cell shows the drop's status badge (an `O` player loses nothing this week, so the owner sees why he is proposed). **The Replace view** gets a horizon chip pair (none, the default as today; "This week"; "Next week"); with one chosen, two columns, Week + and Starts, for that drop, and the rows sort by Week + (then TTLTST). With a week chosen and priced whole, Replace lists **every add candidate** (the rules under "Add candidates": available, eligible at C, LW, RW or D, not out, a game in the horizon), at any position, not only the drop's: Week + is position-free, and every Suggest row's Replace link then lists that add (WS-3, team-lead's decision). With no week, or a week that can't be priced, it lists the drop's positions by TTLTST as before. |
| Ranking | Week + descending, then the add's TTLTST descending, then name and player id. One row per add (his best drop); an add whose every pairing has no positive gain is left out. **The best drop** for an add is the one with the highest gain; on a tie (common on a short horizon, where several drops lose nothing): the open spot first, then the lowest TTLTST, then player id. So the weakest player is the one proposed, never an arbitrary near-median one. |
| Season | The values, the median and the drop threshold use the TTLTST of the page's season (the same `season` toggle and default as Replace and Rosters, `default_season`). |
| Replace and goalies | The Replace view of a goalie shows no horizon chips (goalies aren't in the lineup model). Replace with no horizon chip makes no schedule call, as today. |
| Speed | **The threshold method** (below) is the main algorithm: for a fixed day and remaining roster R′ (the roster less the drop, with games that day), an add with positions P and value v gives `best(R′ + add) = max(best(R′), v + max over s in P of best(R′ with one fewer s slot))`. So his day gain is `max(0, v − c_P)` with `c_P = best(R′) − max over s in P of best(R′, slots − s)`, and he starts that day exactly when `v > c_P`, in the slot type `s` that attains the max (ties in the fixed order C, LW, RW, D). One solve per slot type plus the base, per (day, drop): 7 days × 11 drop options × 5 ≈ 385 solves cover every add, on Suggest and on Replace's column. (quality-mgr checked the identity on 3,000 random instances against an exact solver: 0 mismatches.) The direct re-solve stays only as the test oracle. **Pass line:** under 2 s of compute for a synthetic league of 600 available skaters, 11 drop options and 7 full-slate days. |

## 1. Strategy critique

**A. An exact daily lineup optimizer, the gain computed exactly from per-slot thresholds (chosen).** It answers the
owner's question directly (who starts, who sits) and captures his example without a hand-tuned weight. The daily
problem is small: at most about 20 skaters with a game, 4 slot types, 15 slots.

**B. A heuristic score (TTLTST × games, minus a penalty for crowded positions).** Rejected: it needs a penalty
weight nobody can justify, and gets the owner's example wrong exactly when it matters (a crowded day with an empty
slot at another position).

**C. Greedy daily lineup (best players first).** Rejected: not optimal with multi-position players (a C/LW taking
the last C slot can leave an LW slot empty that a pure C couldn't fill).

**D. A combined multi-add plan for the whole week.** Rejected for v1: combinatorial, and the owner makes one move
at a time; single swaps re-ranked after each fetch are enough.

## 2. What changes

- **`domain/lineup.py`** (pure, test-first, mutmut): `LineupPlayer(player_id, positions: frozenset[str], value:
  float)`, `best_lineup(players, slots: Mapping[str, int]) -> Lineup` (`value`, `starts`, and the assignment
  player → slot type), exact (a max-weight assignment of players to slot types with capacities; the developer picks
  min-cost flow or the Hungarian method on expanded slots, both fine at this size), deterministic on ties.
  `day_thresholds(players, slots) -> Thresholds` (the base value and, per slot type, the best value with one fewer
  slot of that type: the `c_P` of §0 "Speed" for any position set P), and
  `week_gain(days, slots, add: LineupPlayer, add_days: frozenset[int], drop: str | None) -> SwapGain` (`gain`, the
  add's starts and the filled `(day index, slot type)` pairs), where `days` is the per-day list of the owner's
  eligible players (already filtered by games and status; the add is not in it) and `add_days` the day indexes the
  add plays. Built on the thresholds; the direct re-solve is the tests' oracle. No dates, no schedule types: the
  service builds the days.
- **`sources/yahoo`**: parse `max_weekly_adds` into `LeagueSettings` (optional) and each team's `roster_adds` into
  `Team` (`roster_adds: (coverage_week, value) | None`) from the `teams/roster` response. Both are in the committed
  fixtures (`001-...-settings.json`, `009-...-teams-roster.json`). `snapshot_codec` reads stored snapshots
  without them (defaults), so an old cache document still loads.
- **`services/suggest.py`**: builds the horizon from the clock and the scoreboards, loads the slates, builds each
  day's eligible players, the drop and add candidates, the cap check (the shared drop cap-hit helper), the add
  limit, and ranks. Per (day, drop option) it computes the thresholds once; every add is then arithmetic (§0
  "Speed"). No pruning is needed.
- **Web**: `routes/rosters.py` `/rosters/suggest` (owner's team only; any other team: the existing "works for your
  own roster" 400), its template, the Rosters link, the Replace chips and columns.
- **Demo**: the demo league's free agents and roster produce suggestions with the demo schedule (`DemoScheduleSource`
  varies by date after PR #31), with no network.
- **Docs**: SPEC §1 "Added after Phase 1" (the suggester, its value rule, no projections), §7.2 (Rosters' link,
  Replace's chips), a new §7 entry for the page, and "Later" (the drop suggestions brought forward, as the sheet
  export was); `CLAUDE.md`'s scope line; DECISIONS (the scope change, the value
  rule, the drop threshold, the horizon and today, the add limit, waivers, the cap rule, the schedule calls).

## 3. Phases

One commit per phase, gates green.

1. **Lineup optimizer** (`domain/lineup.py`), test-first: an empty roster; more players than slots at one position;
   a multi-position player who must take the other slot (the greedy trap of §1 C); an unrated player benched for a
   rated one but starting into an empty slot; ties (more starts first, then id); slot counts from a mapping (no
   constants); a hypothesis property test against brute force on small random instances (value equal, assignment
   valid); the same assignment for any permutation of the input players; `day_thresholds` and `week_gain` against
   the direct re-solve (a property test over random small instances, multi-position players and zero values
   included); `week_gain` for the owner's example (a lower-TTLTST add who fills empty slots beats a higher one who
   benches a starter), a drop who loses starts, "drop nobody", an add with no games (gain 0). mutmut on
   `fha.domain.lineup*`, every non-`killed` status hand-checked (mutmut pitfalls).
2. **Yahoo fields**: the two parses against the fixtures; missing, malformed and other-week `roster_adds`;
   codec round-trip and an old document without the fields.
3. **Service**: the horizon (tomorrow to `week_end` across Eastern midnight; the last day of the week leaves none;
   next week's dates); status and IR-slot rules; drop candidates at the median (an even count, all unrated, a
   single skater; an unrated 0-GP skater is never an automatic drop); "open spot" from the slot counts; any unknown
   status is out; `coverage_value` compared as an integer; next week starting at tomorrow when Yahoo still reports
   the old week, with adds left unknown; a missing `week_end`; gains under 0.005 not listed; Suggest and Replace give
   the same cap mark for the same pair; the cap rule (`False` dropped, unknown marked); the add limit
   (none left, unknown, next week resets); any unavailable day blocks suggestions, and the note names the days and the retry; stale days
   used; the service's top 10 equals an exhaustive direct re-solve on a small synthetic league; the Gaps table.
4. **Screens**: `/rosters/suggest` for this week and next, the notes (no days left, no adds left, unavailable days,
   waivers), links to Replace with the horizon; another team is a 400; Replace's chips and columns, its sort with
   a horizon, its default unchanged without one; the Rosters link only on the owner's team; the demo page with no
   NHL call; routes that don't load the horizon (League, Matchup, Admin, Players, and Replace with no horizon chip,
   so `test_only_players_and_main_rosters_read_the_schedule` stays as it is) make no extra schedule calls; an
   open-spot row has no Replace link; the drop's status badge.
   A timing check on the demo league and on a synthetic league of real size (600 available skaters, 11 drop
   options, 7 full-slate days): under 2 s of compute. The developer reports the times in the close (a script, not a
   test assertion).
   Tie-break: several drops with the same gain propose the open spot, then the lowest TTLTST, then player id.
   A goalie's Replace view has no horizon chips.
5. **Docs**: SPEC, `CLAUDE.md`, DECISIONS, this plan's status.

## 4. Risks

| Risk | Mitigation |
|---|---|
| The optimizer is wrong in a corner | Exact method, brute-force property test, mutmut. |
| Too slow on a serverless request | The threshold method (about 385 small solves per page); a 2 s pass line, measured in the close. |
| A suggestion for a player on waivers | The page says available players may be on waivers; Yahoo shows it on the add. |
| A misleading value from a partial week | Any unavailable day: no suggestions, the days named. |
| Suggesting dropping a good player | Drops only from the rated bottom half by TTLTST; unrated never automatic; Replace for any other choice. |
| Adds left wrong | Used only when Yahoo's coverage week matches; else "unknown", never a guess. |
| Read as a projection | The page says what Week + is: TTLTST × the starts the add would get, minus what the swap costs. |

## 5. Open questions

None (team-lead's decisions above; the owner chose "team decides").

## 6. Review

### WS-P1 (quality-mgr, at `d39057f`): ready with changes, 12 findings (2 Medium, 9 Low, 1 Nit)

The report is posted verbatim on PR #32. Each finding was checked against the code; all accepted.

| # | Sev | Finding | Triage |
|---|---|---|---|
| 1 | Medium | Unrated skaters (often "hasn't played yet") become the proposed drop for every add | **Accepted.** Checked: `gp_floor_fraction = 0.02` (`engine.py:58`). Unrated skaters are never automatic drops; Replace still offers them; a test |
| 2 | Medium | Re-solving per swap can take tens of seconds on a full-slate week (60 s limit) | **Accepted.** Checked: `maxDuration: 60` (`vercel.json`), no solver library. The threshold method is the main algorithm (§0 "Speed"); pruning dropped; pass line under 2 s for 600 skaters, 7 full days |
| 3 | Low | Float sums make "positive gain" and ties fragile | **Accepted.** Integer values in the domain (TTLTST × 10⁶); gains under 0.005 not listed; a permutation test |
| 4 | Low | `week_gain` doesn't say on which days the add plays | **Accepted.** `add_days: frozenset[int]`; the add is not in `days` |
| 5 | Low | "Open spot" rows have nothing to link to on Replace | **Accepted.** Checked: Replace 400s for a non-roster id (`rosters.py`). No Replace link on those rows |
| 6 | Low | Horizon edges at the week change; `week_end` None; integer compare | **Accepted.** Next week from max(`week_start`, tomorrow), adds left unknown when Yahoo still shows the old week; None dates make the horizon unavailable; integer compare |
| 7 | Low | Unknown statuses; show the drop's status | **Accepted.** Any status but none or `DTD` is out; the drop's badge shown |
| 8 | Low | SPEC "Later" still lists drop suggestions | **Accepted.** Checked: `SPEC.md:824-825`. "Later" updated in the docs phase |
| 9 | Low | Yahoo's `status=FA` / `status=W` could separate waivers | **Accepted as a note** for a later change (unverified API); v1 keeps the one-line caveat |
| 10 | Nit | Replace with no horizon must stay free of schedule calls | **Accepted.** Checked: `test_tonight.py:415-428`. Added to the decision and tests |
| 11 | Low | Seven per-date calls, all-or-nothing, 5-minute backoff per day | **Accepted.** v1 keeps per-date calls; the note names the days and the retry; DECISIONS records the cost |
| 12 | Low | Share the drop's cap-hit rule with Replace | **Accepted.** Checked: inline in `replace_view` (`teams.py:289-294`). One helper; a test that both agree |

The plan is approved after this triage (team-lead, 2026-11-27). Phases 3 to 5 start after PR #31 merges.

### Implementation notes, phases 1-2 (developer, approved by team-lead, 2026-11-27)

The plan contradicted itself in three places; developer resolved them, and team-lead approved:

1. **Ties.** §0 "Speed" said he starts "exactly when `v > c_P`", while lineup ties go to more starts. The direct
   re-solve defines the result: thresholds carry `(value, starts)` and compare lexicographically; an exact tie on
   both falls back to solving that day directly, so the id rule decides.
2. **Speed.** `week_gain` called per (add, drop) would re-price the drop each time. `drop_week(days, slots, drop)
   -> DropWeek` precomputes each day's base and thresholds; `DropWeek.gain(add, add_days)` is arithmetic; `week_gain`
   keeps its signature as a thin wrapper. Measured: 0.205 s for 600 adds, 11 drop options, 7 full-slate days (pass
   line 2 s).
3. **Units.** `LineupPlayer.value` is a float, converted once (`units`, `SCALE = 10**6`); `Lineup.value`,
   `Thresholds` and `SwapGain.gain` are integer units, so the 0.005 cut is an exact integer compare.
4. **Solver.** Not flow or Hungarian: a greedy over the transversal matroid (players in value-then-id order, each
   seated if an augmenting path over slot types exists, moving earlier starters along it). Exact because a player's
   value doesn't depend on his slot; with values ≥ 0 it also gives the most starts. Negative or non-finite values
   raise `ValueError`. The slot tie order is the `slots` mapping's order (the service passes the settings' order).

### Implementation notes, phases 3-5 (developer, approved by team-lead, 2026-11-27)

1. **Demo weeks.** The demo league's weeks were fixed in October, so against a real clock both horizons were
   empty. `demo_services` now dates the two demo scoreboards from its clock (`demo_this_week`: the Monday-Sunday
   week holding today's Eastern date, and the week after; week numbers kept). It also sets the add limit (5) and
   the owner's adds (current week, 2). Plain `demo_snapshot()` is unchanged.
2. **Replace and IR-slot drops.** An IR-slot drop frees no active spot, so, like a goalie, he gets no week chips.
3. **An unpriceable week on Replace** (a day unavailable, no days left, no week dates): a note, dashes in Week +
   and Starts, and the default TTLTST sort. Never a partial week.
4. **No adds left** shows the Gaps table but no adds.
5. **Structure.** `services/suggest.py` is split into `horizon` (pure), `load_week` (I/O), `rank` and
   `replace_gains` (pure), with `plan_week` putting them together. `schedule.slates` loads any list of dates.
   Display text lives in `web/format.py`.
6. **Replace with a week lists every add candidate** (WS-3), at any position. A Suggest row's add and drop can play
   different positions (drop a LW, add a D who fills D gaps), and the drop-position list left the add off the
   linked page. With no week, or an unpriceable one, Replace lists the drop's positions as before.
7. **Speed**, through `rank`: 0.06 s for 600 available skaters, 10 drop options and 7 full-slate days.

