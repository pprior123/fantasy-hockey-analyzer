# Plan: Players opens on Free Agents, not All

Status: draft, 2026-11-27. The owner's request (2026-11-27): "the players page
should default to free agents and not ALL".

## 1. The decision

- **Default owner filter: Free Agents.** `/players` with no `owner` (or a blank
  one) lists free agents (`Owner.FREE`: not on any roster, waivers included).
  The Free Agents chip is lit. Players is the app's start page (login's
  `DEFAULT_NEXT`, the PWA `start_url`), so the app now opens on free agents.
- **All stays a chip, now explicit:** its link carries `owner=all`, as every chip
  link already does (`url('/players', params, owner=...)`). No template change.
- **A name search still starts from the whole league** (SPEC §7.1, DECISIONS
  "Players: name search"). The search form carries only the display state, not
  `owner`, so a search with no `owner` means All, not the new default. Once the
  results show, the chips narrow them as before (a chip link keeps `q`).
- **Clear** (on a search) goes back to the default: Free Agents.
- **Unchanged:** the other chips, the team picker (which overrides the owner
  chip), position, min GP, 0-GP hiding (free agents are not a single team, so
  0-GP free agents stay hidden unless asked for), sort, paging, rank and
  Pctl over the whole league, the Matchup link (`owner=free`, explicit).
- **Saved links** without `owner` now open on free agents. That is the point of
  the change; nothing else in the app links to `/players` with a filter it
  expects to be All.

## 2. What changes

- `services/players_table.py`, `PlayersQuery.from_params`: a blank `owner` is
  `Owner.ALL` when `q` is a search (has a word), else `Owner.FREE`. The dataclass
  default becomes `Owner.FREE` too, so a bare `PlayersQuery()` means what a bare
  URL means; tests that build one for All say so.
- `docs/SPEC.md` §7.1: "Filter chips: All / My Team / Free Agents / Taken
  (default: Free Agents; a name search starts from All)".
- `docs/DECISIONS.md`: a dated entry (what, why, alternatives).
- Tests (written first, failing):
  - `from_params({})` and `from_params({"owner": ""})` give `Owner.FREE`;
  - `from_params({"q": "mcdavid"})` gives `Owner.ALL`; `{"q": "!!"}` (no words)
    gives `Owner.FREE`; `{"q": "x", "owner": "mine"}` keeps `MINE`;
  - the page: `/players` lists only free agents with the Free Agents chip lit;
    `/players?owner=all` lists everyone with All lit; a search with no owner
    finds a rostered player; the All chip's href carries `owner=all`;
  - existing tests that relied on the bare URL meaning All pass `owner=all`.

## 3. Phases

1. Tests, then the default (one commit, green).
2. SPEC, DECISIONS, plan status (one commit).

## 4. Open questions

None. (Recommended and assumed above: a search with no chip still covers the
whole league, and Clear returns to Free Agents.)

## 5. Review

Pending (Codex `gpt-6.1-sol`, high, read-only).
