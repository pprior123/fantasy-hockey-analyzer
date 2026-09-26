# Fantasy Hockey Roster Analyzer — Specification

Status: Phase 1 spec, ready for build. Supersedes the original planning draft
(`nhl-fantasy-app-plan.md`, see git history).

## 1. Product

A private, single-user web app for managing one Yahoo NHL fantasy team.
It replaces an Excel workbook whose core value is a custom player rating,
**TTLTST**, shown next to each player's salary (AAV).

- **User:** one person (the owner). No accounts, no sharing.
- **League:** Yahoo NHL league ID `8076`. 8 managers. Head-to-head categories,
  weekly matchups (Mon–Sun). Daily lineups. Max 5 waiver acquisitions per
  week. Keeper league: exactly 8 keepers, each paid their full cap hit.
- **Scoring categories (10):** G, A, PPP, PIM, HIT, SOG, BLK, W, GAA, SV%.
- **Roster:** 16 active (3 C, 3 LW, 3 RW, 6 D, 1 G), bench, 1 IR + 1 IR+.
  Slot counts are read from Yahoo league settings at runtime (the league
  constitution and earlier notes disagree on bench size).
- **Salary cap:** hard cap, same for all teams, set by the league each season
  (currently $119.6M). Cap hits are PuckPedia's; the league sheet (§4a) records
  them for rostered players, a PuckPedia CSV supplies free agents'. Players in
  IR / IR+ slots and players sent to the minors don't count. A pickup needs cap room first.
  Teams must be compliant from the start of the regular season.
- **Primary device:** phone browser, installable to home screen (PWA).
- **Access:** Yahoo Fantasy Sports API, **read-only** (approved; app created).

### Phase 1 goal

Parity with the spreadsheet: one mobile-friendly view of every player with
salary, TTLTST, percentile, and value, filterable by owner; every team's roster
with its category profile, payroll and cap room; a head-to-head matchup view;
and a cap-aware way to find a replacement for a player. When Phase 1
ships, the owner stops opening Excel.

**Phase 1 ends with a deployed, running app the owner uses day to day.**
Phase 2 is planned from that real use (bugs first, then features), not from
this document.

### Explicitly out of scope for Phase 1

Write access to Yahoo (roster moves), goalie rating model, projections,
rest-of-season or schedule weighting (including games-this-week counts),
historical snapshots, waiver *recommendation* features (drop suggestions,
acquisition counter), trade evaluator, multi-user support. (The Matchup
screen's free-agent shortcut and the Replace view in §7 are filters and sorts
over existing data for a player the owner chose, not recommenders.) Do not build these. Phase 2 and 3
are listed in §10 for context only.

## 2. Architecture

```
Phone browser ──HTTPS──▶ Vercel (Python serverless, FastAPI)
                              │
                              ├──▶ Yahoo Fantasy Sports API (OAuth 2.0, read-only)
                              └──▶ Firestore (server-only, service account)
```

- **Runtime:** Python 3.12, FastAPI, deployed to Vercel Hobby (free,
  non-commercial). Nothing runs until a request arrives.
- **UI:** server-rendered Jinja2 templates + HTMX. Mobile-first CSS. No JS
  build step, no SPA framework.
- **Storage:** Firestore (Spark free plan). Accessed **only** from the server
  using a service account. Firestore security rules deny all client access.
- **Refresh model:** no background jobs. On request, if cached stats are older
  than the TTL (default 30 min), refresh from Yahoo, recompute, serve. A
  manual Refresh button forces it.
- **Why not browser-only:** Yahoo's API does not permit browser (CORS) calls,
  and the OAuth client secret cannot ship to a browser.

### Serverless constraints (design for these from day one)

- Short function time limits. A full refresh is ~20–35 Yahoo calls; they must
  run **concurrently** (async `httpx`, bounded concurrency, e.g. 8), never
  sequentially. Target: full refresh under 8 seconds.
- Ephemeral filesystem. No token files, no SQLite, no local caches in
  production. All persistent state lives in Firestore.
- Cold starts. Keep imports light; avoid heavy dependencies (no pandas in the
  request path).

## 3. Layering and interfaces

Keep the domain pure and the edges swappable. Suggested package layout:

