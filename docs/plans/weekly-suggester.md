# Plan: weekly add suggester

Status: **draft**, 2026-11-27, for quality-mgr's plan review. The owner needs no review of plans (2026-11-27).

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

**Scope change.** `CLAUDE.md` ("no waiver *recommendation* ... screens") and SPEC §1 kept recommendations out of
Phase 1. The owner asked for this one, so it is in scope now: SPEC §1 "Added after Phase 1", `CLAUDE.md`'s
scope line and DECISIONS record it. **No projections** beyond "TTLTST × scheduled games the player would start":
no per-opponent adjustment, no injury-return guess, no minutes or line data.

## 0. Decisions

| Question | Decision |
|---|---|
| The value of a lineup on a day | The sum of TTLTST over the skaters who start: each skater whose NHL team has a game that day can fill one starting slot he is eligible for; the starting skater slots and their counts come from the league's settings (`roster_slots` with `is_starting`, positions C, LW, RW, D: today C3 LW3 RW3 D6). Goalies are left out (TTLTST doesn't rate them; their slot is untouched by a skater swap). The best lineup maximizes that sum: the owner sets his lineup daily (`roster_type: date`), so this assumes he starts the best eligible players. An unrated skater (too few GP) is worth 0 per start; ties go to more starts, then a fixed order by player id. |
| Who can start | Skaters on the owner's roster outside IR slots, with a game that day. A status of `IR`, `IR-LT`, `IR-NR`, `O`, `NA` or `SUSP` means he doesn't play (no projection of a return); `DTD` plays. The candidate add follows the same rules. |
| The value of a swap | `gain(add, drop) = Σ over horizon days [best(roster − drop + add) − best(roster)]`. It counts the add's starts at his TTLTST, minus whoever he pushes to the bench, minus the drop's lost starts, all through the optimizer, so the owner's example holds: a slightly lower TTLTST who fills empty slots beats a higher one who would bench a starter. Shown as **Week +** (TTLTST-starts, 2 decimals). Only positive gains are suggested. |
| "Drop nobody" | When the owner's roster has an open spot (fewer players outside IR slots than the non-IR slots, BN included, from the settings), the add needs no drop and the suggestion says "open spot". |
| Drop candidates | Skaters on the owner's roster outside IR slots whose TTLTST is at or below the **median TTLTST of his rated skaters outside IR slots**, plus his unrated skaters. Never a goalie, never an IR-slot player (dropping one frees no active spot). **Why the median:** it scales with the roster, so the suggester never proposes dropping a star because his schedule is light this week, and leaves about half the skaters as options. The threshold is one named constant (`DROP_SHARE = 0.5`), recorded in DECISIONS. The Replace view still lets the owner pick any drop. |
| Add candidates | Available skaters (`owner_key is None`) eligible at C, LW, RW or D, not out (status rule above), with at least one game in the horizon. Yahoo's available pool includes players on waivers, who can't be added until they clear (2 days in this league); the app can't tell them apart (the recorded responses carry no ownership type), so the page says so in one line. |
| The cap | A suggestion pairs an add with a drop; the existing `swap_ok` decides (`services/teams.py`). A swap that doesn't fit (`False`) is never suggested; an unknown fit is shown with a mark ("cap unknown"). "Open spot" uses the room as it is. |
| Horizon | **This week** (default): from tomorrow, the US/Eastern date after today, to the current week's `week_end` (Yahoo's scoreboard). Today is left out: today's lineup is already set and whether a free agent added today can still play today depends on a Yahoo setting the app doesn't read. **Next week** (`week=next`): the next scoreboard's `week_start` to `week_end` (in the snapshot as `next_scoreboard`), with the roster as it is now. If this week has no days left, the page says so and links to next week. The toggle is hidden when there is no next week. |
| The add limit | `max_weekly_adds` (settings, "5" in the recording) and the owner's adds used this week (`roster_adds` in the scoreboard's team metadata, used only when `coverage_type` is `week` and `coverage_value` is the current week). The page says "Adds left: 3 of 5". With none left this week, it says so and shows no suggestions for this week (next week still works: the limit resets, so it shows "5 adds next week"). Either value missing: "Adds left: unknown", and suggestions still show. v1 ranks **single** swaps: after the owner makes one, the next Yahoo fetch recomputes. |
| Schedule | One `tonight(repo, source, clock, night=d)` call per horizon day (up to 7), run concurrently: the existing per-date cache (`schedule/<date>`, 6 h), per-date lock and per-date failure backoff, unchanged. A warm cache costs nothing; a cold one is up to 7 small NHL calls under one 3 s budget. If **any** horizon day is unavailable, the page shows no suggestions and says which days are missing (never a value from part of the week); stale days are used, with the age note. Reading the whole week from one NHL response would save calls but change the source seam, the parser and the cache; not worth it at this size. |
| Where it shows | **A new page, `/rosters/suggest`** ("Suggest adds", linked from Rosters on the owner's team): the horizon line, the adds-left line, a **Gaps** table (per horizon day, the starting slots left empty by the current best lineup, e.g. "C 1 · D 2", or "none"), and the **top 10 adds**, each with its best drop: Add (name, positions, team, TTLTST), Drop (name, TTLTST, or "open spot"), Week +, Starts (the add's starts of his games in the horizon, e.g. "3 of 4"), Fills (the days and slots he starts, e.g. "Sat C, Mon D"), and a cap mark when the fit is unknown. Each row links to the Replace view for that drop with the same horizon. **The Replace view** gets a horizon chip pair (none, the default as today; "This week"; "Next week"); with one chosen, two columns, Week + and Starts, for that drop, and the rows sort by Week + (then TTLTST). |
| Ranking | Week + descending, then the add's TTLTST descending, then name and player id. One row per add (his best drop); an add whose every pairing has no positive gain is left out. |

