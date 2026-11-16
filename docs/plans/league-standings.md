# Plan: League screen ranks, percentiles, roto points, gaps

Status: implemented, 2026-11-16 (PR #26). Owner chose the features (2026-11-16): rank AND
percentile for each of the 7 categories and overall, sort by any column, heat
shading, roto points, and the gap to the team above. On 2026-11-16 the owner
confirmed all four §5 recommendations: heat shading by team rank (not Pctl,
reversing the original pick), the owner's row not pinned, Roto as the default
sort, and category gaps in per-82 units. OK'd to implement.

## 1. The decision

League (SPEC §7.3) today shows one row per team: mean TTLTST, then either
payroll and room (Salary view) or the 7 category mean norms (Categories view),
with the owner's team pinned first and the rest by TTLTST. It becomes a
standings table built from the same team profiles (SPEC §5).

### Definitions (all from `team_profile`: rated skaters outside IR / IR+ slots)

The categories are the engine's (`view.config.categories`, carried by every
`TeamProfile.categories`): the 7 by default, but SPEC §5 keeps the list in
config, so nothing below assumes 7. For each team T and each column X in
those categories plus TTLTST:
- `value(T, X)` = `mean_norm(T, X)` for a category, `team_ttltst(T)` for TTLTST
  (unchanged; None for a team with no profile players).
- **Rank** among the teams with a value: competition ranking, higher is
  better, ties share the best rank (1, 2, 2, 4), as Players does.
- **Pctl** on the Players scale: where `value(T, X)` would sit among the
  league's players.
  `rank_p = 1 + #(rated players whose X > value)`, `Pctl = (1 - rank_p / pool_size) × 100`,
  where X for a player is his norm in that category (or his TTLTST), over
  rated players, and `pool_size` is the engine's (skaters with GP > 0, rated
  or not). That is exactly the engine's formula for a player
  (`engine.rate`), applied to the team's mean. The owner was told that team
  means sit high on this scale.
- **Roto points** per category: with n teams having a value, rank 1 scores n
  points, rank n scores 1; tied teams share the points of the places they
  span (two teams tied for 2nd each get (n-1 + n-2) / 2). **Roto total** = the
  sum over the configured categories; None if any of them has no value for
  the team.
  It is from rates per 82 games, not Yahoo's season totals, so it's labelled
  "Roto (rates)" and the page notes it is a guide, not the standings.