```
src/fha/
  domain/      # pure: models, metric engine, name matcher. No I/O.
  sources/     # YahooSource, SalarySource (CSV) — external data in
  storage/     # Repository protocol + InMemoryRepository + FirestoreRepository
  services/    # orchestration: refresh, import, ranking
  web/         # FastAPI app, routes, templates, auth
scripts/       # one-off CLIs (yahoo_auth.py, extract_golden.py)
tests/
  unit/ integration/ fixtures/
```

Required seams (Protocols), each with an in-memory/fake implementation used in
tests:

- `YahooSource` — league settings, teams, rosters, player pool, season stats.
- `LeagueSheetSource` — the league's shared salary spreadsheet (§4a): per
  team tab, the salary rows, IR rows, the tab's own payroll, and the cap.
  Two grid readers feed one pure parser: Google Sheets API (production) and
  a downloaded `.xlsx` (dev, tests).
- `SalarySource` — yields `(name, team, aav)` rows for free agents. Phase 1
  impl: PuckPedia CSV. (PuckPedia has a paid private API; a future impl may
  slot in here.)
- `Repository` — players, stats cache + timestamp, salaries, name bindings,
  aliases, Yahoo tokens, config.
- `Clock` — injectable, so cache-TTL logic is testable.

## 4. Data sources

| Data | Source | Notes |
|---|---|---|
| Settings, teams, rosters | Yahoo API | read-only |
| Player pool + ownership | Yahoo API | `status=T` (taken) + top available by rank, paged 25 |
| Season stat totals | Yahoo API | **source of truth** for stats |
| Last season's stat totals | Yahoo API | pre-season / early-season baseline (§5) |
| Weekly matchups | Yahoo API | league scoreboard, current and next week |
| Salaries of rostered players, payrolls, cap | League shared spreadsheet | **source of truth** (§4a) |
| Salaries of free agents | CSV import | PuckPedia export (the sheet only lists rostered players) |

Where both have a rostered player, the league sheet wins.

### 4a. League salary spreadsheet

The league keeps a shared Google Sheet: one tab per team plus a summary tab
holding the cap. Each GM maintains their own tab by hand (the league requires
it within 24 h of roster changes), so layouts differ between tabs: header row
5–7, salary in column F or G, names as "Last, First" or "First Last" (with
nicknames and typos), NHL teams as codes or city names. Tabs also hold GMs'
contact details. Whole tabs are necessarily fetched (the PAYROLL cell's
position is only known after reading), but the app must **never persist, log
or render any cell outside the resolved payroll range, its header row, the IR
rows, and the CAP / PAYROLL cells**; the raw grid is discarded after parsing.

Parsing rule — **key off the tab's own PAYROLL formula, not its labels**:

- The `PAYROLL` cell's formula (followed through at most two cell
  references, e.g. `=SUM(C36)` → `C36 = SUM(F6:F31)`) names a single-column
  range. That column is the salary column and those rows are the counted
  players. Name / position / team are read from the same rows by header.
- Rows labelled `IR` / `IR+` below the range are IR players (salary not
  counted, per league rules).
- The tab's `PAYROLL` value is the team's official payroll. The cap is the
  summary tab's cap cell (each team tab's `CAP` cell references it); a team
  tab whose `CAP` value differs is flagged in the discrepancy report.
- A tab whose formula cannot be resolved to one column range is reported as
  **unrecognized** in Admin, never guessed at.

Reading: production uses the Google Sheets API (values and formulas, one
batched request per refresh) with the Firestore service account; the owner
shares the sheet view-only with that account's email. The sheet ID comes
from env `LEAGUE_SHEET_ID`. Dev reads a downloaded `.xlsx` at
`LEAGUE_SHEET_XLSX` (under `private/`). Admin also accepts an uploaded
`.xlsx` in any environment, as a fallback when the live read is unavailable
(e.g. before the service account exists, or if sharing is revoked). The
upload path imports `openpyxl` lazily so it stays off the cold-start path.

Binding: each tab is bound once to a Yahoo team (Admin; suggested by
roster-name overlap), then each sheet row is matched **within that team's
Yahoo roster** (§6). Both bindings are persisted.