## 1. Strategy critique

**A. An exact daily lineup optimizer, gain by re-solving with and without the swap (chosen).** It answers the
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
  `week_gain(days, slots, add, drop) -> SwapGain` (`gain`, the add's starts and filled `(day index, slot)` pairs),
  where `days` is the per-day list of eligible players already filtered by games and status. No dates, no
  schedule types: the service builds the days.
- **`sources/yahoo`**: parse `max_weekly_adds` into `LeagueSettings` (optional) and each team's `roster_adds` into
  the `Scoreboard` (`adds: Mapping[team_key, (coverage_week, value)]`, optional). Both are in the committed
  fixtures (`001-...-settings.json`, `015-...-scoreboard-week-6.json`). `snapshot_codec` reads stored snapshots
  without them (defaults), so an old cache document still loads.
- **`services/suggest.py`**: builds the horizon from the clock and the scoreboards, loads the slates, builds each
  day's eligible players, the drop and add candidates, the cap check, the add limit, and ranks. **Pruning** keeps
  it fast and exact: for a candidate, `gain ≤ TTLTST × games in the horizon − min over drops of the drop's own
  loss`; candidates are taken in descending bound order and the scan stops when the bound falls below the 10th best
  gain found. The loss of each drop and the per-day base lineups are computed once. A swap only re-solves the days
  the add plays.
- **Web**: `routes/rosters.py` `/rosters/suggest` (owner's team only; any other team: the existing "works for your
  own roster" 400), its template, the Rosters link, the Replace chips and columns.
- **Demo**: the demo league's free agents and roster produce suggestions with the demo schedule (`DemoScheduleSource`
  varies by date after PR #31), with no network.
- **Docs**: SPEC §1 "Added after Phase 1" (the suggester, its value rule, no projections), §7.2 (Rosters' link,
  Replace's chips), a new §7 entry for the page; `CLAUDE.md`'s scope line; DECISIONS (the scope change, the value
  rule, the drop threshold, the horizon and today, the add limit, waivers, the cap rule, the schedule calls).

## 3. Phases

One commit per phase, gates green.

1. **Lineup optimizer** (`domain/lineup.py`), test-first: an empty roster; more players than slots at one position;
   a multi-position player who must take the other slot (the greedy trap of §1 C); an unrated player benched for a
   rated one but starting into an empty slot; ties (more starts first, then id); slot counts from a mapping (no
   constants); a hypothesis property test against brute force on small random instances (value equal, assignment
   valid); `week_gain` for the owner's example (a lower-TTLTST add who fills empty slots beats a higher one who
   benches a starter), a drop who loses starts, "drop nobody", an add with no games (gain 0). mutmut on
   `fha.domain.lineup*`, every non-`killed` status hand-checked (mutmut pitfalls).
2. **Yahoo fields**: the two parses against the fixtures; missing, malformed and other-week `roster_adds`;
   codec round-trip and an old document without the fields.
3. **Service**: the horizon (tomorrow to `week_end` across Eastern midnight; the last day of the week leaves none;
   next week's dates); status and IR-slot rules; drop candidates at the median (an even count, all unrated, a
   single skater); "open spot" from the slot counts; the cap rule (`False` dropped, unknown marked); the add limit
   (none left, unknown, next week resets); any unavailable day blocks suggestions; stale days used; pruning gives
   the same top 10 as exhaustive search on a synthetic league (a test with pruning off versus on); the Gaps table.
4. **Screens**: `/rosters/suggest` for this week and next, the notes (no days left, no adds left, unavailable days,
   waivers), links to Replace with the horizon; another team is a 400; Replace's chips and columns, its sort with
   a horizon, its default unchanged without one; the Rosters link only on the owner's team; the demo page with no
   NHL call; routes that don't load the horizon (League, Matchup, Admin, Players) make no extra schedule calls.
   A timing check on the demo league and on a synthetic league of real size (about 600 available skaters): the
   developer reports the server time in the close (not a test assertion).
5. **Docs**: SPEC, `CLAUDE.md`, DECISIONS, this plan's status.

## 4. Risks

| Risk | Mitigation |
|---|---|
| The optimizer is wrong in a corner | Exact method, brute-force property test, mutmut. |
| Too slow on a serverless request | Small daily problems; pruning with a proven bound; only the add's game days re-solved; a measured time in the close. |
| A suggestion for a player on waivers | The page says available players may be on waivers; Yahoo shows it on the add. |
| A misleading value from a partial week | Any unavailable day: no suggestions, the days named. |
| Suggesting dropping a good player | Drops only from the bottom half by TTLTST; Replace for any other choice. |
| Adds left wrong | Used only when Yahoo's coverage week matches; else "unknown", never a guess. |
| Read as a projection | The page says what Week + is: TTLTST × the starts the add would get, minus what the swap costs. |

## 5. Open questions

None (team-lead's decisions above; the owner chose "team decides").

## 6. Review

(quality-mgr's plan review goes here, with team-lead's triage.)
