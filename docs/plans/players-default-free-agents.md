# Plan: Players opens on Free Agents, not All

Status: Approved by the owner 2026-11-27; implemented

The owner's request (2026-11-27): "the players page
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
  chip), position, min GP, 0-GP hiding (outside a name search, free agents are
  not a single team, so 0-GP free agents stay hidden unless asked for; a
  search still shows them), sort, paging, rank and Pctl over the whole league.
  Matchup's "Free agents who help here" is its own page
  (`/matchup/free-agents`), not a Players link, so it is untouched. SPEC §7.4
  says it "opens Players filtered to free agents": that is old drift, and
  this change corrects only the wording.
- **Saved links** without `owner` now open on free agents. That is the point of
  the change; nothing else in the app links to `/players` with a filter it
  expects to be All.

## 2. What changes

- `services/players_table.py`, `PlayersQuery.from_params`: a blank `owner` is
  `Owner.ALL` when `q` is a search (has a word), else `Owner.FREE`. **The
  dataclass default stays `Owner.ALL`**: the default is a URL decision, made
  where the URL is parsed, so `PlayersQuery(q="connor")` and the service tests
  keep their league-wide meaning (Codex finding 1). The one test comparing a
  parsed search with a constructed one (`test_players_table.py:345`) still
  holds; a new assertion pins `from_params({}) == PlayersQuery(owner=Owner.FREE)`.
- `docs/SPEC.md` §7.1: "Filter chips: All / My Team / Free Agents / Taken
  (default: Free Agents; a name search starts from All)". §7.4: the Matchup
  link opens its own free-agent page (wording only).
- `docs/DECISIONS.md`: a dated entry (what, why, alternatives).
- Tests (written first, failing):
  - `from_params({})` and `from_params({"owner": ""})` give `Owner.FREE`;
  - `from_params({"q": "mcdavid"})` gives `Owner.ALL`; `{"q": "!!"}` (no words)
    gives `Owner.FREE`; `{"q": "x", "owner": "mine"}` keeps `MINE`;
  - the page: `/players` lists only free agents with the Free Agents chip lit;
    `/players?owner=all` lists everyone with All lit; a search with no owner
    finds a rostered player; the All chip's href carries `owner=all`;
  - ownerless links keep the free-agent scope: sort headers, season and view
    toggles, Apply (the filter form), "show them" / "hide 0 GP", and both
    paging paths (the no-JS `href`, whole page so far; the htmx `hx-get`,
    one page) give the same free-agent sequence with no repeats; `owner=all`
    survives the same links;
  - a team overrides the owner chip (no chip lit); back to Team "Any" with no
    owner gives free agents again;
  - the default page's 0-GP note counts hidden free agents only;
  - a search narrowed to Free Agents still shows a 0-GP free agent.
- Existing web tests, migrated case by case, not wholesale (Codex finding 2):
  - tests about the whole list (paging totals `test_screens.py:230-253`,
    the search-inclusion lists `343-348`, the rank test's "every row" page
    `498-503`, the Totals row `719-721`, the demo salary count
    `test_demo_screens.py:53`) pass `owner=all`;
  - tests about the bare URL keep it and change their expectations: Clear
    (`test_screens.py:421-431`: the same URL, now the played free agents and
    the free agents' 0-GP count) and the blank or punctuation-only search
    (`365-375`: no search, so the default, free agents).

## 3. Phases

1. Tests, then the default (one commit, green).
2. SPEC, DECISIONS, plan status (one commit).

## 4. Open questions

None. (Recommended and assumed above: a search with no chip still covers the
whole league, and Clear returns to Free Agents.)

## 5. Review (Codex `gpt-6.1-sol`, high, read-only, 2026-11-27): ready with changes

The log is on PR #28. Each finding was checked against the code.

| # | Sev | Finding | Triage |
|---|---|---|---|
| 1 | Medium | Changing the dataclass default also changes directly built searches (`PlayersQuery(q=...)` would search free agents) and many service tests | Accepted, by a simpler design: the dataclass default stays All; only `from_params` applies Free Agents (§2) |
| 2 | Medium | "Pass `owner=all`" is too blunt: Clear and the blank search must keep the bare URL and change expectations; lists the affected tests | Accepted: case-by-case migration list in §2 |
| 3 | Medium | No tests for ownerless sort/toggle/Apply links or the two paging paths keeping the default | Accepted: tests added to §2 |
| 4 | Low | No Matchup link to Players exists; it is `/matchup/free-agents`; SPEC §7.4 has drifted | Accepted: §1 corrected; SPEC wording fixed, no behaviour change |
| 5 | Low | The 0-GP statement needs "outside a name search" | Accepted: §1 |