Discrepancy report (Admin, and a badge on Rosters): Yahoo roster players
missing from the tab, tab players not on the Yahoo roster, and a tab payroll
that differs from the sum of its matched rows. (The league penalizes
out-of-date sheets; this makes staleness visible.)

**League key:** Yahoo needs `{game_key}.l.8076`. Resolve the current NHL
`game_key` at runtime (games endpoint, `game_codes=nhl`); never hardcode it —
it changes every season.

**Player pool scope:** all rostered players (≈216) plus the top ~300 available
players. Not every NHL player. The TTLTST divisors are computed over this pool
(see §5).

**Stat ID mapping:** Yahoo identifies stats by numeric IDs. Build the mapping
from the league settings response, not a hardcoded table. This also resolves
whether Yahoo exposes PPP directly (use it if so; otherwise PPG + PPA).

### Yahoo client choice (M2 spike)

`yfpy` and `yahoofantasy` both support NHL but default to token *files*, which
don't work on Vercel. Spend ≤1 hour checking whether either accepts an
externally supplied token and refresh callback. If yes, wrap it behind
`YahooSource`. If not, write a thin `httpx` client (OAuth refresh + JSON
format via `?format=json`). Record the decision in `docs/DECISIONS.md`.

### OAuth consent (one-time, human-in-the-loop)

Yahoo requires an HTTPS redirect URI; the app registers
`https://localhost:8000`. `scripts/yahoo_auth.py`:

1. Prints the Yahoo authorization URL.
2. The owner opens it, clicks Allow. The browser redirects to
   `https://localhost:8000/?code=...` — the page may fail to load; that's fine.
3. The owner pastes the full redirected URL back into the script.
4. The script exchanges the code, then stores the refresh token via the
   `Repository` (Firestore in prod; a gitignored local file only in dev).

Agents must not attempt this step. It requires the owner.

## 5. Metric engine (must match the spreadsheet)

Pure functions in `domain/`. Seven skater categories only:
**G, A, PPP, PIM, HIT, SOG, BLK.** Goalies are excluded from TTLTST.

```
eligible(p)        = p.gp > 0 and p.gp >= GP_FLOOR_FRACTION * max(gp over pool)
                     # default 0.02
per82(cat, p)      = p.stat[cat] / p.gp * 82                           # eligible only
divisor(cat)       = mean of the min(TOP_N, #eligible) highest per82(cat, ·)
                     over eligible players                              # TOP_N = 10
norm(cat, p)       = per82(cat, p) / divisor(cat), or 0 if divisor(cat) == 0
TTLTST(p)          = arithmetic mean of norm over the 7 categories
rank(p)            = 1 + #{eligible q : TTLTST(q) > TTLTST(p)}   # competition
                     # ranking: equal scores share a rank (1, 2, 2, 4)
percentile(p)      = (1 - rank(p) / N) * 100,  N = #eligible players
value(p)           = p.aav / TTLTST(p) / 1_000_000 # None if no AAV or TTLTST == 0
```

Rules:

- Ineligible players (including GP = 0) are **unrated**: TTLTST, rank,
  percentile and value are all None. They still appear in lists.
- With no eligible players (e.g. before opening night with "This season"
  forced), every player is unrated and the ranking is empty — no error.
- A divisor of 0 (nobody eligible recorded the stat) gives every player
  norm 0 in that category; the category stays in the mean so TTLTST remains
  comparable.
- N and ranks are computed over all eligible players, independent of any UI
  filter. Display order for equal TTLTST: name, then player_id.
