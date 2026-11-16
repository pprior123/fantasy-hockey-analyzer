# Plan: League ranks teams by the Pctl of their average TTLTST, not Roto

Status: draft, Codex-reviewed, 2026-11-16. The owner's decision (2026-11-16), after trying the
League standings (PR #26): "I'd prefer the existing percentile method that
converts avg TTLTST to percentile value … so I can see that my average is 90th
percentile or something". Scope chosen: **overall only**.

## 1. The decision

- **Remove Roto** from League: the column, the sort, the note, and the roto
  points in `domain/standings.py`. It has no other use, and "encode only what
  is decided" (the owner found it hard to read).
- **Add a visible Pctl column** next to TTLTST. Its value is the Pctl the
  standings already compute for each team's average TTLTST: where a player
  with that TTLTST would sit on the Players scale,
  `(1 - (1 + #rated players above) / pool_size) × 100`. Today it shows only in
  the cell's title and the team details.
- **Default sort: Pctl, best first,** with ties broken by TTLTST in the same
  direction, then by team name. Pctl never falls as TTLTST rises, but it is a
  step function (`1 + #players above`). Two teams with different averages
  can share a Pctl when no player's TTLTST lies between them, and only the
  TTLTST tie-break keeps the higher average first (Codex, plan review).
  Sorted this way, the order is exactly TTLTST's. The Pctl header sorts too
  (`sort=pctl`); TTLTST's header keeps its own sort.
- **Unchanged:**
  - category cells keep their mean norm, #rank and rank-based heat shading;
  - the team details (value, rank, Pctl and gap per column);
  - My gaps;
  - the owner's row stays unpinned (bold, "you");
  - Salary view: Team, TTLTST, Pctl, Payroll, Room.
- **`sort=roto`**, from a link saved since PR #26 merged earlier today, becomes
  a 400 ("unknown sort"), like any other unknown sort. No alias: nothing else
  links to it.
- **The note under the table** explains the Pctl. It is where a player rated
  at the team's average TTLTST would rank among the league's players (the
  Players scale), over the team's rated skaters outside IR, and it is from
  rates. The "Roto" sentence goes.

## 2. What changes, what stays

Changes:
- `domain/standings.py`:
  - drop `Cell.points` and `TeamStanding.roto`, and the `roto` flag of
    `_column`;
  - tests and the module docstring follow;
  - mutmut.
- `services/teams.py`: `LeagueSort.ROTO` becomes `LeagueSort.PCTL`, the
  default, keyed on `standing.ttltst.pctl`; the tests follow.
- `web/templates/league.html`: the Roto column becomes a Pctl column after
  TTLTST, and the note changes. The route is unchanged.
- `docs/SPEC.md` §5 ("League standings": drop the roto lines) and §7.3; a
  DECISIONS entry (the owner reverses Roto; the 2026-11-16 League entry gets
  a pointer); `docs/plans/league-standings.md` gets a dated "Roto superseded
  by plans/league-pctl.md" line, its text otherwise kept as history.

Stays: ranks, gaps, per-82 gaps, heat, details, My gaps, the 400s, Players.

## 3. Phases (one commit each, each green)

Ordered so each commit stays green (Codex, plan review): add the Pctl first,
then remove Roto from every layer at once.

1. **Add Pctl** while Roto still exists.
   - Service: `LeagueSort.PCTL` with the (Pctl, TTLTST, name) order, and the
     default sort.
   - Template: the Pctl column after TTLTST.
   - Tests:
     - every sort key in both directions, with blanks last, including a team
       with no value after a genuine Pctl 0;
     - the step-function case in both directions: two teams with one Pctl and
       different TTLTST, the higher average first in the best-first order
       (players [1, 3] and [1, 4] give one Pctl);
     - exact TTLTST ties, by name;
     - Pctl order equals TTLTST order on both demo seasons.
   - Screen tests: the Pctl column's values (from the service data, "—"
     against 0 in both views), the default order, and the Pctl header sorting
     and flipping.
2. **Remove Roto** from the domain (`Cell.points`, `TeamStanding.roto`, the
   `roto` flag), the service (`LeagueSort.ROTO`), the template (column, note)
   and every test that uses them, in one commit.
   - Assertions in mixed tests that aren't about Roto are kept, such as the
     ranks-in-1..n property.
   - The note explains the Pctl.
   - `sort=roto` joins the screen's 400 cases.
   - No "Roto" is left on the page.
   - mutmut on `fha.domain.standings*`.
   - A 390 px check.
3. Docs.

## 4. Open question (answered: the owner chose the Pctl of the average, 2026-11-16)

1. **Pctl of the average, or the average of the players' Pctls?** The plan uses
   the first: the Pctl of the team's average TTLTST, as the owner asked. It
   reads as "a player rated at your team's average sits at the Nth
   percentile". The other, the mean of the profile players' own Pctls, reads
   more literally as "your players average the Nth percentile". On the demo
   league it runs 5 to 10 points lower (Team 4: 85 against 80), and it can
   order teams differently from TTLTST. *Recommend the Pctl of the average*:
   it is what the owner described, it is already computed and tested, and,
   with TTLTST breaking Pctl ties, it orders teams exactly as TTLTST does.

## 5. Review (Codex `gpt-6.1-sol`, high, read-only, 2026-11-16): ready with changes

| # | Severity | Finding | Triage |
|---|---|---|---|
| 1 | High | Pctl is a step function (`1 + #players above`), so two teams with different TTLTST can share a Pctl; the name tie-break could then put the lower average first. "Same order as TTLTST" was false. | Accepted: the sort is (Pctl, TTLTST, name), with the counterexample tested in both directions (§1, §3.1). |
| 2 | Medium | Removing Roto from the domain first breaks the service and template, so the phases wouldn't each be green. | Accepted: add Pctl first, then remove Roto from every layer in one commit, keeping the non-Roto assertions (§3). |
| 3 | Low | The implemented League plan still prescribes Roto with no pointer to its replacement. | Accepted: a dated pointer line (§2). |

Codex also asked to keep the tests for a missing value after a genuine 0, for
empty pools, and for "—" against 0, and to add `sort=roto` to the screen's
400 cases. All are in §3. On the open question, Codex agrees: the Pctl of the
average.