- **Gap to the next place up** in X: `value(A) - value(T)`, where A is the
  next distinct value strictly above T's. It is the amount needed **to tie**
  that place, so it is labelled "to tie". Tied teams each have a gap to the
  next higher distinct value: with values 10, 8, 8, both 8s show a gap of 2
  to the 10. The gap is None for the best value and for teams with no value.
  The "above" teams are all the teams at value A (names joined, by name). A
  gap is rounded up for display, so a positive gap never shows as 0.
  Category gaps are shown as the difference in the **mean skater's per-82
  rate** (gap × the category's divisor), e.g. "G: 2.1 per 82 to tie Team X",
  with the norm gap in the details. The TTLTST gap stays in TTLTST units.

### The screen

- **Categories view:** each category cell shows the mean norm, with the rank
  under it in small type ("#2") and the cell shaded by heat (§ open question 1).
  Columns: Team, Roto, TTLTST, G … BLK. The Pctl is in the cell's `title` and
  in a "Ranks" line when a row is tapped (see below), to keep the phone width.
- **Salary view:** Team, Roto, TTLTST (with rank), Payroll, Room: unchanged
  apart from Roto and the TTLTST rank.
- **Sort by any column:** tap a header (Team, Roto, TTLTST, a category,
  Payroll, Room). `sort` and `dir` query params, validated like Players'
  (unknown → 400, quoted at most 12 characters). Default: Roto, best first.
  The owner's row stays highlighted (bold) but is no longer pinned to the top,
  so the order reads as standings (open question 2).
- **Team details, no script:** the team name becomes a native
  `<details><summary>`, as Players' rows already do (`_player_rows.html:8`;
  the CSP `default-src 'self'` forbids inline handlers). Inside it: a link
  "Roster →" (today's tap target) and a small list of every column: value ·
  rank · Pctl · gap to tie. The expanded content wraps (`white-space: normal`,
  overriding the table cells' `nowrap`), so it can't widen the table.
- **My gaps** (above the table): one line per category for the owner's team,
  **in category order** (gaps in different units can't be ranked against each
  other): "G #4 · 2.1 per 82 to tie Team X". It is information, not a
  recommendation (SPEC §1 keeps waiver/trade recommendations out of scope).
  Hidden when the owner's team isn't known (`view.my_team` is None).
- Heat shading must not be the only cue: the rank number is always printed,
  and the colours meet contrast on white.

## 2. What changes, what stays

Changes:
- `domain/standings.py` (new, pure, test-first, mutmut): `standings(teams:
  Mapping[str, TeamProfile], players: Sequence[Rating], pool_size: int,
  divisors: Mapping[Category, float]) -> Mapping[str, TeamStanding]`, where
  `TeamStanding` holds, per column, `Cell(value, rank, pctl, points, gap,
  gap_per82, above_team)` and `roto_total`.
- `services/teams.py`: `league_table` returns the summaries with their
  standing; sorting moves to a `LeagueQuery(sort, descending)` with
  `from_params`, like `PlayersQuery`.
- `web/routes/league.py`, `templates/league.html`, `static/app.css` (5 heat
  classes, the details row), `_macros.html` if the sort header macro is shared
  with Players.
- `docs/SPEC.md` §7.3 and §5 (the definitions above), `docs/DECISIONS.md`
  (owner's choices, scope note: information, not recommendation).

Stays: `team_profile`, `compare` / Matchup, the engine, Players' Pctl, the
season and view toggles, payroll / room.

## 3. Phases (one commit each, each green)

1. `domain/standings.py`, test-first (categories from the profiles, with a
   subset-of-categories test):
   - examples for ranks with ties, roto points with ties, and pctl;
   - pctl matches `engine.rate` exactly when the team value equals a
     player's score;
   - gaps and gaps per 82: tied leaders, tied followers (10, 8, 8), several
     teams at the target value, and a tiny positive gap;
   - None handling (a team with no profile players; a category with one
     team).
   - Hypothesis properties: each category's roto points sum to n(n+1)/2;
     ranks lie in 1..n; pctl lies in [0, 100) and never falls as the value
     rises.
   - mutmut.
2. `LeagueQuery` + `league_table` with standings and sorting; service tests:
   - every sort key in both directions, None last, an unknown sort → error;
   - **the wiring:** a two-season fixture whose team order reverses and
     whose divisors differ, asserting ranks, Pctl, points and per-82 gaps
     for each season;
   - the Pctl denominator over a pool with rated skaters, skaters below the
     GP floor with GP > 0, GP-0 skaters and goalies;
   - a season with every GP at 0, and one with GP > 0 but nobody eligible:
     everything unavailable, no error;
   - no owner's team (no My gaps).
3. Route + template + CSS: both views, heat classes, team details, My gaps,
   sort headers carrying `view` and `season`. Screen tests on the demo league,
   including escaping and the 400s. League renders the whole table (8 rows):
   no paging, no htmx. **Manual check at 390 px:** opening and closing
   details, reading Pctl, scrolling sideways, following "Roster →".
4. Docs.

## 4. Risks

- **Width on a phone:** 10 columns in the Categories view. Mitigation:
  rank inside the cell (no new column), Pctl only in the details row, Roto
  as one narrow column. Checked at 390 px before the PR.
- **Over-reading rates as standings:** the "Roto (rates)" label and a note;
  SPEC §5's "profiles, not projected weekly totals" line applies.
- **Small samples:** a team's mean over few profile players moves a lot.
  Shown as is (the profile already does this); no new rule.

## 5. Open questions (with recommendations; all four confirmed by the owner, 2026-11-16)

1. **What the heat shading follows.** Team means cluster high on the players'
   Pctl scale (most teams may sit at 60–90), so shading by Pctl would make
   most cells look alike. *Recommend: shade by the team's rank in that column*,
   so strengths and weaknesses stand out, and keep the Pctl as a number.
   Band = from the rank's position (rank − 1) / (n − 1) over the n teams
   with a value, in 5 equal steps; ties share a band; a cell with no value,
   or n = 1, is neutral.
2. **The owner's team pinned first?** Today it is. *Recommend: not pinned*
   when sorting (standings read top to bottom), bold and with a "you" marker.
3. **Default sort.** *Recommend Roto (rates)*, the closest to standings. The
   alternative is TTLTST, today's order.
4. **Gap units.** *Recommend per-82 units* for categories ("+2.1 G per 82"),
   with the norm gap in the details row. The alternative is norm units only.

## 6. Review (Codex `gpt-6.1-sol`, high, read-only, 2026-11-16): ready with changes

| # | Severity | Finding | Triage |
|---|---|---|---|
| 1 | Medium | "the 7 categories" conflicts with SPEC §5 (categories are config); a subset config would miss columns/divisors. | Accepted: categories come from the profiles, with a subset test (§1, §3.1). |
| 2 | Medium | Sorting My gaps by size compares different units. | Accepted: category order (§1). |
| 3 | Medium | "To pass" vs the formula (which ties); tie policy unclear (10, 8, 8); several teams at the target. | Accepted: "to tie", tied teams gap to the next distinct value, all target teams named, round up (§1, §3.1). |
| 4 | Medium | Tests don't check the season / pool wiring or degenerate pools. | Accepted: two-season, denominator, empty-pool and no-owner tests (§3.2). |
| 5 | Medium | The details toggle: the CSP forbids inline scripts; `nowrap` cells would widen. | Accepted: native `<details>` as on Players, wrapped content, a manual 390 px check (§1, §3.3). |

Open questions: Codex agrees with all four recommendations. On 1 it adds:
neutral cells with no value, ties share a band, and bands for fewer teams.
That is now in the recommendation.