- `PPP = PPG + PPA` unless Yahoo provides PPP directly.
- Injured players keep their rate stats and stay ranked (owner's explicit call).
- Divisors are **recomputed on every refresh**. (The workbook stores them as
  static values, which drift stale — a known flaw we are fixing.)
- **Parity wins:** if `extract_golden.py` shows the workbook computes
  percentile, rank ties or eligibility differently from the above, M1 follows
  the workbook, updates this section, and records the change in
  `docs/DECISIONS.md`.
- `GP_FLOOR_FRACTION`, the category list, and the top-N (10) are config, not
  literals.

### Baseline season (pre-season and early season)

Before opening night every current-season GP is 0, and for the first weeks
per-82 rates are noise. So the engine runs over one season's pool at a time,
never mixing seasons:

- If the current season's max GP over the pool is below `BASELINE_MIN_GP`
  (config, default 10), the default view uses **last season's totals**
  (fetched from Yahoo for the same pool of players). Otherwise it uses the
  current season.
- A season toggle (This season / Last season) overrides the default.
- The UI always labels which season's numbers are shown.

### Team profiles and matchup comparison

Pure functions in `domain/`, built on the per-player norms above:

```
profile_players(team)   = the team's rated skaters, excluding IR / IR+ slots
mean_norm(team, cat)    = mean of norm(cat, p) over profile_players(team)
sd_norm(team, cat)      = population std dev of the same values
team_ttltst(team)       = mean TTLTST over profile_players(team)
matchup(me, opp, cat)   = mean_norm(me, cat) - mean_norm(opp, cat)
trailing(cat)           = matchup(me, opp, cat) < MATCHUP_CLOSE_MARGIN
                          # config, default 0.05: "behind or close"
need_score(p)           = sum of norm(cat, p) over trailing categories
```

A team with no profile players has mean, std dev and TTLTST of None (shown
"—"), and no category counts as trailing against or for it. With one player,
std dev is 0.

Goalies are shown with raw W / GAA / SV% only (no model; out of scope).
These compare team *profiles* (rates), not projected weekly totals, which
depend on games played that week (schedule weighting is out of scope). The
UI says so.

### Salary cap

Pure functions in `domain/`. The cap and each team's payroll come from the
league sheet (§4a). `SALARY_CAP` config exists only as an override if the
sheet's cap cell is unreadable. (The constitution's "NHL cap + 7.5%" is out
of date; the sheet is authoritative.)

```
counts(p)          = p is in the tab's PAYROLL range (so not an IR row);
                     false for a player missing from the tab (a discrepancy,
                     flagged per §4a)
payroll(team)      = the tab's official PAYROLL value
cap_room(team)     = cap - payroll(team)
fits(p, team)      = aav(p) <= cap_room(team)
                     # a straight pickup, no drop
room_after(team, drop, add) = cap_room(team) + aav(drop)·counts(drop) - aav(add)
swap_ok(team, drop, add)    = room_after(team, drop, add) >= 0
                     # a Yahoo add/drop, which is one transaction
```

`fits` is for pickups without a drop (the Matchup "fits my cap" filter);
`swap_ok` is for add/drop swaps (the Replace view). Acquiring a player who is
currently injured still needs his full cap hit to fit at the moment of
acquisition — he leaves the payroll only once placed in an IR slot — so the
IR status of the incoming player never changes either predicate. That an
add/drop counts as simultaneous is an assumption about league practice; see
§11.

A free agent with no PuckPedia AAV shows "—" and is excluded from cap
filters, with a count of how many were excluded; never treated as 0.

### Golden reference: the owner's workbook

`2025_2026_stats.xlsx` (not committed — see §8). It holds the **2025-26**
season. The app targets **2026-27**, so 2025-26 is also "last season" for the
baseline rule above. Known structure:

- Sheet `Stats Data Source`: raw scraped stats, ~900 players.
- Sheet `Fantasy Analysis`: per-player derived columns; category divisors in
  `W6:AC6` (static values); TTLTST in column `AD`; ranking table `Table2`
  (`AE:AL`) with percentile and `$/TTLTST`; owner's roster in `Table4`
  (`AP2:BK26`); seven other managers' rosters in tables below it; manager
  comparison `Table10` (`BP10:BZ21`).
- Sheet `Salaries`: `TEAM, POS` / name / cap hit, ~830 rows.
- Sheet `Name Aliases`: 57 hand-maintained name fixes.
- Known label bug: row-2 header labels drift from what formulas pull (e.g.
  column `Q` labelled `hits` pulls blocks from `Stats Data Source!AE`). Trust
  the formulas, not the labels.

`scripts/extract_golden.py` reads the workbook once with `openpyxl`
(`data_only=True`) and writes committed JSON fixtures:

