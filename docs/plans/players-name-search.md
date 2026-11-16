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
- A **Clear** link (`q=None`) next to the box returns to the unfiltered list.

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

1. `name_matches` + tests (examples above, an empty query, accents, comma
   form, multi-word order, punctuation; a hypothesis property: a name always
   matches each of its own words). mutmut on `fha.domain.names*`.
2. `PlayersQuery.q`, `_filtered`, `zero_gp_hidden`; tests in
   `test_players_table.py`: matches across owners and free agents, 0-GP
   included, combined with an owner chip / team / position, sort and paging
   unchanged, over-long `q` refused.
3. Template + route tests in `test_screens.py`:
   - the box, and the Clear link;
   - `q` carried by chips, sort headers, the filter form, the view toggle and
     Show more (htmx partial);
   - submitting resets the owner and team;
   - a name with `<script>` is escaped;
   - rank/Pctl of a searched player equal his unfiltered values;
   - at 390 px the box fits the toolbar.
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
