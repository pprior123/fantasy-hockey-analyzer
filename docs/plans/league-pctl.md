# Plan: League ranks teams by the Pctl of their average TTLTST, not Roto

Status: draft, 2026-11-16. The owner's decision (2026-11-16), after trying the
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
- **Default sort: Pctl, best first.** It is the same order as TTLTST, since
  Pctl rises with the value. The Pctl header sorts too (`sort=pctl`).
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
  a pointer).

Stays: ranks, gaps, per-82 gaps, heat, details, My gaps, the 400s, Players.

## 3. Phases (one commit each, each green)

1. Domain: remove roto points and the roto total; mutmut on
   `fha.domain.standings*`.
2. Service: `LeagueSort.PCTL` (default), tests:
   - every sort key in both directions, with blanks last;
   - `sort=roto` refused;
   - Pctl order equals TTLTST order.
3. Template and screen tests:
   - the Pctl column's values, from the service data;
   - the default order;
   - the Pctl header sorts and flips;
   - no "Roto" anywhere on the page;
   - the note;
   - a 390 px check.
4. Docs.

## 4. Open question (with a recommendation)

1. **Pctl of the average, or the average of the players' Pctls?** The plan uses
   the first: the Pctl of the team's average TTLTST, as the owner asked. It
   reads as "a player rated at your team's average sits at the Nth
   percentile". The other, the mean of the profile players' own Pctls, reads
   more literally as "your players average the Nth percentile". On the demo
   league it runs 5 to 10 points lower (Team 4: 85 against 80), and it can
   order teams differently from TTLTST. *Recommend the Pctl of the average*:
   it is what the owner described, it is already computed and tested, and it
   orders teams exactly as TTLTST does.