- `tests/fixtures/golden_players.json` — per player: name, GP, the 7 raw
  stats, workbook TTLTST, percentile, AAV.
- `tests/fixtures/golden_divisors.json` — the `W6:AC6` values.
- `tests/fixtures/alias_seed.json` — the 57 aliases.

Golden tests:

1. **Exact parity:** with divisors *injected* from `golden_divisors.json`,
   the engine reproduces workbook TTLTST for every eligible player within
   `1e-3`, and percentiles within `0.1`.
2. **Divisor recompute:** with divisors *computed*, report any difference from
   the workbook's static divisors. A mismatch is expected (stale statics) and
   must be written to `docs/DECISIONS.md` with the numbers — not "fixed" by
   hardcoding.

## 6. Name matching (salary ↔ Yahoo player)

Salary rows are matched to Yahoo `player_id` **once** and the binding
persisted. Never re-match by name on every request.

League-sheet rows are matched **only against the bound Yahoo team's roster**
(~27 candidates), which makes surname-only entries ("Andersen") and typos
("Oetterger") resolvable: a unique surname within the roster is a match;
fuzzy matches are still candidates needing confirmation. PuckPedia rows use
the full cascade against the whole pool. Names may be "Last, First"; the
normalizer handles both orders.

Team codes differ between sources (Yahoo `TB`, PuckPedia `TBL`, the sheet's
"Tampa Bay" or typos like `CLS`). A canonical NHL team map in `domain/`
(codes, Yahoo abbreviations, city and nickname variants) normalizes team
before any "+ team" step; an unmapped team string is treated as unknown
(skips steps 1–2) and reported in Admin, never guessed.

Cascade:

1. Exact name + team.
2. Normalized name + team (strip accents, lowercase, drop punctuation,
   nickname table: Matt/Matthew, Mitch/Mitchell, Alex/Alexander, …).
3. Normalized name only (if unique).
4. Fuzzy (`rapidfuzz`, score ≥ 90) → **candidate only**, requires owner
   confirmation on the review screen.

Seed the alias table from `alias_seed.json`. Every seed alias is also a test
case.

Salary import is **idempotent**: re-importing the same CSV (or re-reading an
unchanged sheet) updates rows in place. A single-player AAV edit exists for
free agents whose contracts change mid-season.

Salary convention: store the cap hit (AAV). Entry-level contracts with
performance bonuses: store base cap hit in Phase 1, and keep a nullable
`cap_hit_with_bonuses` field so the convention can change without a migration.

## 7. Screens (Phase 1)

All must be usable at 390 px width.

Common to the tables: a **Categories** toggle swaps the salary columns
(AAV · $/TTLTST) for the 7 category norms, since all of them will not fit at
390 px. A season label and toggle per §5.

1. **Players** — table: Name · Pos · Team · Owner · GP · TTLTST · Pctl · AAV ·
   $/TTLTST. Tap a header to sort. Filter chips: All / My Team / Free Agents /
   Taken, plus a team picker for any single team; position; min GP. Tap a row
   to expand the 7-category norm breakdown. Sticky header. Last-refreshed time
   and a Refresh button.
