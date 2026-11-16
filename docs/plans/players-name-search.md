# Plan: Players name search

Status: draft, 2026-11-16. Owner agreed to the feature as proposed (2026-11-16).

## 1. The decision

A name box on **Players**. Type part of a name, tap Search (or the phone's Go),
and the list shows the players whose names match, **whoever owns them** (the
owner's team, another team, or a free agent), **0-GP players included**, in
whichever view is open (Salary, Categories, Totals). Rank and Pctl don't
change: they stay computed over the whole league (SPEC §7.1), so a searched
player shows his real rank.

Why: today, finding one player means picking the right owner chip, perhaps
showing 0-GP players, and scrolling a 1,600-row list 100 rows at a time.

### Matching

`name_matches(query, name)`, a new pure function in `domain/names.py`:
- both sides go through the existing `normalize_name`, which casefolds, strips
  accents and punctuation, and turns "Last, First" into "first last";
- every word of the query must appear inside the name, in any order
  ("mcd", "connor mc", "mcdavid connor", "Stützle" ↔ "stutzle" all match);
- a query that normalizes to nothing ("", "  ", "!!") is no search.

Not fuzzy: a typo finds nothing, and the "no match" line says so. Admin's
"Find a player" is a single substring test (`admin.py` `_search`); it is left
as is (not this feature's scope).

### What a search does to the other filters

- **Submitting the name box starts from the whole league.** Its form carries
  only `q` and the display state (`view`, `season`, `sort`, `dir`). Owner chip,
  team, position and min GP are reset, so "whoever owns them" holds even if My
  Team was showing.
- **After a search, the filters still narrow it.** The chips, the team picker,
  position, min GP, the sort headers, the view toggle and Show more all carry
  `q` (they already pass every query param through `url(...)` or hidden inputs).
  So "Sidney" then the Free Agents chip works.
- **0-GP players are included while `q` is set**, as on a single team's list
  (`hide_zero = not (zero_gp or single_team or q)`), and the 0-GP note is not
  shown.
- A **Clear** link next to the box returns to the unfiltered list. It is
  built from the same display-state allowlist as the search form (`view`,
  `season`, `sort`, `dir`), not `url(params, q=None)`, which would keep a chip
  or team chosen after the search (`format.url` keeps every other param).

## 2. What changes, what stays

Changes:
- `domain/names.py`: `name_matches` (pure, test-first, mutmut).
- `services/players_table.py`: `PlayersQuery.q: str | None`, read in
  `from_params`. Over 40 characters is a `QueryError`, i.e. a 400, like the
  other params; the 400 page quotes at most 12 characters, per the
  convention. The query is normalized once; `_filtered` keeps matching rows
  and `zero_gp_hidden` returns 0 while searching.
- `web/templates/players.html`: a search form above the owner chips; `q` added
  to the filter form's hidden inputs; a Clear link; the count line says
  "N players match “q”" (escaped by Jinja), or "No player matches “q”".
- `docs/SPEC.md` §7.1: one sentence. `docs/DECISIONS.md`: an entry.

Stays: rank and Pctl (computed in `build_view` before any filter), sorting,
paging (Show more carries `q`), Rosters, Admin's search, the matcher.

## 3. Phases (one commit each, each green)

1. `name_matches` + tests: the examples above, accents, the comma form,
   word order, punctuation, and **false cases**: an unrelated name, a
   misspelling ("mcdavdi"), and "connor crosby" against "Connor McDavid" (every
   word must match, not any). A query that normalizes to nothing is "no
   search", decided by the caller (`PlayersQuery`), not by `name_matches`.
   Hypothesis: a name matches each of its own words; adding a word that isn't
   in the name never turns a miss into a match. mutmut on `fha.domain.names*`.
2. `PlayersQuery.q`, `_filtered`, `zero_gp_hidden`; tests in
   `test_players_table.py`:
   - matches across owners and free agents;
   - 0-GP players included **without** `zero_gp=1`;
   - combined with an owner chip, a team, a position;
   - absent, blank and punctuation-only `q` behave exactly like no `q`,
     including the normal 0-GP hiding;
   - an over-long `q` is refused;
   - zero results;
   - a goalie and an unrated skater are found by name;
   - tied names.
3. Template + route tests in `test_screens.py`:
   - the box; the Clear link's URL **and** its rows after a search narrowed by
     a chip;
   - `q` carried by chips, sort headers, the filter form and the view toggle;
   - submitting resets owner, team, position and min GP;
   - **paging, end to end:** a query matching over 200 demo players (e.g.
     "a"), followed through the htmx `/players/rows` links to the end
     (exact ordered rows, none repeated or unfiltered) and through the plain
     `/players?start=` link (cumulative rows), as the existing paging test
     does; a new search or filter resets `start`;
   - escaping of the **echoed query**: a `q` with quotes, `&` and markup, with
     and without matches, is escaped in the input value, the hidden input and
     the count / no-match text, and round-trips through the generated URLs;
   - rank parity: a rated player below first place (non-null rank and Pctl)
     has the same values searched as unfiltered (compared in the service
     data, since the row shows Pctl but no rank cell), in both seasons, with
     a player whose GP differs between them.
   - **Manual check at 390 px** (not a route test): a full-length query,
     Search, Clear and the no-match text fit; wrapping is fine.
4. Docs (SPEC, DECISIONS).

## 4. Risks

- Hygiene: `q` is echoed into the page only through Jinja autoescape and
  `url(...)` (which URL-encodes). Nothing is stored or logged.
- Performance: one `normalize_name` per row per request (~1,600 rows); the
  same cost Admin's search already pays. Fine for serverless.

## 5. Open questions (with recommendations)

1. **Live search as you type** (htmx `keyup delay:300ms` swapping the table)?
   *Recommend no for now:* a form submit works without JS, is simpler to
   test, and the phone keyboard's Go key is one tap. Add it later if the
   submit feels slow.
2. **Should Admin's "Find a player" use `name_matches` too** (word order
   free)? *Recommend yes, but in a later small PR*, so this one changes one
   screen.

## 6. Review (Codex `gpt-6.1-sol`, high, read-only, 2026-11-16): ready with changes

| # | Severity | Finding | Triage |
|---|---|---|---|
| 1 | Medium | Clear via `url(params, q=None)` keeps chips/filters chosen after the search (`format.url` keeps every other param). | Accepted: Clear uses the display-state allowlist (§1). |
| 2 | Medium | The domain tests couldn't fail for an always-true or any-word matcher. | Accepted: false cases and a monotonic property; the "no search" rule lives in `PlayersQuery`, tested there (§3.1, §3.2). |
| 3 | Medium | Paging needs route-level traversal tests, not just "`q` is carried". | Accepted: end-to-end paging through both link kinds (§3.3). |
| 4 | Medium | Fixtures: the existing `EVERY_ROW` sets `zero_gp=1` and would bypass the new rule; rank parity could compare blanks; seasons differ. | Accepted (§3.2, §3.3). |
| 5 | Low | The escaping test should target the echoed query, not player names (already escaped). | Accepted (§3.3). |
| 6 | Low | A TestClient can't check 390 px. | Accepted: a manual check (§3.3). |

Open questions: Codex agrees with both recommendations (no live search now;
Admin later, keeping Admin's "empty query finds nothing" behaviour).
