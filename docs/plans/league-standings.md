# Plan: League screen ranks, percentiles, roto points, gaps

Status: draft, 2026-11-16. Owner chose the features (2026-11-16): rank AND
percentile for each of the 7 categories and overall, sort by any column, heat
shading, roto points, and the gap to the team above.

## 1. The decision

League (SPEC §7.3) today shows one row per team: mean TTLTST, then either
payroll and room (Salary view) or the 7 category mean norms (Categories view),
with the owner's team pinned first and the rest by TTLTST. It becomes a
standings table built from the same team profiles (SPEC §5).

### Definitions (all from `team_profile`: rated skaters outside IR / IR+ slots)

For each team T and each column X in {G, A, PPP, PIM, HIT, SOG, BLK, TTLTST}:
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
  sum over the 7 categories; None if any category has no value for the team.
  It is from rates per 82 games, not Yahoo's season totals, so it's labelled
  "Roto (rates)" and the page notes it is a guide, not the standings.
- **Gap to the team above** in X: `value(A) - value(T)`, where A is the team with the smallest value strictly above T's.
  None for rank 1, and for teams with no value. Ties have no gap (a tied team
  is not "above"). Shown in per-82 units where possible (a category gap ×
  that category's divisor = the difference in the mean player's per-82
  total), e.g. "G +2.1 per 82 to pass Team X"; the TTLTST gap stays in TTLTST
  units.

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
- **Tap a team row** (today: a link to Rosters): the name stays a link to
  Rosters. A small "▸" toggle opens a details row with, for each column,
  value · rank · Pctl · gap to the team above (in per-82 units).
- **My gaps** (above the table): one line per category for the owner's team:
  "G #4, +2.1 per 82 to pass Team X", sorted by the smallest gap first, so
  the closest places to gain read first. It is information, not a
  recommendation (SPEC §1 keeps waiver/trade recommendations out of scope).
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

1. `domain/standings.py`, test-first:
   - examples for ranks with ties, roto points with ties, and pctl;
   - pctl matches `engine.rate` exactly when the team value equals a
     player's score;
   - gaps and gaps per 82;
   - None handling (a team with no profile players; a category with one
     team).
   - Hypothesis properties: each category's roto points sum to n(n+1)/2;
     ranks lie in 1..n; pctl lies in [0, 100) and never falls as the value
     rises.
   - mutmut.
2. `LeagueQuery` + `league_table` with standings and sorting; service tests
   (every sort key, both directions, None last, unknown sort → error).
3. Route + template + CSS: both views, heat classes, details row, My gaps, sort
   headers carrying `view` and `season`; screen tests on the demo league,
   including escaping and the 400s; check at 390 px.
4. Docs.

## 4. Risks

- **Width on a phone:** 10 columns in the Categories view. Mitigation:
  rank inside the cell (no new column), Pctl only in the details row, Roto
  as one narrow column. Checked at 390 px before the PR.
- **Over-reading rates as standings:** the "Roto (rates)" label and a note;
  SPEC §5's "profiles, not projected weekly totals" line applies.
- **Small samples:** a team's mean over few profile players moves a lot.
  Shown as is (the profile already does this); no new rule.

## 5. Open questions (with recommendations)

1. **What the heat shading follows.** Team means cluster high on the players'
   Pctl scale (most teams may sit at 60–90), so shading by Pctl would make
   most cells look alike. *Recommend: shade by the team's rank in that column*
   (8 ranks → 5 bands: 1, 2–3, 4–5, 6–7, 8), so strengths and weaknesses
   stand out, and keep the Pctl as a number.
2. **The owner's team pinned first?** Today it is. *Recommend: not pinned*
   when sorting (standings read top to bottom), bold and with a "you" marker.
3. **Default sort.** *Recommend Roto (rates)*, the closest to standings. The
   alternative is TTLTST, today's order.
4. **Gap units.** *Recommend per-82 units* for categories ("+2.1 G per 82"),
   with the norm gap in the details row. The alternative is norm units only.