2. **Rosters** — team picker (default: the owner's team). The team's players
   with the same metrics, plus the team profile (§5: mean norm per category
   with standard deviation, and mean TTLTST), payroll, cap room (over-cap in
   red; incomplete payroll flagged). Goalies listed with raw W / GAA / SV%
   and Yahoo rank (no TTLTST).
   - **Replace** (owner's team): tap a player to see free agents eligible at
     any of that player's positions, ranked by TTLTST, each with ΔTTLTST,
     cap room after the swap (`room_after`), and per-category deltas. Toggle:
     only swaps where `swap_ok` holds.
3. **League** — one row per team: mean norm per category, mean TTLTST,
   payroll and cap room (replaces the workbook's manager comparison,
   `Table10`). Tap a team to open it in Rosters.
4. **Matchup** — the owner's opponent for the current week and next week
   (week switch). Side-by-side team profiles per category with the
   difference; trailing categories (§5) highlighted. Goalies raw. A "Free
   agents who help here" link opens Players filtered to free agents and
   sorted by `need_score`, with a "fits my cap" filter.
5. **Admin** — league sheet status (last read, per-tab parse status incl.
   unrecognized tabs, tab ↔ Yahoo team binding, discrepancy report);
   PuckPedia CSV import for free agents; match review (unmatched row vs. top
   3 candidates, tap to bind); single-player AAV edit; force refresh; config.

Footer on every page: "Fantasy data provided by Yahoo Fantasy" linking to
Yahoo Fantasy (required attribution).

### App authentication

Single password from env var `APP_PASSWORD`, verified once, then a signed,
HttpOnly, Secure session cookie (long-lived). No user table.

## 8. Secrets and data hygiene

- Secrets live in `.env` locally and in Vercel environment variables in
  production: `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`, `APP_PASSWORD`,
  `SESSION_SECRET`, `FIRESTORE_PROJECT_ID`,
  `FIRESTORE_SERVICE_ACCOUNT_JSON` (the key's JSON content, not a file path —
  there is no persistent filesystem on Vercel), and `LEAGUE_SHEET_ID`.
- **Never** commit, print, log, or echo secret values. Never read `.env` into
  output. Tests never need real secrets.
- The owner's workbook, the league sheet (and its ID), and any salary CSVs
  stay out of git (`private/`, `*.xlsx`, `*.csv` are ignored). Only derived
  JSON fixtures are committed. League-sheet tests use **synthetic** sheets
  that reproduce each layout variant with made-up names — never real tabs.
  They are built in the test code (openpyxl for the `.xlsx` reader, JSON
  grids for the parser), so no `.xlsx` is ever committed.
  No manager names or contact details anywhere in the repo.
- Firestore rules: deny all client reads and writes. Server uses the service
  account.
- The repository is public. Assume everything committed is world-readable.

## 9. Testing (non-negotiable)

- **Tooling:** `pytest`, `pytest-cov`, `pytest-asyncio`, `hypothesis`,
  `respx` (httpx mocking), `mutmut`, `ruff`, `mypy --strict` on `src/`.
- **CI:** GitHub Actions on every push and PR: ruff, mypy, pytest with
  coverage. CI fails if any gate fails.
- **Coverage gates:** ≥ 95% line and branch on `domain/`; ≥ 90% on
  `sources/`, `storage/`, `services/`; ≥ 80% on `web/`. Enforce per package,
  not one global number.
- **No live network in tests.** Yahoo responses are recorded once, sanitized
  (no tokens, no personal identifiers beyond league/team names), and committed
  under `tests/fixtures/yahoo/`. A test that touches the network fails CI.
- **Golden tests** per §5.
- **Property tests** (`hypothesis`) for the engine: GP = 0 and below-floor
  players are excluded without error; input order does not change output;
  the top-10 players in a category have mean norm = 1 for that category;
  TTLTST is invariant to scaling every player's stats by the same factor.
- **Matcher tests:** every seed alias, plus accent, nickname, and
  duplicate-name cases.
- **Mutation testing:** `mutmut` on `domain/`; surviving mutants must be
  killed or justified in the PR. This is the guard against tests that execute
  code without asserting anything.
- **Web tests:** FastAPI `TestClient` for every route, including auth
  redirect, filters, and sort. Templates rendered and checked for key content.
- **Test-first for the engine and matcher.** Write the failing test, then the
  code.

## 10. Milestones

Each milestone ends with CI green and every acceptance criterion met. Do not
start the next milestone until the current one is accepted.

### M0 — Scaffolding
- `uv` project, Python 3.12, `src/` layout, ruff, mypy, pytest config.
- GitHub Actions CI running all gates (with a placeholder test).
- `.gitignore` extended: `private/`, `*.xlsx`.
- `docs/DECISIONS.md` created.
- **Accept:** fresh clone → `uv sync && uv run pytest` passes; CI green.

### M1 — Metric engine (no Yahoo)
- `scripts/extract_golden.py` → the three fixtures in §5.
- `domain/` models + engine per §5.
- **Accept:** both golden tests pass (exact parity within tolerance;
  divisor recompute reported in DECISIONS.md); property tests pass; `domain/`
  coverage ≥ 95%; `mutmut` survivors resolved.

### M2 — Yahoo source
- Client spike + decision (§4). `YahooSource` implementation.
- `scripts/yahoo_auth.py` consent flow (§4).
- Game-key resolution, stat-ID mapping from league settings, rosters (with
  slot, so IR / IR+ is known), player pool, current- and last-season stats,
  league scoreboard for the current and next week. Concurrent fetch with bounded concurrency; token refresh
  on 401.
- Record + sanitize real responses into fixtures (owner runs the recording
  once after consent).
- **Accept:** all Yahoo tests pass offline from fixtures; a mocked full
  refresh completes with calls issued concurrently (asserted); PPP question
  answered in DECISIONS.md; last-season stats and next week's opponent are
  retrieved from fixtures.

### M3 — Storage and salaries
- `Repository` protocol, `InMemoryRepository`, `FirestoreRepository`
  (integration-tested against the Firestore emulator), and a dev-only
  `LocalJsonRepository` (gitignored file) so the app can run locally against
  real data before any cloud setup. Never used in production.
- League sheet (§4a): pure parser, `.xlsx` grid reader, Google Sheets API
  grid reader (tested with mocked HTTP; used live from M5), tab ↔ team
  binding, team-scoped matching, discrepancy report.
- PuckPedia CSV import for free agents (idempotent), matcher cascade (§6),
  alias seeding, bindings persisted, single-player edit.
- Refresh service with TTL and injectable `Clock`.
- **Accept:** parser handles every layout variant seen in the real sheet
  (as synthetic fixtures) and reports an unresolvable tab as unrecognized;
  run locally against the owner's downloaded sheet, every tab parses and its
  payroll matches the sheet's own; matcher tests incl. all seed aliases
  pass; re-import is a no-op; TTL behaviour tested with a fake clock;
  coverage gates met.

### M4 — Web UI
- Players, Rosters, League, Matchup, Admin screens (§7), Categories and
  season toggles, password auth, attribution footer, PWA manifest + icons.
- Team profile, matchup, `need_score` and salary-cap functions in `domain/`
  (test-first).
- **Accept:** route tests pass; the owner runs the app **locally against real
  Yahoo data** (`LocalJsonRepository`), on laptop and on their phone over the
  local network at 390 px, and signs off; all gates green.

### M5 — Deploy
- Vercel project, env vars, Firestore project with deny-all rules, service
  account, production redirect URI added to the Yahoo app. Owner shares the
  league sheet view-only with the service account; `LEAGUE_SHEET_ID` set.
- Run consent in production; first real refresh.
- **Accept:** owner opens the app on their phone, logs in, sees their roster
  with TTLTST and AAV and next week's matchup; refresh completes within the
  time limit. **This is the Phase 1 target: a running app.**

### Later (context only — do not build)
- **Phase 2:** to be planned from the owner's use of the running Phase 1 app.
  Candidates so far: waiver recommendations (drop suggestions), trade
  evaluator, tonight's-games lineup helper and games-this-week counts (NHL
  schedule API), weekly acquisition counter, goalie-appearance tracker (the
  league requires 3 per week), export of the owner's roster in the league
  spreadsheet's format (the league requires it within 24 h of changes).
- **Phase 3:** history snapshots, schedule/rest-of-season weighting, goalie
  model, keeper-value view.

## 11. Open questions

1. ~~Keeper count and cost~~ — resolved: exactly 8, each paid full cap hit.
2. Whether Yahoo exposes PPP directly — resolved in M2.
3. Firestore vs. a free Postgres (e.g. Neon) — Firestore is the default;
   revisit only if the Repository implementation fights it.
4. PuckPedia CSV export columns — owner to supply the header row before M3.
5. `BASELINE_MIN_GP` (default 10) and whether IR / IR+ players should count
   in team profiles (default: no) — revisit after the owner tries the app.
6. Whether the league treats a Yahoo add/drop as simultaneous for cap
   purposes (`swap_ok`), or requires room for the add before the drop
   (then Replace should use `fits`). Owner to confirm.
7. Sheet layout drift between seasons: the parser keys off each tab's
   PAYROLL formula, so relabelled headers are fine; an unresolvable tab is
   flagged, not guessed. Revisit if GMs restructure tabs.
