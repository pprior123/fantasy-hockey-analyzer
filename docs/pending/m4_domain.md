# Pending for DECISIONS (M4 stream D: domain profiles and cap)

## 2026-09-26 — M4: team profiles, matchup and need_score as built (SPEC §5)
`fha.domain.profiles`:
- **Input:** `Member(player_id, in_ir_slot, rating)`. `rating` is the engine's
  `Rating`, or None for a goalie (or a player the engine didn't rate). Domain
  stays free of Yahoo models; the view layer builds the `Member`s from roster
  entries. A player listed twice is an error.
- **Profile players:** rated skaters outside IR / IR+ slots. Per category,
  the mean (`statistics.fmean`) and the population std dev (`pstdev`); also
  the mean TTLTST. With no profile players every value is None. With one
  player the std dev is 0.
- **`compare(me, opp)` gives `Matchup(diff, trailing)`:**
  - `diff[cat] = me − opp`, or None when either team is empty.
  - `trailing` is the categories where `diff < close_margin` (strictly). A
    level category (diff 0) trails, since "behind or close" includes level.
    With an empty side, nothing trails.
  - Profiles on different category lists can't be compared: that is an
    error.
- **`need_score(rating, trailing)`:** the player's norms summed over the
  trailing categories (`math.fsum`), and 0.0 with none trailing. It is
  **None for an unrated player**, not 0, so an unrated free agent doesn't
  rank level with players who genuinely add nothing. The Matchup shortcut
  sorts None last.
- **`ProfileConfig(close_margin=0.05, categories=SKATER_CATEGORIES)`:**
  - The margin must be a finite number ≥ 0. At 0, a category trails only
    when behind or level. A negative margin would stop treating a level
    category as trailing, which contradicts SPEC's "behind or close". NaN or
    inf would make every comparison meaningless.
  - The categories must be non-empty and unique, and should be the engine's
    (`EngineConfig.categories`). A rating missing a configured category's
    norm is an error, not a silent 0.

Alternatives, all rejected for the reasons above:
- `need_score` 0 for unrated players;
- the sample std dev (SPEC says population);
- a negative margin.

## 2026-09-26 — M4: salary-cap predicates as built (SPEC §5)
`fha.domain.cap`:
- **`cap_room(cap, payroll)`** can be negative (over the cap).
- **`fits(aav, room)`** is `aav <= room`, so an over-cap team can't make even
  a $0 pickup.
- **`room_after(room, drop, add)` and `swap_ok(...)`** follow SPEC.
- **`CapHit(aav, counted)`** is a rostered player as the sheet has him.
  `counts(drop)` is False for an IR row (`counted=False`) and for a player
  missing from the tab (`drop=None`).
- **Unknowns:** None means unavailable, and the predicates then return
  **None, never False**. The cases:
  - unknown room: an unrecognized or unbound tab;
  - unknown incoming AAV: a free agent without one;
  - a *counted* drop whose salary is unknown (the sheet's `???`): the freed
    amount is unknown.

  An IR or missing drop frees nothing even when its salary is unknown, so
  that case stays defined.
- **The incoming player's IR status** isn't an input, so it can't change
  either predicate.
- **Amounts:** whole dollars. A negative AAV, cap or payroll is refused, and
  so is a bool. Room may be negative.

Mutation testing: `mutmut` over `src/fha/domain`, 936 mutants, all killed.
