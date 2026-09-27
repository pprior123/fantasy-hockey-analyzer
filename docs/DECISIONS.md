# Decisions

Record significant decisions here: date, decision, why, alternatives considered.

## 2026-09-26 — Hosting: Vercel (serverless) + Firestore
Free, nothing to keep running, phone-browser access from anywhere. Browser-only
was rejected: Yahoo's API blocks browser (CORS) calls and the OAuth client
secret can't ship to a browser. Rejected: Firebase Cloud Functions (requires
paid Blaze plan), Render free tier (cold-start sleep), Fly.io (no free tier for
new accounts), self-hosting on the Mac (must stay awake).

## 2026-09-26 — Salaries via CSV import behind a SalarySource interface
Cap hits change rarely (signings, extensions). PuckPedia's API is private and
paid; scraping it conflicts with its terms. The interface leaves room for a
live source later.

## 2026-09-26 — Language: Python
Mature Yahoo Fantasy wrappers exist for Python (yfpy, yahoofantasy); the
Yahoo API is awkward enough that this saves real time.

## 2026-09-26 — Test network guard: pytest-socket
*Superseded by "Test network policy hardened" below.*
All tests run with `--disable-socket --allow-unix-socket`, so any test that
opens an internet socket fails (CLAUDE.md hard rule 2) instead of relying on
review to catch it. Unix sockets stay allowed because asyncio's event loop
uses a socketpair. The Firestore-emulator integration tests (M3) will need a
targeted `--allow-hosts=127.0.0.1` opt-in. Alternative: respx's
`assert_all_mocked` only — rejected, it covers httpx but not other clients.

## 2026-09-26 — Per-package coverage gates via scripts/check_coverage.py
pytest-cov supports only a global `--cov-fail-under`. SPEC §9 requires
per-package gates on line *and* branch coverage, so a small script reads
`coverage.json` and checks each `fha` subpackage against
`[tool.fha.coverage-gates]` in pyproject.toml. Modules directly under `fha/`
are gated as `_root`. Packages with no measurable code are skipped. Code
cannot escape the gates: a package with code but no gate fails; a source
file with code that is missing from the report fails (coverage.py omits
unimported files in directories lacking `__init__.py`); an empty report
fails. Alternative: one `coverage report
--include=... --fail-under=N` call per package — rejected, it checks the
combined line+branch percentage and errors on packages with no data yet.

## 2026-09-26 — Build backend and dependency policy
`uv_build` backend, `src/` layout, Python pinned to 3.12 (`>=3.12,<3.13`).
Runtime dependencies are added in the milestone that needs them (none in M0),
and script-only tools such as `openpyxl` stay in the dev group so they never
reach the serverless bundle (cold starts, SPEC §2).

## 2026-09-26 — Domain purity is enforced by a test
*Superseded by "Domain purity is an allowlist" below.*
`tests/unit/test_architecture.py` parses every module under `fha/domain/`
and fails on imports (relative imports resolved) of I/O, network, clock
(`datetime`, `time`), randomness, `sys`/`logging`, or the outer layers, and
on calls to builtin `open`/`print`/`input`/`eval`/`exec`/`__import__`. Cheap, and it turns the "respect the seams" rule into a
gate.

## 2026-09-26 — Phase 1 target is a running app; scope additions
Phase 1 ends with the deployed app in daily use; Phase 2 is planned from that
use. Added to Phase 1 at the owner's request, as views over data already
fetched: Rosters for any team with team category profiles, a League
comparison (the workbook's `Table10`), a Matchup screen (current/next week
opponent, trailing categories, a free-agent shortcut sorted by need), a
Replace view (owner picks a player; free agents ranked with cap impact), a
Categories toggle for phone width, and a baseline-season rule so the app is
useful before and early in 2026-27. Waiver *recommendations*, trade
evaluation and schedule weighting stay out. M4 now ends with the owner
running the app locally against real data (dev-only `LocalJsonRepository`)
before any cloud setup. Alternative: keep the original three screens and
defer the rest — rejected by the owner; the extra views are cheap given the
data is already fetched.

## 2026-09-26 — Salaries and cap: league sheet is the source of truth
The league's shared Google Sheet is what the league enforces: per-team tabs
with each player's cap hit, the team's payroll, and the cap ($119.6M for
2026-27). The league constitution's cap formula ("NHL cap + 7.5%") is out of
date. So rostered salaries, payrolls and the cap come from the sheet; a
PuckPedia CSV supplies free-agent salaries only (the sheet lists rostered
players only). Tabs are hand-maintained with differing layouts, so the
parser keys off each tab's own PAYROLL formula (which identifies the salary
column and counted rows, excluding IR rows) rather than header labels, and
flags any tab it cannot resolve. Sheet rows are matched only within the bound
Yahoo team's roster, which resolves surname-only entries and typos. Read live
via the Sheets API with the Firestore service account in production; a
downloaded `.xlsx` in dev and as an Admin upload fallback. The sheet contains
managers' contact details: no cell outside the parsed ranges is persisted,
logged, rendered or committed (whole tabs must be fetched to find the
PAYROLL cell, so "never read" is not implementable);
tests use synthetic sheets. Alternatives: PuckPedia for everyone (rejected —
not what the league enforces, and diverges when GMs' entries differ); treat
the sheet as reference only (rejected by the owner); upload-only, no live
read (kept as fallback; rosters change weekly so it would go stale).

## 2026-09-26 — Test network policy hardened
pytest-socket's plugin leaves gaps: sockets are open until test setup (so
collection-time imports can connect), tests can opt out via the
`enable_socket` marker, the `socket_enabled` fixture or `--force-enable-socket`,
its loopback exception depends on test order, and it lifts restrictions
before fixture teardown. So the plugin is disabled (`-p no:socket`) and
`tests/conftest.py` + `tests/network_policy.py` own the lifecycle using
pytest-socket's functions: sockets blocked from `pytest_configure` and never
re-enabled, except during setup/call/teardown of a test marked
`allow_hosts(...)` with loopback-only hosts (reserved for the M3 Firestore
emulator), where `connect`, `sendto` and name resolution are restricted to
loopback. A non-loopback `allow_hosts` aborts the run. Relies on the private
`pytest_socket._remove_restrictions`; tests fail if that changes.

## 2026-09-26 — Domain purity is an allowlist; no coverage exclusions in domain
The purity test allows only listed stdlib modules (plus `rapidfuzz` and
`fha.domain`); anything else fails, so new I/O libraries can't slip in.
`datetime` is allowed for date types, but `.now()`/`.today()`/`.utcnow()`/
`fromtimestamp` references are banned (called or not, e.g. as a default
argument), as are I/O builtins and `__builtins__`. mutmut's injected
trampoline import is allowed only under `mutants/`. `check_coverage.py` fails
on excluded lines or any `# pragma` comment in `domain/`, where the 95% gate
and mutation testing apply.

## 2026-09-26 — Known network-policy gaps for M3 (review round 4, deferred)
These only matter once the Firestore-emulator tests exist, and the right fix
depends on how those tests are written, so they are M3 work, not M0:
1. In `loopback_only`, only `connect`, `sendto` and `getaddrinfo` are
   guarded; `connect_ex`, `sendmsg`, `gethostbyname(_ex)` and `getnameinfo`
   are not (a port-wait helper using `connect_ex` could reach a real host).
2. gRPC (used by google-cloud-firestore) opens sockets in C, bypassing the
   Python guard entirely. Scrub `FIRESTORE_EMULATOR_HOST`,
   `GOOGLE_APPLICATION_CREDENTIALS` and `FIRESTORE_SERVICE_ACCOUNT_JSON` for
   unmarked tests (autouse fixture); assert the emulator host is loopback
   for marked ones.
3. A session/module-scoped emulator fixture torn down during an unmarked
   test's teardown runs blocked and errors (loudly). Keep emulator fixtures
   function-scoped or in `pytestmark`-marked modules, or wrap their
   finalizers in `loopback_only()`.
4. Code that catches broad exceptions can swallow a `SocketBlockedError`, so
   a test that forgot to mock still passes (no packets leave). Record blocked
   attempts and fail the test even if the exception was caught.

## 2026-09-26 — M1: workbook parity findings ("parity wins", SPEC §5)
`scripts/extract_golden.py` checks every formula it relies on, then reads the
cached values. Where the workbook differs from SPEC §5's first draft, M1
follows the workbook and the spec was updated:
1. **Eligibility is strict:** `AD = IF(E > 0.02*MAX(E), …, 0)`, so GP must
   *exceed* 2% of max GP (was `>=`). Same result on this data (max 73, floor
   1.46: the 18 unrated players all have GP 1).
2. **Percentile's N counts unrated skaters too:**
   `(1 - rank / MAX(Table2[Ranking])) * 100`, and `Table2` has one row per
   player row (845), unrated players ranked last at 0. With N = eligible (827)
   percentiles would be off by up to 2.1, far outside the 0.1 tolerance.
   Every workbook row has GP >= 1, so the data can't tell "all skaters" from
   "skaters with GP > 0". The engine uses **GP > 0** (review round 1): the
   app's pool holds GP-0 players (IR, prospects, pre-season), and counting
   them would let their number move every percentile (400 of them would lift
   the lowest rated player from 2.1 to 33.6). Both readings give 845 here.
3. **Ties:** `Table2` ranks by position (`LARGE(AD, k)`), so a tie takes
   consecutive ranks and the name lookup repeats the first tied player. A
   score's first rank is the one every tied player shares, which is SPEC's
   competition ranking; kept. One tie in the data (2 players), covered by a
   test. The second tied player has no `Table2` row (the lookup repeats the
   first), so its golden percentile is the extractor's reading of the tie,
   not a displayed workbook value.
Kept from SPEC, not the workbook: unrated players have TTLTST/rank/
percentile None rather than the workbook's 0, since 0 would read as a real
(worst) rating.

Result, with divisors injected: all 827 rated players match TTLTST and
percentile (max difference: TTLTST 2.2e-16, percentile 0; the same with
divisors computed). Tolerances are
SPEC's (1e-3, 0.1).

## 2026-09-26 — M1: divisors use the workbook's formula (confirmed by the owner, see below)
SPEC assumed `W6:AC6` were static values gone stale, and described the
divisor as the mean of the top-10 per-82 rates. Both were wrong. The cells
are live array formulas:
`AVERAGE(LARGE(stat, {1..20})) / AVERAGE(LARGE(GP, {1..20})) * 82`, i.e. the
mean of the top-20 totals over the mean of the top-20 GP (ranked
independently). The `Y4` label "mean top ten" is wrong, like the row-2
labels. Recomputing with that formula reproduces all seven divisors exactly
(golden test 2). That test pins the stat half of the formula; the GP half
can't be told apart on this data (28 players share the max of 73 GP, so any
top-k up to 28 gives 73), so top-20 for GP is pinned by the extractor's
formula check and the unit tests. The report (`uv run python scripts/divisor_report.py`):

| Category | Workbook `W6:AC6` | Recomputed, workbook method (top 20) | Spec method (top-10 per-82) | Spec / workbook | Low-GP contributors: workbook / spec |
|---|---:|---:|---:|---:|---:|
| G | 41.8986 | 41.8986 | 47.1510 | 1.125 | 0 of 20 / 0 of 10 |
| A | 66.2740 | 66.2740 | 80.0930 | 1.209 | 0 of 20 / 1 of 10 |
| PPP | 35.8329 | 35.8329 | 45.5914 | 1.272 | 0 of 20 / 3 of 10 |
| PIM | 110.9247 | 110.9247 | 231.9700 | 2.091 | 0 of 20 / 6 of 10 |
| HIT | 257.2329 | 257.2329 | 342.1082 | 1.330 | 0 of 20 / 0 of 10 |
| SOG | 267.4548 | 267.4548 | 304.5128 | 1.139 | 0 of 20 / 0 of 10 |
| BLK | 166.1904 | 166.1904 | 187.1714 | 1.126 | 0 of 20 / 0 of 10 |

Low GP = below 20% of the pool's max GP (73). Switching to the spec method moves a rated player 27.0 ranks on average; 47 of the workbook's top 50 stay in the top 50.

The top-10 per-82 method is badly skewed by small samples: 6 of its 10 PIM
contributors (and 3 of 10 for PPP) have under 20% of max GP, e.g. a 3-GP
player with 11 PIM rates 301 per 82, and the league leader (144 PIM in 71 GP,
166 per 82) ranks ninth. The workbook's method has no low-GP contributors in
any category: a few games rarely add up to a top-20 total. So the engine defaults to the workbook method (`DivisorMethod.WORKBOOK`,
top 20), which also keeps Phase 1's goal of parity with the spreadsheet. The
per-82 method stays available (`DivisorMethod.TOP_PER82`, whose top-N
defaults to 10), so either choice is one config value. With the workbook method a separate, higher GP
floor for divisors isn't needed. **Owner decision (SPEC §11.5):** confirm
the workbook method, or choose per-82 (and then decide on a divisor floor).
This goes beyond the "parity wins" rule (percentile, ties, eligibility), so
SPEC marks it provisional until the owner confirms at the M1 boundary.
Alternatives: follow SPEC's top-10 per-82 as written (rejected pending the
owner: its premise, stale statics, was false, and it changes ratings by 27
ranks on average); a separate divisor floor now (rejected: an owner
decision, and unneeded with the workbook method).

## 2026-09-26 — M1: golden fixtures and workbook quirks
- `golden_players.json` keys players by the stats site's player ID (`Stats
  Data Source!A`), not a Yahoo ID. It's fixture identity only; M2/M3
  bind real players by Yahoo `player_id`.
- Each `Fantasy Analysis` row's source row is read from its own formula, not
  assumed. The workbook rates 845 of the 899 scraped players: its last row
  pulls source row 900 and skips 846–899 (54 low-GP players), likely after the
  source table grew. The golden pool is the 845 it rates; the app rates its
  own pool, so the quirk doesn't carry over.
- AAV is the workbook's `S` column (null where `#N/A`, 152 players). It
  looks up cap hits by name, so the defenceman Elias Pettersson carries the
  forward's $11.6M. Kept as-is, since it's golden data; the app matches by
  player ID (SPEC §6).
- openpyxl (+ `types-openpyxl`) is dev-only, per the dependency policy.
- The domain allowlist needed no additions (`math`, `dataclasses`, `enum`,
  `collections.abc`).

## 2026-09-26 — Divisor method confirmed; rating settings are owner-editable
The owner confirmed the workbook's divisor method (top-20 totals over top-20
GP, x82) as the default, and asked for it to be adjustable: the divisor
method, the divisor count (default 20) and the GP floor fraction (default 2%)
become settings in the Admin screen. The engine already takes all three as
`EngineConfig` (M1), so the work is persistence in the Repository's config
(M3) and the settings form (M4). A setting change re-rates every player on
the next view; the cache stores raw stats, not ratings, so no refresh is
needed. Switching method pre-fills that method's usual count (20 or 10)
unless the owner typed one. Alternatives: fixed in code, or env vars
(rejected: the owner wants to change it from the app; either would need a
redeploy).

## 2026-09-26 — M2: Yahoo client is a thin async httpx client (spike, SPEC §4)
Spike (well under the hour): read the source of yfpy 17.0.0 and yahoofantasy
1.4.9, the current releases.
- **yfpy** does take an externally supplied token (`yahoo_access_token_json`,
  with `store_file=False` on the underlying `yahoo-oauth`), but it has no
  refresh callback: a refreshed token is only held on the query object and
  must be read back out. It is synchronous (`requests`), so the bounded
  concurrent fetch SPEC §2 requires would need a thread pool. It also falls back to
  `YAHOO_*` environment variables for any missing token field, calls
  `sys.exit(1)` on a malformed token, sleeps between retries, and on a 401
  re-authenticates but still parses the failed response. It pins `requests`
  and `python-dotenv` exactly and pulls in `yahoo-oauth` → `rauth`, `myql`,
  `pyaml`: heavy for a cold start.
- **yahoofantasy** accepts client ID, secret and refresh token directly, but
  every fetch goes through a pickle cache written to `<key>.yahoofantasy` in
  the working directory (the read-only filesystem on Vercel), it parses XML
  (`xmljson`, `pydash`), is synchronous, and has no refresh callback either.

Neither passes, so the app has its own client (`fha.sources.yahoo`): `httpx`
(the only new runtime dependency; async, and respx already mocks it),
`?format=json`, an `asyncio.Semaphore` bounding concurrency (default 8), the
token behind a `TokenStore` protocol, which is loaded once and saved after every
refresh (Yahoo may rotate the refresh token), and a single-flight refresh on 401 (concurrent
401s trigger one refresh, then each request retries once). What the libraries
would have saved (response parsing) is a few hundred lines and has to be
tested against recorded fixtures anyway. Alternatives: wrap yfpy in a thread
pool and read the token back after each call (rejected: sync, heavy deps,
`sys.exit`/env fallbacks in library code); fork yahoofantasy's context
(rejected: XML and file persistence are its core).

## 2026-09-26 — M2: how the Yahoo source reads the league
A full refresh (`HttpYahooSource.fetch_snapshot`) runs in four dependent
stages; each stage's requests go out together, at most 8 in flight:
1. `games;game_codes=nhl` → the current game (the latest season listed; this
   works whether Yahoo lists one game or every season's). League key =
   `{game_key}.l.8076`.
2. `league/{key}/settings` + `game/{game_key}/stat_categories`.
3. At once: `league/{key}/teams/roster` (every team with today's slots,
   one call), every available player (`players;status=A;sort=AR`, pages of
   25, in waves of 8 until a short page, stopping with an error past 5,000),
   and the scoreboards for the current week and the next (none after the
   last week).
4. Season stats by player key, at the game level
   (`players;player_keys=.../stats;type=season`), current season and last
   season (`;season={season-1}`, same player keys). Each page's 25 players,
   and the rostered players in batches of 25, are requested as soon as they
   are known, so paging and stats share the 8 slots instead of taking turns.

Stats are fetched separately from the pages to keep GP in hand: a
league-scoped stats request returns the league's categories, and GP isn't
one. With ~1,500 players listed that is roughly 60 pages + 2 × 70 stats
calls; the recording measures the real time against the 8 s target. If it is
too slow: cache last season's stats (they never change), fold stats into the
pages if the probe shows league-scoped stats carry GP, or raise concurrency.

- **Pool (M1 round-2 follow-up; owner's decision):** every rostered player
  on every team, in any slot (IR, IR+ and NA included), then every available
  player Yahoo lists, minus any already rostered. SPEC first said "the top
  ~300 available". On the 2025-26 golden data, cutting the 845-player pool to
  516 (216 + 300) left the divisors almost unchanged (PIM -2.9% and HIT -1.2%
  at most, depending on the stand-in for Yahoo's rank) but dropped rated
  players' percentiles by 13-20 points on average (up to 39): rank 250 went
  from the 70th percentile to the 52nd, rank 400 from the 53rd to the 22nd. The owner chose
  the whole league, as in the workbook ("try every player for now and see how
  it goes"). GP-0 players are harmless: they count in neither N nor the
  divisors. `HttpYahooSource(available=n)` still caps the count if the
  refresh proves too slow.
- **Stat map:** by display name, league settings first, then the game's
  stat list (GP; PPG and PPA if the league lacked PPP). PPP is Yahoo's own
  stat when present. The answer for this league is recorded below after the
  fixtures.
- **Strictness:** a settings response for another league or season, a
  scoreboard for another week, stats labelled with another season, or a pool
  player missing from the stats all fail the refresh loudly rather than
  producing plausible-looking wrong data.
- **Token:** `TokenStore` protocol (load/save). The Repository implements it
  in M3; until then the owner-run scripts use `private/yahoo_token.json`
  (mode 0600, atomic replace). The whole token is stored (access token and expiry too) so
  serverless invocations don't refresh on every cold start.
- **Recording:** `scripts/record_yahoo.py` records only the Fantasy API
  host (never the token endpoint), sanitizes, re-checks for manager keys,
  tokens and email addresses, and writes nothing if any check fails.
  Fixtures are compact JSON (the whole-league pool makes them several MB). A few
  extra probe requests (league-scoped stats, last season by last season's
  game key, preseason rank) go to gitignored `private/yahoo_probes/` to
  confirm the choices above against real responses.

Assumptions the recording confirms or corrects: that `;season=` returns
last season's totals for current player keys (the parser refuses stats
labelled with another season, or with no season label), that the game-level
stat list includes a skater "GP", and how long a whole-league refresh takes.
Also to check against the real responses (review round 2): whether Yahoo
returns a last-season line for players with no NHL games last season
(rookies); if it leaves them out, the "pool player missing from the stats"
rule fails every refresh and must become "no line = no games" for last
season only; and whether a whole-league refresh sees 999 (rate limit) or
5xx answers, which currently fail the refresh (no retry).

## 2026-09-26 — Yahoo API access: pending approval (M2 blocker)
Consent and token exchange work, but every Fantasy API endpoint, public ones
included, answers 403 "This application is not authorized to perform this
action" (`scripts/yahoo_diagnose.py`). Since 2026-07-22 Yahoo refuses apps
that have only the Yahoo Developer Network "Fantasy Sports: Read" permission;
access now needs an approved application at sports.yahoo.com/developer,
then a signed API Access and Use Agreement and the app's Client ID submitted
on the confirmation page, which Yahoo provisions
([yfpy #84](https://github.com/uberfastman/yfpy/issues/84)). The owner applied
in late August 2026 without a Client ID (the app, `OQP5c1XE`, was created
later), and the confirmation step hasn't happened, so no Client ID is
provisioned. SPEC §1 said "approved"; corrected. Yahoo's only reply was an
automated acknowledgement on 2026-09-01 ("review typically takes 1-2 weeks"),
sent to the email on the application, from a no-reply address. Yahoo lists
no other contact. On 2026-09-26 the owner submitted the confirmation page
(sports.yahoo.com/developer/application-confirmation/) with App ID
`OQP5c1XE` and its Client ID in the notes, but from a different email address
than the application's. The owner checked the Client ID and App ID against
the developer site (both match) and resubmitted the confirmation the same day
from the application's email (the address Yahoo's acknowledgement went to),
noting the earlier mix-up. Next: run
`scripts/yahoo_diagnose.py` periodically; a 200 means access is on and the
owner runs `scripts/record_yahoo.py`. If nothing happens in a week or two,
re-apply at sports.yahoo.com/developer/access with the Client ID filled in. Until then M2's real recording, the PPP answer from real data and
the real refresh timing are blocked; the code is tested against synthetic
Yahoo-shaped responses. If Yahoo refuses, the fallback (NHL public stats API
+ the league sheet for rosters) is an owner decision and a redesign.

## 2026-09-26 — M3: stacked on M2 while Yahoo access is pending
M3 starts on `m3-storage-salaries`, branched from `m2-yahoo-source` rather
than `main`, because M2 can't merge until the real Yahoo fixtures exist. This
bends the milestone gate (SPEC §10), with the owner's agreement: M2's code
passed two review rounds (no medium-or-worse findings; PR #4), and waiting
for Yahoo's approval would stall everything. M3's Repository implements M2's
`TokenStore`. If the real fixtures force changes in M2, M3 is rebased onto
the result. Alternative: wait for Yahoo (rejected: no end date).

## 2026-09-26 — M3: free-agent salary CSV format (answers SPEC §11 Q4)
The owner copies PuckPedia's salary tables (skaters and goalies) into a CSV
under `private/`, and adds one header row. PuckPedia's copied table has no
header, no NHL team column (only the team's GM) and 25 columns of contract
and stat detail, saved by the owner's spreadsheet app in Mac Roman with
non-breaking spaces in names. The owner is "not married to the format", so:
- Columns are read **by header name**, case-insensitive and trimmed.
  Required: `Player` ("Last, First" or "First Last"), `Pos` (C, L, R, D or G)
  and `Cap Hit` (e.g. `$18,000,000`). Optional: `Team`. Every other column is
  ignored, so its header may be anything or blank.
- A missing required header, or a malformed value in a required column,
  rejects the whole file, naming the row. Nothing is guessed.
- Encoding: UTF-8 (with or without a BOM), else Mac Roman. Non-breaking
  spaces become spaces.
- Goalies are imported too. They aren't rated in Phase 1, but a free-agent
  goalie's cap hit matters to `fits` / `swap_ok` (SPEC §5). Rostered goalies
  are already in each tab's PAYROLL.
- The GM column is never used to infer a team (GMs change jobs). With no
  team, the SPEC §6 cascade skips steps 1-2, and **position group breaks
  ties** in steps 3-4: forward (C, L, R, LW, RW, W, F), D or G. For example,
  the two Sebastian Ahos (a C and a D) resolve separately. A remaining tie is
  a candidate for review. The group is used rather than the exact position
  because sources disagree on C versus wing. (Owner approved, 2026-09-26.)
- Test CSVs are synthetic, in the same shape, and built in test code (SPEC
  §8: no `.csv` is committed). The real CSV is never committed (hard rule 3).
Alternatives: parse PuckPedia's headerless paste by column position
(rejected: silent breakage when PuckPedia changes its table); require a
clean three-column file (rejected: more work for the owner on every import).

## 2026-09-26 — M3: Firestore and Google Sheets over REST (httpx), not client libraries
`FirestoreRepository` talks to the Firestore REST API, and the Sheets grid
reader to the Sheets REST API. Both use httpx, which is already the Yahoo
client's only runtime dependency, and a service-account access token from
`google-auth`. The emulator speaks the same REST API, and tests use it
without credentials. Reasons:
- Cold start: `google-cloud-firestore` imports gRPC and protobuf on every
  cold start (SPEC §2: keep imports light).
- The test network guard: gRPC opens sockets in C, bypassing the guard
  ("Known network-policy gaps for M3", item 2). httpx goes through Python
  sockets, which the guard sees.
- One HTTP stack, mocked the same way (respx / MockTransport), for Yahoo,
  Firestore and Sheets.
The cost is our own value encoding (Firestore's typed JSON values) and a few
hundred lines. Alternative: `google-cloud-firestore` (rejected for the
reasons above; revisit if the REST layer becomes a burden). Owner's choice,
2026-09-26.

## 2026-09-26 — M3: league sheet layouts seen in the real sheet (SPEC §4a)
Structure of the owner's downloaded sheet (2025-26 contents), read with a
script that printed only formulas, header rows and the shape of each
column's values inside the payroll ranges. No cell values outside them were
read, and none are recorded here or in fixtures. There are 8 team tabs, one
summary tab (the cap is at `B3`), and two other tabs that aren't team tabs
(no PAYROLL).
- `PAYROLL` label at `A3`, formula at `C3`. It is either `SUM(F7:F33)`
  directly, or one reference away: `=SUM(C36)` or `=C40`, where that cell
  holds the `SUM`. Function names come in either case (`sum`, `SUM`).
- `CAP:` at `A2`, `C2` = `='<summary tab>'!B3`.
- The salary column is F on seven tabs and G on one, and the range rows
  differ on each tab. The header row sits just above the range (rows 5-7),
  **or is the range's first row** (one tab: "NAME / Position / NHL Team /
  2025-2026 Salary" inside the SUM range, which ignores the text). Header
  labels vary: `NAME`; `Position` or `Pos.`; `NHL Team` or `Team`; the salary
  header is a season label ("25-26", "2026-2027", "2025-2026 Cap Hit",
  "25- 26 Salary"). The position column is D or E. Some tabs have extra
  seasons' salary columns beside the counted one, which are ignored.
- Names are "First Last" on most tabs and "Last, First" on one. Teams are
  codes on most tabs and city names on one. Positions are codes, words or
  combinations.
- There are blank rows inside the range. A few salary cells hold text
  (`???`, a single space): no known salary, which is shown and flagged
  rather than read as 0. One salary is `0`, read as 0.
- `IR` / `IR+` labels are in column A of the rows after the range, some of
  them empty slots (label only). Their salary may be in the salary column.
- Some tabs have unlabelled player rows below the range (not counted, not
  IR): they are ignored, and a player in them who is on the Yahoo roster
  shows up as "missing from the tab" in the discrepancy report.
- Below all of that, each tab has a contact block (a header row, then GM
  details). Below the range, the parser looks only at column A, for the
  exact labels `IR` / `IR+`; only those rows are read. No cell of any other
  row enters a parsed result, an error message or a log.
The synthetic parser fixtures reproduce each of these variants.

## 2026-09-26 — M3: the network-policy gaps are closed
The four gaps recorded before M3 ("Known network-policy gaps for M3"):
1. **Unguarded calls:** closed. In the loopback window, `connect_ex` and
   `sendmsg` are guarded along with `connect` and `sendto`. All five
   name-resolution functions (`getaddrinfo`, `gethostbyname`,
   `gethostbyname_ex`, `gethostbyaddr` (and so `getfqdn`), `getnameinfo`)
   allow only loopback, both in that window and while blocked.
   pytest-socket guarded only two of them while blocked.
2. **gRPC:** moot, since Firestore goes over httpx (see "Firestore and Google
   Sheets over REST"). The environment is scrubbed anyway: an autouse fixture
   removes the Firestore, Google-credential, sheet and Yahoo variables for
   unmarked tests. Marked (emulator) tests keep `FIRESTORE_EMULATOR_HOST`,
   which must be loopback or the test fails, but lose every credential.
3. **Fixture teardown:** avoided by design. The emulator runs outside pytest
   (CI and local runs start it with `firebase emulators:exec`), so no fixture
   starts or stops it. Emulator-backed repositories are function-scoped.
4. **Swallowed errors:** closed. Every blocked attempt is recorded, and the
   test phase that made it fails even if the code caught the exception. The
   guard's own tests claim their attempts with `expect_blocked()`. A
   pytester run proves that a broad `except` still fails its test.

## 2026-09-26 — M3: the Repository is a small document store
`Repository` (SPEC §3) has five operations: `get`, `put`, `delete`, `all`,
and an atomic `replace_all` of one collection. Typed records (stats cache,
salaries, bindings, aliases, config) are layered over it in `fha.storage`,
so each backend (in-memory, local JSON file, Firestore) is small, and a
shared contract suite (`tests/unit/storage/contract.py`) runs against all of
them.

Firestore's limits are checked in every backend (`check_document`,
`check_id`): 1 MiB per document, measured as Firestore measures it (a
number is 8 bytes, a string its UTF-8 bytes + 1, and so on: `firestore_size`;
900 KB kept as headroom), 500 writes per commit, the ID rules, reserved
field names, nesting at most 20 deep, 64-bit integers, no list directly
inside a list, and finite numbers. So a test against the in-memory backend
refuses what production would (checked against the emulator). Types round-trip exactly (bool is not int, and 1.0 stays
a float).

Anything bigger than a document (the whole-league stats cache, the free-agent
salary list) is split into chunk documents and written with one
`replace_all`, so a reader never sees half of a refresh. The Yahoo token is
a document in the `secrets` collection (`RepositoryTokenStore` implements
M2's `TokenStore`); Firestore rules deny every client (SPEC §8).

Alternative: a typed method per record on the protocol (rejected: three
backends × about ten methods, each needing the same encoding).

## 2026-09-26 — M3: refresh behaviour
- **The stats cache** is the raw `LeagueSnapshot` in the `stats_cache`
  collection: a meta document plus chunks for the available players and
  both seasons' stat lines, written with one `replace_all`. Ratings are
  never cached, so a settings change needs no refresh.
- **Fresh means** `0 <= now - fetched_at < TTL`, with a default TTL of 30
  min. A timestamp from the future counts as stale (clock skew between
  serverless instances shouldn't pin a cache forever).
- **Concurrency:** one refresh at a time per process. A request that waited
  behind another's refresh uses its result, even a forced one. Instances
  can still refresh concurrently; with one user that costs at most a
  duplicate Yahoo read.
- **Failures:**
  - A failed refresh with a cache serves the stale snapshot with the error
    attached, so the UI can say "Yahoo unavailable, showing data from HH:MM".
    Without a cache the error propagates.
  - A damaged or old-format cache is refetched. A backend failure (Firestore
    down) is an error, not "no cache".
- **Last season's stats** are refetched on every refresh for now. Caching
  them (they never change) waits for the M2 recording's real timing; see
  "how the Yahoo source reads the league".
Alternatives: fail the request when Yahoo fails (rejected: an outage would
blank the app although the data is at most hours old); cache ratings
(rejected: SPEC §10 M3).

## 2026-09-26 — M3: the storage backends (Firestore REST, dev file, factory)

**FirestoreRepository** (`fha.storage.firestore`) talks to Firestore's REST
API v1 with httpx.
- **Typed values:** encoded exactly (`nullValue`, `booleanValue`,
  `integerValue` as a string, `doubleValue`, `stringValue`, `arrayValue`,
  `mapValue`). A bool is never an int, an int never comes back as a float,
  and a whole double is decoded to a float even when Firestore sends it as
  a JSON integer. Kinds the app never writes (timestamps, bytes, references,
  geo points) and non-finite doubles are refused. The error names the kind,
  never the content.
- **By-ID operations name the document in the request body:** `get` is a
  one-document `:batchGet`, and `put` and `delete` are one-write `:commit`s.
  The emulator answered a legal 1,500-byte ID in a URL with 404 (its
  percent-encoded path is about 4.5 KB), and body names need no path
  quoting. A `put` is an update write without a mask, so it replaces the
  whole document, as the contract requires (verified on the emulator).
- **`replace_all`:** lists the collection, then sends one atomic `:commit`
  of updates for the given documents and deletes for the rest. Its limits
  are checked before sending:
  - updates plus deletes at most `MAX_BATCH` (500);
  - the request at most 10 MiB, Firestore's limit per request.
  So a replace can be refused although the other backends would accept it
  (e.g. 300 new IDs replacing 300 old ones is 600 writes). The typed layer
  must keep chunk IDs stable (`chunk-0..n`) and chunked collections well
  under 10 MiB in total. Note: Firestore's documentation has dropped the
  500-write limit for batches in places. 500 is kept anyway, since it's
  shared with the other backends and harmless at this app's sizes.
- **Race:** a document created by another writer between the list and the
  commit survives the replace. Accepted: each collection has one writer at a
  time (the refresh, or an Admin import), and the next replace removes it. A
  read-write transaction (`beginTransaction`, a transactional list, then
  commit) would close the gap. It was rejected because it takes more
  requests per refresh and brings emulator lock semantics into the tests,
  for a race the app doesn't have.
- **Errors:** `RepositoryError("Firestore <action> <collection/id>: HTTP
  <status> (<Google status>: <message>)")`, or `"Firestore <action>
  failed: <ExceptionType>"` for transport errors. The token is never in a
  message.

**Tokens** (`fha.sources.google_auth`):
- `ServiceAccountTokens(key_json, scopes, http)` is an async callable
  returning a bearer token. It is shared with the Sheets reader (stream B),
  which is why it lives in `sources`.
- It signs the JWT with google-auth's `RSASigner` and `jwt.encode`, imported
  lazily at the first token, and exchanges it at the key's `token_uri` with
  httpx. That makes google-auth plus `cryptography` the new runtime
  dependencies, and `requests` isn't needed.
- The token is cached until 60 s before expiry, and a refresh is
  single-flight.
- No message carries the key, the assertion or the token.
- `emulator_token()` returns the emulators' documented fake, `owner`.
- Alternatives:
  - google-auth's `service_account.Credentials.refresh`, rejected because
    it needs a `requests` or `aiohttp` transport;
  - hand-rolled RS256 with `cryptography`, rejected because google-auth
    already does it correctly.

**LocalJsonRepository** (`fha.storage.local_json`), dev only:
- One JSON file, read and rewritten whole on each operation.
- Each write creates a fresh 0600 temp file (`O_EXCL`) and `os.replace`s the
  file, as the token file does. A failed write leaves the previous file.
- It refuses to construct when `VERCEL` is set.

**`repository_from_env(environ, http)`** (`fha.storage.factory`), checked in
this order:
1. `FIRESTORE_EMULATOR_HOST`, which must be a loopback `host:port`, since
   the emulator's fake token must not leave the machine. The project is
   `FIRESTORE_PROJECT_ID` or `demo-fha`; `demo-` projects never reach real
   Google services.
2. `FIRESTORE_PROJECT_ID` + `FIRESTORE_SERVICE_ACCOUNT_JSON`: production.
   Either one alone is an error naming the other.
3. `FHA_LOCAL_REPOSITORY=<path>`: the dev file.
4. Otherwise an error listing the variables.

Errors name variables, never values. The caller owns the httpx client. The
Firestore and file modules are imported only by the branch that needs them.

**Emulator tests** (`tests/unit/storage/test_firestore_emulator.py`):
- **What runs:** the shared contract, plus a raw check that ints are
  `integerValue`s and a 450-document listing that crosses page boundaries.
- **Isolation:** function-scoped. Each test first clears the database with
  the emulator's `DELETE /emulator/v1/projects/{p}/databases/(default)/documents`.
  A unique project per test isn't possible with `singleProjectMode`.
- **Skipping:** they skip without `FIRESTORE_EMULATOR_HOST`, but under
  `FHA_REQUIRE_EMULATOR=1`, as in CI, that is a failure instead.
- **Config:** `firebase.json` pins the emulator to 127.0.0.1:8181 with the
  UI off.
- **CI:** a second job, `firestore-emulator`, runs setup-java 6.0.1
  (Temurin 21) and setup-node 7.0.0, then `firebase-tools@15.31.0
  emulators:exec`.

## 2026-09-26 — M3: how the league-sheet parser reads a tab (SPEC §4a)
`fha.sources.league_sheet` has four parts:
- a grid model (`Grid` / `Tab` / `Cell`: each cell's value and formula);
- two readers, `.xlsx` (`read_xlsx`) and the Google Sheets API
  (`read_sheets_api`);
- one pure parser, `parse_sheet(grid) -> ParsedSheet`;
- `LeagueSheetSource`, implemented by `XlsxLeagueSheet`,
  `SheetsApiLeagueSheet` and `FakeLeagueSheet`.

Rules beyond SPEC §4a, and why:
- **Labels:** `PAYROLL` and `CAP` are found in a tab's first 10 rows, matched
  case-insensitively with a trailing `:` or `.` ignored. The formula is the
  first non-empty cell within 3 columns to the right of the label (`C3`
  beside `A3` on every real tab). A tab with no PAYROLL label is not a team
  tab: it is listed in `other_tabs` (the summary tab and the notes tabs).
- **Formula:** only `=SUM(X:Y)` over one column, reached directly or through
  at most two references (`=SUM(C36)`, `=C40`). References may be absolute
  (`$F$7`), in either letter case, with any spacing, and reversed ranges are
  allowed. Anything else leaves the tab *unrecognized*, with a reason:
  - several ranges;
  - a range over more than one column;
  - arithmetic;
  - a cross-tab reference;
  - a reference to a cell with no formula;
  - more than two references;
  - no formula beside the label;
  - no computed value, as in an `.xlsx` that was never recalculated.
  
  An unrecognized tab has payroll `None` (SPEC §5: its cap room is
  "unavailable") but keeps its CAP value.
- **Header row:** the range's first row is checked first, then up to three
  rows above it. The first row with a name header (`NAME`, `Names`, `Player`,
  `Players`, `Player name`) is the header. The position (`Position`, `Pos`,
  `Pos.`) and team (`NHL Team`, `Team`, `NHL`) columns come from the same
  row. They are optional, and read as "" when missing: matching works
  without them (SPEC §6). The name header is required: without it the tab
  is unrecognized.
- **Rows in the range:** a row with no name and a blank salary is skipped.
  A salary that isn't a number (`???`, text) reads as `None`, which is what
  `SUM` ignores, so the counted total still equals the tab's PAYROLL. A row
  with a salary but no name is kept, so the owner can see it. Salaries are
  rounded to whole dollars.
- **IR rows:** only rows below the range whose column A is exactly `IR` or
  `IR+`, trimmed but case-sensitive. A label with no name is an empty slot
  and is skipped. In every other row below the range, only column A is
  looked at. So the contact block, whose name cells sit in the same columns
  as players', is never read into a result.
- **Cap:** the summary cap is the cell referenced by most team tabs' `CAP`
  formulas (`='<tab>'!B3`). A tab whose CAP value differs is listed by
  `ParsedSheet.cap_mismatches`.
- **Hygiene:**
  - A `Tab`'s repr shows only its title and cell count, so a grid logged by
    accident leaks nothing.
  - Error reasons name cell addresses and the PAYROLL formula, never another
    cell's contents.
  - The sources hold only the parsed result. Their reprs hide the sheet ID
    and the file path.
  - Tests plant fake contact details on every synthetic tab, and on the
    summary tab, and assert they appear in no result, repr or reason.
- **Readers:**
  - `.xlsx`:
    - The workbook is opened twice, once for formulas and once for cached
      values.
    - openpyxl is now a runtime dependency, imported only inside
      `read_xlsx`. A subprocess test checks that importing the sources
      doesn't load it.
    - An unreadable file is a `LeagueSheetError`.
  - Sheets API:
    - A single `spreadsheets.get` with `includeGridData=true` and a `fields`
      mask for titles, `userEnteredValue` (formulas) and `effectiveValue`.
    - The token comes from an injected async callable. Stream A's
      service-account provider plugs in here, with scope
      `sheets_api.SCOPE`.
    - Values of the wrong JSON type are refused, naming the cell address.
    - HTTP and transport errors name the status or exception type only. They
      are raised `from None`, because httpx messages can carry the URL, and
      the URL holds the sheet ID (SPEC §8).

Checked against the owner's downloaded sheet (2025-26 contents) with
`scripts/check_league_sheet.py`, which prints only statuses, counts, sums and
row numbers. All 8 team tabs parse, each tab's PAYROLL equals the sum of its
parsed salaries exactly, and every CAP matches the summary cap. The summary
tab and two notes tabs are recognized as non-team tabs. Two findings are
flagged for the owner:
- One tab has a counted row whose salary is `???` (reads as None).
- Two IR rows on another tab have no salary; that's fine, since IR salaries
  aren't counted.

Alternatives considered:
- Guessing the header by column letters (rejected: the columns move between
  tabs).
- Treating any row below the range with a name as IR (rejected: unlabelled
  rows below are not IR, and the contact block would be read).
- Reading only the payroll range from the Sheets API (rejected: the range is
  known only after the PAYROLL formula is read, and two requests cost more
  than one).

## 2026-09-26 — M3: name normalization and nicknames (SPEC §6)
`fha.domain.names.normalize_name` is how every source's names are compared:
- accents are stripped (NFKD minus combining marks), and a small table covers
  the letters NFKD leaves alone (ø, æ, œ, ß, ł, đ, ð, þ, ı);
- the name is casefolded;
- apostrophes and periods are dropped (O'Reilly → oreilly, J.T. and J.T →
  jt); any other mark, hyphens included, splits words
  (Haman-Aktell = Haman Aktell);
- whitespace, non-breaking spaces included, is collapsed;
- "Last, First" is turned into "first last" (split at the first comma).

The fold runs twice: one pass can expose more work (Ǣ → ǣ → æ → ae), and two
settle for every Unicode code point (checked exhaustively once; a
hypothesis test and two pinned cases guard it).

Nicknames are **groups of equivalent first names**, and a name may sit in
several (Cal is Callan or Calvin). `name_keys` gives one key per group, and
two names match if any key is shared. The groups come from the workbook's
57 aliases, plus the short forms SPEC §6 lists and a few of the commonest
(Sam, Ben, Dan, Chris, Pat). The table is deliberately small: anything else
goes to an alias or to fuzzy review. With the groups, 53 of the 57 seed
aliases match without the alias table. The other four need it:
- J.J. Moser (Janis);
- Zuccarello-Aasen;
- Martinsen-Lilleberg;
- the workbook's "Vornkov" typo.

Alternatives: one canonical first name per nickname (rejected: Cal can't be
both Callan and Calvin); a large public nickname list (rejected: more false
equivalences, with every match auto-bound at steps 1-3).

## 2026-09-26 — M3: canonical NHL team codes
Canonical codes are the NHL's own three-letter codes, which PuckPedia uses
(TBL, NJD, SJS, LAK, …). Each team also maps from:
- its city and nickname, and both together;
- Yahoo's abbreviations (TB, NJ, SJ, LA, and lowercase forms like Edm,
  Mon, Was, Nsh, StL);
- a few common short forms (Habs, Leafs, Preds, Isles, Caps, Avs, Canes,
  Sens, Pens, Bolts, Philly).

Matching is case- and punctuation-insensitive (St. Louis = St Louis = STL).
A shared spelling is an error when the table is built (tested). Unknown
strings map to None, never to a guess. On purpose:
- **"New York" / "NY" are None:** Rangers or Islanders is ambiguous.
- **Arizona / ARI are None:** Utah is a new franchise, not a renamed one.
  The 2026-27 data shouldn't contain Arizona; if it does, it shows up as an
  unknown team in Admin.
- **Utah** is `UTA`, from "Utah", "Utah Mammoth", "Utah Hockey Club" and
  "Utah HC".
- **`CLS` is Columbus:** SPEC §6 calls it a sheet typo. No other team's
  name or code gives CLS, so reading it as the Blue Jackets (CBJ) can't
  collide. `CLB`, NHL.com's old code, is included too. Other one-letter-off
  typos are not added: they are guesses.
- `CAL` is Calgary, `FLO` Florida, `WIN` Winnipeg, `VEG` / "Las Vegas" Vegas,
  `ANH` Anaheim, `NAS` Nashville: unambiguous legacy or alternate codes.

## 2026-09-26 — M3: position groups
`position_group` gives F, D or G:
- **F:** C, L, R, LW, RW, W, F, Center/Centre, Wing/Winger, Forward,
  "Left Wing" / "Right Wing", and combinations like "C/LW" or "LW,RW";
- **D:** D, LD, RD, Defense/Defence, Defenseman/Defenceman;
- **G:** G, Goalie, Goaltender, Goalkeeper.

A string mixing groups ("C/D", "F,G") is None. So is an unknown token or a
roster slot ("BN", "IR", "Util"). A None group never breaks a tie.

## 2026-09-26 — M3: the matcher cascade (SPEC §6 as built)
`fha.domain.matcher.match(query, candidates, aliases=..., scope=...)`:
0. **Aliases first:** a row whose normalized name has an alias is looked up
   by the alias's stats name(s) *instead* (`by_alias`). A salary name may
   alias to several stats names.
1. `EXACT`: the same name up to whitespace, and the same canonical team.
2. `NORMALIZED`: a shared name key (normalized, nicknames), and the same team.
3. `NAME`: a shared name key, any team.
4. `SURNAME`, **roster scope only**: the row's whole normalized name equals
   a candidate's surname. So "Andersen" and "Di Giuseppe" match, but
   "Luke Hughes" never matches Quinn on surname.
5. Fuzzy, rapidfuzz `token_sort_ratio` on name keys: never a match.
   - `REVIEW` if the best score is at least 90 in pool scope (SPEC), or 75 in
     roster scope.
   - Otherwise `UNMATCHED`.
   - Either way, the top 3 candidates are returned for the Admin match
     review, which lists "unmatched row vs. top 3 candidates" (SPEC §7).
   - In roster scope a one-word row is also scored against surnames, so
     "Oetterger" scores 77.8 against Oettinger.

An unknown or missing team skips steps 1-2. **Position group breaks ties at
every step**, not only steps 3-4. Steps 1-2 tie only when two players share
a name and a team: the Elias Petterssons are both Vancouver. There, position
is the only way to tell them apart. A tie position can't break is
`AMBIGUOUS`, and returns every tied candidate. Position never overrides a
unique name: a row saying D still matches the only Connor McDavid
(superseded: see "M3 review round 2: weak matches go to review"). Fuzzy
ties are ordered same group first.

Why roster fuzzy is 75: among the roughly 27 players of one roster, a typo
like "Oetterger" (77.8) should still be offered first. The owner confirms
every fuzzy candidate, so a lower bar costs only a suggestion. Why
`token_sort_ratio`: it's order-insensitive and doesn't inflate on substrings
the way `WRatio` does (Zuccarello vs Zuccarello-Aasen scores 95 there).

Results don't depend on candidate order (tested). Mutation testing
(`mutmut`, all of `src/fha/domain`): 636 mutants, all killed.

## 2026-09-26 — M3: free-agent CSV parser as built
`fha.sources.puckpedia.parse_salary_csv(bytes) -> list[SalaryRow]`, following
"M3: free-agent salary CSV format":
- **Positions:** accepted if they map to a position group, so "C/L" is
  accepted too, a superset of C/L/R/D/G. They are stored uppercased.
- **Cap hits:** `$18,000,000`, `18000000` and ` $ 775,000 ` are accepted,
  and `$0` reads as 0. Rejected: `$1.5M`, negative amounts, and misplaced
  commas such as `$18,00,000`.
- **Blank lines** are skipped (pasted pages leave them). Line numbers are
  the file's.
- **The first non-blank row is the header.**
- **Names** are tidied (whitespace) but not normalized: the matcher does
  that.
- **Duplicate rows** (the same player pasted twice) are all returned. The
  import service decides what re-importing means (idempotent, SPEC §6).

On the owner's `private/salaries-example.csv`, with the agreed header row
prepended: 100 rows, 0 goalies (the skater table only), 0 errors, and no
non-breaking spaces left in names.

## 2026-09-26 — M3: salary services (league sheet and free agents)
- **Stored:** the parsed league sheet only (`league_sheet/latest`: the
  payroll ranges' rows, IR rows, payroll and cap values, the read time). The
  grid never reaches the Repository. Tab names are stored, since they are
  what a tab is bound by. They live only in the owner's Firestore or dev
  file, never in the repo.
- **Tab ↔ team:** bound once, one tab per team, in the Admin screen.
  `suggest_bindings` proposes the team that matches at least half of a
  tab's rows, strictly more than any other team does. A team that would be
  suggested for two tabs is suggested for neither. Rebinding a tab forgets
  its row bindings, since they were matched against another roster.
- **Rows ↔ roster:** SPEC §6 roster scope, aliases first. An automatic match
  is persisted like a confirmed one (`auto:<step>` or `confirmed`), so a row
  is never re-matched by name. The exception is a row whose bound player has
  left the roster: it is matched again. Two rows on one player trust
  neither, and both go to review.
- **Discrepancy report:** Yahoo roster players in no row; rows matched to no
  roster player; the tab's PAYROLL against the sum of its matched counted
  rows. An unbound tab reports nothing yet. Its payroll, like an
  unrecognized tab's, is None ("unavailable", SPEC §5).
- **Free agents:** rows are keyed by normalized name + position group and
  stored in canonical order. Re-importing the same file writes nothing.
  - Duplicates with the same cap hit collapse. Duplicates with different cap
    hits are reported and not imported.
  - Bindings and overrides are one document each (a few hundred entries),
    so an import is a handful of writes rather than one per row. The alias
    table is one document per salary name, written once, at seeding.
  - An AAV override (the single-player edit) beats the import. A player
    bound to two rows gets no cap hit and is listed as a problem.
- **The alias seed** ships in the package (`fha/data/alias_seed.json`, NHL
  names only). A test keeps it identical to the extracted fixture.
- **Speed:** matching 800 rows against a 1,700-player pool took 8.2 s,
  because it normalized the same names millions of times. The matcher now
  memoizes its name functions (0.7 s). Tests clear the caches, since a cache
  shared across tests hid two mutants.

## 2026-09-26 — M3 review round 1: what changed
Two independent reviewers (storage and refresh; salaries). Their findings are
fixed as listed in PR #5's review log. The design changes:
- **Refresh:**
  - After a failed Yahoo refresh, requests within 60 s get the stale
    snapshot and the error without calling Yahoo, forced ones included. So
    during an outage, requests queued behind a failure no longer each wait
    out their own Yahoo attempt.
  - A process keeps its last decoded snapshot, and a request reads only the
    cache's small meta document to learn whether that snapshot is still
    current. A whole-league cache is 2-3 MB of JSON, and Spark allows
    10 GiB of egress a month.
  - A snapshot Yahoo returned but the store refused is still served, with
    the error.
- **Firestore:**
  - `replace_all` lists only document IDs, using a field mask no document
    can match, since the app keeps `__`-prefixed field names out of
    documents.
  - Responses are requested without pretty-printing.
  - A token failure is a `RepositoryError`.
- **Dev file:** it must be inside the checkout's `private/`. A bare
  `private` path component isn't enough (on macOS every temp path resolves
  under `/private`). Setting the Firestore variables together with
  `FHA_LOCAL_REPOSITORY` is an error, not a silent choice.
- **Tests' network policy:**
  - A blocked attempt during collection fails the session.
  - A blocked attempt is not hidden by a skip.
  - Every SPEC §8 name is scrubbed; emulator tests keep only
    `FIRESTORE_EMULATOR_HOST`.
- **League sheet:**
  - Rebinding a tab to the same team keeps its confirmed rows.
  - A tab CAP that differs from the summary cap is in the tab's discrepancy
    report.
  - A bound tab that is unrecognized reports only its status.
  - Whole-dollar rounding (at most $0.50 per salary and on the payroll) is
    not a payroll discrepancy.
  - Unknown team strings are listed in both reports.
  - An even split between cap references gives no cap.
  - `check_league_sheet` shows tabs as "tab N" unless `--names`.
- **Matcher:**
  - A surname is split on the raw name's whitespace, so a hyphenated first
    name (Jean-Gabriel Pageau) stays one word.
  - Steps 3-4 skip a player whose team and position group both contradict
    the row's: he is only a fuzzy candidate, not a silent permanent binding.
- **Free agents:** a nullable `cap_hit_with_bonuses` (SPEC §6), read from an
  optional `Cap Hit With Bonuses` column. The base cap hit stays the one
  used.
- **Pushback:**
  - The John/Jonathan nickname group stays: the workbook's own aliases pair
    Johnny Gruden with Jonathan Gruden.
  - CI pins actions exactly, but Java 21 and Node 22 only to their major
    lines (patch drift can't affect the emulator tests). firebase-tools is
    pinned exactly; its npm dependencies can't be locked through npx.
- **For M4:**
  - Set the `httpx` logger to WARNING in the app's logging setup: at INFO it
    logs request URLs, which carry the sheet ID and the Firestore project.
  - Create `RefreshService` and the token providers per event loop (their
    `asyncio.Lock`s bind to the first loop that uses them).

## 2026-09-26 — M3 review round 2: weak matches go to review (owner's decision)
The round-2 reviewer found that a stale sheet row "Hughes / D / NJD" bound
silently and permanently to the roster's only Hughes, a C on another team.
The row's salary went to the wrong player, and the discrepancy report showed
nothing, although the report exists to make stale tabs visible (SPEC §4a).
The owner chose (2026-09-26):
- **Surname-only rows (roster scope):** a unique surname still matches, but
  if the row's team **or** position group contradicts that player, it
  becomes a candidate for review. The picked player is offered first, at
  100.
- **Name-only rows with no team (the PuckPedia CSV):** a position-group
  contradiction alone sends the row to review. Without a team, position is
  the only other evidence. Round 3 made the rule precise: a position
  contradiction sends a name match to review unless the team confirms it.
  That covers sheet rows whose team is blank or unmapped ("NY"), and players
  whose own team is missing.
- **Unchanged:** full-name matches with a team (steps 1-2), and name-only
  matches where the team fits, still bind even if the position disagrees,
  since sources differ on positions. A name-only match contradicted by both
  team and position is only a candidate (round 1).

The cost is one confirmation per contradicted row, versus a wrong salary
silently feeding `room_after` / `swap_ok`. SPEC §6 is updated to match.

The owner asked about a Team column in the CSV. It is already optional
(DECISIONS, CSV format), and it lets rows match at steps 1-2 and skip the
position-only review. PuckPedia's copied table has no team, so adding one
means pasting team by team. That's deferred until the first real import
shows whether the review list is long.


## 2026-09-26 — M3 review rounds 2-3: how the refresh service keeps its snapshot
`RefreshService` keeps one snapshot in memory per process, the newest it has
seen:
- **Where it comes from:** decoded from the store, or fetched by this
  process. If the store refused the save, the snapshot carries a "not saved:
  …" note and is still served until the TTL, rather than sending every
  request back to Yahoo. The note is dropped once the store shows the same
  fetch, for example when a commit landed but its reply timed out.
- **Per request:** only the cache's meta document is read. The full cache is
  downloaded only when the store holds a newer fetch than memory. The
  download is single-flight, and its result replaces memory only if it is
  still newer when it arrives, since a local refresh may have finished in
  the meantime. Memory never regresses.
- **Not downloaded:** a stored fetch stamped in this clock's future (another
  instance's skewed clock: it would never count as fresh). Nor a newer fetch
  that failed to decode (another deploy's format), which is downloaded once
  and then skipped.
- **`refresh_error`** is a note for the UI. Either a refresh failed and the
  snapshot is stale, or the snapshot is fresh but "not saved". M4 must word
  the two differently.
- **Limit:** if Firestore is fully down, even a fresh snapshot in memory
  isn't served, because the meta read fails first. That follows "a backend
  failure is an error": the rest of the app needs Firestore anyway, and
  Yahoo isn't called again.
- **Also noted:** Google's error messages, which can name the project, reach
  `RepositoryError` and so the owner's UI and logs. The only audience is the
  owner, so they are kept for diagnosis.

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
  leaves unrated free agents out.
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

## 2026-09-26 — M4: the view layer
The screens don't compute. `fha.services.league_view.build_view` rates one
season's pool on every request, from the cached raw snapshot, the rating
settings and the salaries, into `PlayerRow`s and `TeamInfo`s:
- **Season:** the default is last season while the current season's max
  skater GP is below `BASELINE_MIN_GP` (10), when last season was fetched
  (SPEC §5). The toggle overrides it.
- **Salaries:** a rostered player's cap hit comes from his matched sheet row
  (the sheet wins, SPEC §4), then the CSV book (import plus overrides).
  Unknown stays None. A payroll exists only for bound, recognized tabs.
- **Row order:** rated skaters by rank, then unrated skaters, then goalies.

The pure per-screen modules on top of it:
- `players_table`: the Players filters and sort. Ranks and percentiles never
  change with a filter. Blanks sort last.
- `teams`: team summaries, the league table (the owner's team first), the
  matchup with need-sorted free agents and the "fits my cap" filter, and
  the Replace view with the `swap_ok` toggle. Both cap filters leave out
  unknown rows and count them (SPEC §5).

## 2026-09-26 — M4: the web shell (app factory, auth, chrome, PWA, demo mode)

- **Wiring.** `create_app(AppContext)`. The context holds `Settings` and a
  services factory. The factory runs in the app's lifespan, inside the
  running event loop, because the repository, refresh service and token
  providers own httpx clients and `asyncio.Lock`s ("For M4" in the round-1
  entry). Routes reach them through `request.app.state.context.services`
  (the `fha.web.app.services(request)` helper).
  - `AppContext.from_env` wires production: `repository_from_env`, the Yahoo
    client with `RepositoryTokenStore`, `RefreshService`, and the live sheet
    (`LEAGUE_SHEET_ID` with the service account's key) or a downloaded one
    (`LEAGUE_SHEET_XLSX`). Or it wires the demo league.
  - Missing configuration raises `ConfigError`, naming variables only. The
    app then shows it on an error page instead of crashing.
  - `fha.web.main:app` builds lazily on the first ASGI event. Importing it
    reads no environment and loads no heavy library (tested in a
    subprocess).
- **Auth (SPEC §7).**
  - `APP_PASSWORD` is compared with `hmac.compare_digest`.
  - The session is an itsdangerous `URLSafeTimedSerializer` token (salt
    `fha-session-v1`), signed with `SESSION_SECRET`, in the cookie
    `fha_session`: HttpOnly, SameSite=Lax, Secure unless
    `FHA_INSECURE_COOKIES=1` (local http only), for 400 days.
  - Every path except `/login`, `/manifest.webmanifest` and `/static/*`
    redirects to `/login?next=<path?query>`. htmx requests get a 401 with
    `HX-Redirect`. `next` is a relative path only (no open redirect).
  - Throttling is per process: after 5 failed logins in 60 s, logins are
    refused with 429 until the window passes. Serverless instances count
    separately; with one user and a strong password that stops casual
    guessing. The password is never logged (tested).
  - CSRF: SameSite=Lax keeps cross-site POSTs from carrying the cookie, which
    is enough for a single-user app with no GET side effects. Routes that
    change state must be POSTs.
  - API docs routes are off.
- **Chrome.**
  - `base.html` is mobile first (designed at 390 px), with a sticky nav and a
    header slot for the season label and last-refreshed time. The footer
    carries "Fantasy data provided by Yahoo Fantasy", linking to Yahoo
    Fantasy (required attribution).
  - htmx **2.0.4** is vendored at `src/fha/web/static/htmx.min.js`, from
    `https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js`, sha256
    `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`. There
    is no CDN at runtime and no build step.
  - Unexpected errors show a page without details, and log only the
    exception type and path. The `httpx` and `httpcore` loggers are set to
    WARNING, since at INFO they log URLs carrying the sheet ID and project.
- **PWA.**
  - `manifest.webmanifest`: standalone, `start_url /players`, theme
    `#0b3d91`, icons 192 and 512, and an apple-touch-icon at 180.
  - The icons are drawn by `scripts/make_icons.py` (stdlib zlib+struct PNG
    writer; a puck on the brand blue).
  - **No service worker:** installable to the home screen without one, and
    nothing to cache-bust. Offline use isn't a Phase 1 goal.
- **Demo mode (`FHA_DEMO=1`).**
  - `fha.sources.yahoo.demo.demo_snapshot(seed)` is a deterministic
    synthetic league: 8 teams of 27, every slot kind, 300 free agents, both
    seasons under Yahoo's stat IDs, and this and next week's scoreboards.
    The week is 3, so the baseline view applies. Names are made up from
    syllables.
  - Storage is `FHA_LOCAL_REPOSITORY` (inside `private/`) or in memory. The
    league sheet comes from `LEAGUE_SHEET_XLSX` if set.
  - This lets the owner see every screen before Yahoo access exists. Real
    Yahoo data replaces it with no code change.
- **Test client.** Starlette 1.7's `TestClient` asks for `httpx2` and warns
  on httpx (an error under `filterwarnings=error`), so `httpx2` is a dev
  dependency. The app itself still uses httpx.

Alternatives:
- Starlette's `SessionMiddleware` (rejected: it signs a whole session dict,
  where a single fixed claim is simpler to check);
- JWTs (rejected: no need for claims or a key-rotation story);
- a CDN for htmx (rejected: the phone may be offline, and it's one more
  third-party request).

## 2026-09-26 — M4: the rated screens as built (SPEC §7.1-7.4)
- **Routes and query parameters:**
  - `GET /players` takes `owner`, `team`, `pos`, `min_gp`, `sort` (a column
    or a category), `dir`, `season` and `view`. `view=cats` is the
    Categories toggle; the default is salary.
  - `POST /refresh` takes `next`, a relative path only, via `safe_next`.
  - `GET /rosters` takes `team` and `season`.
  - `GET /rosters/replace` takes `drop`, `swap_ok=1` and `season`.
  - `GET /league` takes `season`.
  - `GET /matchup` takes `week=current|next` and `season`.
  - `GET /matchup/free-agents` takes `week`, `fits=1` and `season`.
- **No JS needed.**
  - Filters are links (the chips) plus a GET form (team, position, min GP).
  - Sorting is header links: tapping the sorted column flips `dir`, and a new
    column starts in its natural direction (best first, cheapest first for
    $/TTLTST).
  - A row's norm breakdown is a `<details>` in the name cell.
  - htmx isn't used yet. The pages are plain enough that full reloads are
    fast.
- **Bad parameters** (an unknown owner, position, view, sort, week, team,
  season or toggle value) render a 400 page in the layout with a plain
  message, never a 500.
- **Formatting (`fha.web.format`):**
  - Money is `$7.25M` from $1M up, `$0.925M` from $1,000, and whole dollars
    below, rounded half-up in decimal; a minus sign for negative room, which
    never shows as zero (round 1, below). Unknown money is always "—", never
    $0 (SPEC §5).
  - The rating settings in use appear on every rated screen, e.g. "workbook
    top-20, floor 2%" (SPEC §7.5).
  - The filters are installed on the app's Jinja environment when the
    screens are imported (`app.py` includes the routers lazily).
- **The season toggle** is shown only when last season was fetched. The
  label always says which season is shown.
- **Rosters:**
  - Payroll shows "unavailable" until the team's tab is bound and
    recognized.
  - Over the cap shows red ("over the cap").
  - The "Sheet differs" badge links to Admin.
  - Replace links appear only on the owner's team. A drop missing from the
    sheet, or on an IR row, is labelled "frees nothing".
- **Sorting by a category** shows its column only in the Categories view:
  the salary view has no room for the norms at 390 px.
- **The rates note** ("profiles compare rates, not projected weekly
  totals", SPEC §5) is on Rosters, Replace, League, Matchup and the
  free-agent list.
- **At 390 px:**
  - Tables scroll sideways inside `table-wrap`, and the first column (the
    name) is sticky.
  - Chips scroll horizontally.
  - The filter form wraps.

## 2026-09-26 — M4: the Admin screen (SPEC §7.5)
One page at `/admin`, in sections: the league sheet, tab bindings,
discrepancies and row review, the salary CSV, free-agent review, the cap
hit edit, refresh, rating settings, and configuration.
- **Post/redirect/get.** Every change is a POST (`/admin/sheet/read`,
  `/admin/sheet/upload`, `/admin/bind`, `/admin/sheet/confirm`, `/admin/csv`,
  `/admin/fa/confirm`, `/admin/fa/unbind`, `/admin/aav`, `/admin/settings`)
  that redirects to `/admin#<section>`. The outcome travels as a `flash`
  query value: an itsdangerous token signed with `SESSION_SECRET` (salt
  `fha-admin-flash-v1`), valid for 10 minutes. So a message can't be forged
  in a link, a reload repeats nothing, and GET changes no state. The one
  exception is the league sheet's automatic row matches, which every rated
  screen persists (bind once).
- **Without Yahoo data** (before access, or during an outage with no
  cache), the page still renders. The sheet, settings and config sections
  work; bindings, review, the CSV import and the cap hit search say they
  need Yahoo data. Invalid stored rating settings also don't break the page,
  since the settings form is where they're fixed.
- **Hygiene (SPEC §4a).** Only `ParsedSheet` data is rendered (tab names,
  statuses, reasons, payroll, cap, counted rows). A test uploads a synthetic
  sheet with a contact block and asserts none of it appears, before and
  after binding.
- **Uploads.** The `.xlsx` is capped at 4 MB and the CSV at 2 MB; the file
  is read one byte past the cap to tell. An unreadable file, a
  `LeagueSheetError` or a `SalaryCsvError` becomes a message, never a 500.
  openpyxl stays lazy (the cold-start test passes).
- **The cap hit edit.** Typed amounts are `$7,250,000`, `7250000`, `7.25M`
  or `725K` (M/K in either case, spaces ignored). Commas must group
  thousands, and a scaled amount must come to whole dollars. Anything else
  (`7.25`, `1,5`, `7M5`) is refused, not guessed. The search is a GET over
  the pool by normalized name (at most 20 results). Each result shows its
  cap hit and source: sheet, override or csv. The owner can clear an
  override, or unbind the player's CSV row, which sends it back to review.
- **Rating settings.** The GP floor is entered as a percent ("2" or
  "2.5%") and stored as the fraction (`Decimal`, so 2.5 is exactly 0.025).
  A blank divisor count means "the method's default", which is how
  switching method picks up its usual count without JS: the form shows each
  method's default and the effective top-N in use. Invalid values show the
  engine's own message.
- **Review.** Sheet rows the cascade couldn't bind, and free-agent rows,
  list their top 3 candidates as one-tap buttons. A free-agent confirm can
  also save an alias. At most 30 free-agent reviews are shown at a time,
  with a count of the rest.
- **Refresh** is a POST to `/refresh` with `next=/admin` (the Players
  router's route).
- `fha.services.free_agents` gains `load_bindings` and `load_overrides`,
  used to show where a cap hit comes from and to offer unbind.

Alternatives: a flash stored in the Repository and cleared on read
(rejected: a GET with a side effect); rendering the page straight from the
POST (rejected: a reload resubmits the upload); a JS toggle to pre-fill the
count (rejected: no JS needed, since blank already means the default).

## 2026-09-26 — M4 review round 1: what changed
Reviewed at `105bb41`. Every medium is fixed with a test that fails without
the fix; the lows are fixed or recorded here.
- **No Yahoo data (M4R1A-1).** With no cache, anything the source raises (a
  timeout as much as a Yahoo 403) is now a `RefreshError`, chained to the
  cause. The rated screens show it as a 503 "No Yahoo data" page with the
  reason and the nav (`fha.web.data.NO_DATA` = `RefreshError`, `ViewError`),
  and Admin renders around it as before.
- **A refused service account (M4R1A-2)** is a `LeagueSheetError`
  ("service-account auth failed: …"), so Admin's read shows it.
- **Sticky header (M4R1A-3/B-3).** A box that scrolls sideways is also a
  vertical scroll container, and sticky cells stick to it, not the page. So
  `.table-wrap` scrolls both ways, at most a screen tall less the nav, the
  footer and the safe-area insets, and `thead th` sticks at its top. The
  Players row count sits above the table, so scrolled to the page's end the
  header is still below the nav. Checked in Chrome at 390×844: the header is
  in view at every page and table scroll position.
- **Rosters' Refresh (M4R1B-1)** is no longer nested in the team picker's
  form. A test checks that no page nests forms.
- **A failed Refresh says so (M4R1B-2).** The refresh service notes a failure
  on a snapshot that is still fresh while it is within the retry window
  (60 s), unless a newer fetch succeeded since; it replaces a "not saved"
  note (round 2). So the page the Refresh
  button returns to shows "Yahoo couldn't be reached". This keeps the note
  server-side instead of in a URL flag that every link would carry on.
- **`SALARY_CAP` (M4R1B-4)** is read by `Settings.from_env` (whole dollars,
  commas allowed) and passed as `load_salaries(cap_override=)`. The sheet's
  cap still wins. Admin's configuration list shows it.
- **Test strength (M4R1B-5).** The Players sort tests assert the exact order,
  per column and direction: ties by name then id, blanks last. They run over
  the demo league, and the expected order comes from the row values, not
  from the code under test. Replace has dual-eligible and Util cases, and
  the cap filters have `AAV == room` and zero-room-after cases. Name
  tie-breaks are also tested. Every mutant the reviewer applied by hand,
  and a few more, now fails a test.
- **Goalies' Yahoo rank (M4R1B-6): deferred to the M2 recording.** The
  `Player` model has no rank, and Yahoo's rank fields (`player_ranks`, or the
  `sort=AR` order of available players) have never been seen in a real
  response. Parsing an unseen shape would be a guess. It lands with the M2
  fixtures, before M4 acceptance, which needs real data anyway. SPEC §7.2
  notes this.
- **Categories toggle (B-7).** `view=cats` now works on Rosters, Replace and
  League too (SPEC §7: "common to the tables"). Salary view:
  - Rosters: AAV and $/TTLTST;
  - Replace: AAV and room after;
  - League: payroll and room.
  - the Matchup need list: AAV and Fits (added in round 3).

  The Categories view swaps those columns for the 7 norms (Δ norms on
  Replace). Links carry `season` and `view`, and the nav carries `season`.
- **Money (B-8)** rounds half-up in decimal. Amounts under $1M get three
  decimals, and under $1,000 whole dollars, so −$4,000 is `-$0.004M`, not
  `-$0.00M`. Numbers and percentiles round half-up too, never show `-0.00`,
  and show "—" when not finite.
- **Hardening (A-4, A-7, A-8, A-9, A-11, B-9, nits):**
  - **Cap hit input:** capped at $1,000,000,000, and nothing over 20
    characters is parsed.
  - **`min_gp`:** at most 4 digits.
  - **Login after a POST:** `next` is the same-site Referer, since a POST's
    path may have no GET (e.g. `/refresh`).
  - **Admin POSTs check their values:**
    - a tab must be in the stored sheet;
    - a team, or a player, must be in the Yahoo data;
    - a sheet row must be on its tab, and its player on the bound roster;
    - an alias is named from the stored row and the pool, never the form;
    - a `RepositoryError` (e.g. a tab named `__x__`) is a message, not a 500.
  - **Request size:** a body over 4.5 MB (Vercel's limit) is refused by its
    Content-Length before it is read. The sheet upload cap is 4 MB.
  - **Settings:** `SESSION_SECRET` must be at least 32 characters, and
    `FHA_INSECURE_COOKIES=1` is refused when `VERCEL` is set.
  - **Security headers** on every response: nosniff,
    `Referrer-Policy: same-origin`, `X-Frame-Options: DENY`.
  - **The error app** answers every method.
  - **Tests:** a sweep checks that every non-public route, POSTs included,
    needs a login. Entered test clients now exit.
- **Unexpected errors (A-5).** A middleware now answers them, instead of an
  exception handler that Starlette re-raised to the server's traceback
  logger. So "logs only the exception type and path" is now true: the test
  runs with server exceptions raised, and none escapes.
- **Without lifespan events (A-10).** If the host sends none, the services
  are built on the first request, once. This is harmless if Vercel does send
  them. The httpx client of a first-request build is never closed, and a
  serverless instance ends with its process.
- **The login throttle (A-6) stays global per process.** On Vercel, keying it
  by `x-forwarded-for` is sound, since Vercel sets that header. But a
  lockout costs the owner at most 60 s, and only while someone is guessing,
  which is also when a lockout is wanted. Revisit in M5 if it bites.
- **Logout (A-11)** deletes the cookie only, so a copied cookie stays valid
  until it expires. Revoking all sessions means rotating `SESSION_SECRET`.
  A server-side revocation list would add a store read to every request.
  Not worth it for one user; recorded for M5's runbook.

Alternatives:
- a `refresh=failed` query flag (rejected: every filter and toggle link would
  carry it on);
- a signed flash like Admin's (rejected: the same problem, and a GET-visible
  token);
- an httpx-level `GoogleAuthError` catch in the route (rejected: every
  caller of the reader would need it).

## 2026-09-26 — M4 review round 2: what changed
Reviewed at `59351ec`. Both reviewers reverted each round-1 fix in their
scope; every one failed a test. Domain mutmut killed 936 of 936. Two
mediums, both fixed with tests:
- **A negative amount on the sheet (M4R2B-1).** A negative salary crashed
  every rated screen and Admin, because `PlayerSeason` refuses one. The
  parser now reads a negative salary or cap as unknown, like `???`. A
  negative PAYROLL makes the tab unrecognized ("the PAYROLL beside C2 is
  negative"), since cap arithmetic can't use it.
- **Damaged stored rating settings (M4R2A-1).** They gave a 500 on the rated
  screens and on four Admin POSTs. The rated screens now show a page that
  says so and links to Admin's rating settings. The Admin POSTs flash "…
  needs valid rating settings first". Admin GET already handled this.

Lows and nits, all fixed:
- **Storage down (M4R2B-3)** is a 503 page, "the app's storage couldn't be
  reached". It shows no details, since Firestore's reason text can name the
  project, and it logs the type and path only.
- **A missing or mistyped form field (M4R2A-6)** is the 400 page. FastAPI's
  422 JSON used to echo the input back.
- **Content-Length (M4R2A-2)** is checked for digits and length before
  `int()`. The 413 page says 4.5 MB, the real limit.
- **A lazy build that raises anything but a ConfigError (M4R2A-3)** is
  retried on the next request, instead of leaving the instance broken. The
  httpx client is made once.
- **`CACHE_TTL_MINUTES` and `BASELINE_MIN_GP` (M4R2A-4)** must be finite and at
  most 1e6. `nan` and `1e400` used to get through or crash the build.
- **The season toggle and Refresh (M4R2B-4)** are now on Replace and the
  Matchup free-agent list too (SPEC §7).
- **The `view` parameter (M4R2B-6):** Matchup refuses a bad `view` like every
  other rated screen. A 400 page links back to its screen's top level, since
  a Replace link without `drop` would itself be a 400.
- **Test gaps (M4R2A-5, M4R2B-5)** are closed, and each mutant the reviewers
  listed now fails a test:
  - the stored alias document is asserted;
  - the confirm POSTs are tested against a store that refuses writes;
  - a 4.2 MB sheet is tested;
  - the sign of the Δ norms is tested;
  - "My Team" with no team of mine is tested;
  - eligible positions versus the display text are tested;
  - the `view` input in the picker and the back link is tested;
  - roster and Replace colspans are tested;
  - money at $1,000 and $999.50 is tested;
  - the failure note at the fetch's own instant is tested.
- **A Content-Security-Policy** (`default-src 'self'; frame-ancestors
  'none'; base-uri 'none'; form-action 'self'; object-src 'none'`). htmx's
  inline indicator style is turned off with `htmx-config`, since the
  templates have no inline scripts or styles.
- **Nits:**
  - a `vh` fallback before the `dvh` table height;
  - trailing categories listed in SPEC order;
  - `money(999.5)` is `$0.001M`;
  - the cap-hit search says it needs Yahoo data when there is none;
  - `players.html` uses `in_ir_slot`.

## 2026-09-26 — M4 review round 3: what changed
Reviewed at `4d1f6fc`.
- **Reviewer A:** approve, 3 lows.
- **Reviewer B:** 1 medium, lows.

Both reverted every round-2 fix in their scope, and each revert failed a
test. Domain mutmut killed 936 of 936.

**Fixed:**
- **The need list's Categories toggle (M4R3B-1, medium).** SPEC §7's toggle
  was missing on the Matchup free-agent list. The Categories view swaps AAV
  and Fits for the 7 norms and highlights the trailing categories.
- **A stored negative amount (M4R3A-2)** reads back as unknown in
  `decode_sheet`, so a sheet stored before round 2 can't crash the screens.
- **A huge row number in a PAYROLL formula (M4R3A-1)** makes the tab
  unrecognized: a cell reference's row stops at 12 digits.
- **Store errors in Admin's flash (M4R3A-3).** The flash is signed but not
  encrypted, and travels in the redirect URL. So a `RepositoryError` now
  flashes a fixed "the app's storage refused the change" and logs only the
  type. Firestore's own text can name the project.
- **HTTP errors (M4R3B-3).** A 404, a 405 or a malformed body now gets the
  app's page instead of FastAPI's JSON, and a 405 keeps its `Allow` header.
- **The stale note (M4R3B-2)** says Yahoo is asked again a minute after a
  failure, so a second tap on Refresh within the minute isn't a mystery.
- **Short quotes on the 400 page (M4R3B-6):** a bad `season` or `view` value
  is quoted at most 12 characters.
- **The tests the reviewers' surviving mutants pointed at:**
  - IR+ is outside the profile;
  - exactly $0 of room is not over the cap;
  - a team with no rated player is last in the league table;
  - equal names are ordered by id;
  - the Players filter form keeps `view`;
  - the trailing categories are listed in SPEC order;
  - `money(-0.4)` is `$0`;
  - the CSP is checked whole;
  - the 400 page's back link;
  - one httpx client across build retries;
  - no alias from a name to itself;
  - a `//` Referer path.

**Recorded, not changed:**
- **The failure note is per instance (M4R3B-2).** On Vercel, the GET after a
  failed Refresh can land on another instance, which shows no note. Sharing
  the failure would mean a store write on every failed refresh. That's not
  worth it for one user: the next stale-cache request retries anyway.
- **`MATCHUP_CLOSE_MARGIN` (M4R3B-4)** is config in the code
  (`ProfileConfig.close_margin`, 0.05), not an env variable or an Admin
  setting. SPEC §5 says "config" without naming where, and §11 Q6 already
  plans to revisit such defaults after the owner uses the app.
- **A chunked body with no Content-Length** skips the request-size check.
  The per-file caps still hold after spooling, and Vercel's own 4.5 MB limit
  applies.
- **htmx is loaded but unused.** It's kept, since SPEC names it for the UI
  and Phase 2 is expected to use it.


## 2026-09-26 — M4 review round 4: what changed
Reviewed at `133264c`.
- **Reviewer A:** 1 medium, lows.
- **Reviewer B:** approve with lows.

Both reverted every round-3 fix in their scope, and each revert failed a
test. B applied 61 hand mutants to the services, formatting and templates;
40 were killed, 5 of the survivors are equivalent, and the rest are below.

**Fixed:**
- **A binding whose tab left the sheet (M4R4A-1, medium).** When a GM renames
  their tab, the old tab's binding still holds the team. Binding the renamed
  tab was then refused, and Admin didn't list the old binding, so nothing
  could undo it. Admin now lists such bindings, marked "no longer on the
  sheet", each with an Unbind button. The POST already accepted a bound tab
  that isn't on the sheet.
- **Store text on the pages (M4R4A-2).** The Yahoo token lives in the store,
  so a refresh can fail with a `RepositoryError`, and its text (Firestore's
  reason) reached the no-data page, Admin and the stale note. The refresh
  service now names a store error by its type only, in failure notes and in
  "not saved" notes. With round 2's storage page and round 3's flash, the
  app never shows Firestore's text on a page.
- **A 5000-digit divisor count (M4R4A-3)** flashed Python's own "Exceeds the
  limit (4300 digits)" text. The form now refuses anything over 6
  characters, in its own words.
- **Error pages for visitors (M4R4A-4).** A 404 under `/static/` or a 405 on
  `/login` showed the nav and Log out without a login. They show neither
  now.
- **Profiles on the engine's categories (M4R4B-1).** Stored rating settings
  with a category subset, which only a store edit can make, gave a 500 on
  four screens: the team profiles always used all 7 categories. They now
  use the engine's.
- **Short quotes (M4R4B-3).** On Players, a bad `owner`, `pos`, `sort` or
  `dir` is quoted at most 12 characters too, and the `view` quote is tested.
- **Tests:**
  - the need list's Categories cells, their trailing highlight and its
    colspans (M4R4B-2);
  - the 2.5% floor label (M4R4B-6).
- **The bye message (M4R4B-7)** says "next week" on next week's list.

**Recorded, not changed** (the round-4 triage: lows that aren't one-to-three-line
fixes, and don't guard a claim made here, are listed rather than coded):
- **Rendered details no test pins (M4R4B-4):**
  - Rosters' SD column and its $/TTLTST cell;
  - goalie SV%;
  - the "Free agents who help here" link keeping `week=next`;
  - the owner chips clearing a picked team;
  - Replace's "IR row: frees nothing" and its `over` class;
  - League's `over` class and "sheet" badge;
  - the need list's "no" for Fits.

  Each renders correctly today; a mutant that breaks one passes the suite.
- **Tie-breaks no test pins (M4R4B-5):**
  - Replace's unrated-last order;
  - the need list's id tie-break;
  - the League table's name tie-break;
  - goalie name order in the league view.
- **The stale note matches its backoff by text (M4R4A-5).** `data.py` finds
  "retrying after" in the note, which can include Yahoo's own message. It's
  brittle but pinned by a test; a flag on `Cached` would be cleaner.

Round 4 found one medium, fixed with a test, so round 5 runs: the loop ends
at the first round with no medium or worse.

## 2026-09-26 — M4 review round 5: what changed
Reviewed at `a4e6114`.
- **Reviewer A:** approve with 2 lows.
- **Reviewer B:** 1 medium, lows.

Both reverted every round-4 fix in their scope, and each revert failed a
test.

**Fixed:**
- **"You lead everywhere" with nothing to compare (M4R5B-1, medium).** If
  either team has no profile players (SPEC §5: nothing trails), the need
  list said "You lead everywhere, by more than the close margin". That's
  reachable through the UI before opening night with "This season" forced.
  It now says "Nothing to compare yet: {team} has no rated skaters in these
  numbers", naming the empty team.
- **Replace without my tab (M4R5B-2).** With no sheet, or my tab unbound,
  every drop was labelled "not on the sheet: frees nothing". It now says my
  cap room is unavailable. "Not on the sheet" is kept for a player missing
  from a bound tab.
- **The 400 page for a bad login form (M4R5A-1).** A visitor who posted a
  file as the password saw the nav and Log out. `bad_form` now shares
  `http_error`'s signed-in check.
- **The Admin flash's length (M4R5A-2).** A message quoting huge random
  input made the redirect URL too long. The flash text is cut at 300
  characters, and the kept search at 100.
- **Tests:** the 13-character boundary of the quoted query values, and a
  1.25% floor label (M4R5B-3).

**Recorded, not changed (M4R5B-3):** the fewer-categories screens are
tested for rendering, not for their colspans or headers, and no test checks
which parameter a Players 400 message names. Only a hand-edited store can
set fewer categories.

Round 5 found one medium, fixed with a test, so round 6 runs.

## 2026-09-26 — M4 review round 6: what changed
Reviewed at `5dfdac2`.
- **Reviewer A:** 1 medium, under the rubric (a fix without a test), plus lows.
- **Reviewer B:** 2 narrow mediums, both false statements in rare states.

Both reverted every round-5 fix in their scope; all but one failed a test
(A-1 below).

**Fixed:**
- **"There is no next week" without my team (M4R6B-1).** With no owner's team,
  Matchup's Next week said the season had ended. It now says my team isn't in
  the league's data, as the current week did. With my team present, `game`
  can only be None for next week, since the current scoreboard is always in
  the snapshot.
- **"No rated skaters" when they're on IR (M4R6B-2).** Profiles leave out
  IR / IR+ slots (SPEC §5), so the need list now says "no rated skaters
  outside IR / IR+", as Rosters' profile note does.
- **An unknown cap (M4R6B-3).** When the sheet's cap is unknown, and no
  `SALARY_CAP` is set, every room is unknown too. Replace now says my cap room
  is unavailable, and League's note adds that room needs the cap.
- **The kept search (M4R6A-1, A-2)** is trimmed and then capped at 100
  characters, and is now tested. Cutting before trimming could turn a padded
  search into blanks.
- **The flash cap cut real messages (M4R6A-3).** Round 5's 300-character cut
  could drop the end of a CSV import summary, including the unknown teams,
  which nothing else shows. Now the parts that can grow are cut instead:
  - Admin's own error messages quote at most 40 characters of form input
    (a tab, a cap hit, a GP floor). A success message names a tab whole,
    but only one that matched a stored sheet tab;
  - the conflict and unknown-team lists show 8 names, each cut at 40
    characters, then "and N more".

  Some errors raised elsewhere still quote their input whole: the CSV
  parser's (a `Pos` cell, a dollar amount) and the engine's (an unknown
  divisor method). A 1000-character cut on the whole flash bounds those
  (tested in round 7); real messages never reach it.
- **Tests:** the signed-in bad-form page keeps the nav (M4R6A-4).

**Recorded, not changed:**
- Replace's "No free agents at C,LW fit the cap" can read as "none fit" when
  every row was left out as unknown. The note above it gives the count.
- Grammar: "(1 players)" on the Rosters profile note.
- The conflict list names raw row keys (`name|F`).
- A 1e-7% floor shows as "1e-07%".

Round 6 found three narrow mediums, each fixed with a test, so round 7 runs.

## 2026-09-26 — M4 review round 7: what changed
Reviewed at `e8d86c0`.
- **Reviewer A:** 1 medium, under the rubric (a fix without a test), plus 2
  lows.
- **Reviewer B:** approve with lows. No medium or worse.

Both reverted every round-6 fix in their scope. Every round-6 fix in B's
scope failed a test; in A's scope, the four cuts below didn't.

**Fixed:**
- **The flash's last guard had no test (M4R7A-1).** Round 6 moved the old
  test onto a path that `_short` cuts first. A new test posts 20,000
  random hex characters as the divisor method, which the engine's error
  quotes whole, and checks the flash is cut at 1000 characters and the URL
  stays short.
- **Three untested cuts (M4R7A-2):** a long tab in a row match, a long cap
  hit or GP floor, and a long name in the conflict list. Each now has a test.
- **The round-6 entry overstated the cut (M4R7A-3).** It said all quoted
  input stops at 40 characters, but the CSV parser's and the engine's errors
  quote theirs whole, bounded only by the last guard. The entry is
  corrected.
- **Without my team, the need list (M4R7B-2)** says so, like Matchup does
  since round 6.
- **An empty week (M4R7B-1)** no longer says "a bye": the source fetches
  weeks up to the league's end week, playoffs included. Round 8 replaced
  the round-7 wording (M4R8B-1).
- **An unknown room isn't green (M4R7B-4)** on Rosters.

**Recorded, for the owner:**
- **Matchup shows mean norms, not std devs (M4R7B-3).** SPEC §7.4 asks for
  "side-by-side team profiles", and a profile (§5) has both. At 390 px the
  table already holds me, the opponent and the difference per category. The
  std devs are on each team's Rosters page. Adding them is the owner's call
  at acceptance.

Round 7's only medium was a missing test, now added, so round 8 runs.

## 2026-09-26 — M4 review round 8: what changed
Reviewed at `c322218`.
- **Reviewer A:** approve with lows.
- **Reviewer B:** 1 medium, in round 7's own wording.

Both reverted every round-7 change in their scope. All but one failed a
test (A-1 below).

**Fixed:**
- **"A bye, or the playoff bracket isn't set yet" (M4R8B-1, medium).** When
  the owner's team is out of the playoffs, the other teams still play, so
  neither reason is true. The app knows only the league's end week, not
  which weeks are playoffs, so it can't tell a bye from an elimination or
  an unset bracket. Matchup now says "Week N: Yahoo lists no opponent for
  you.", which is true whatever the cause. Tested with a week where two
  other teams play and mine doesn't.

  The lesson: a message should state what the data shows, not guess at a
  cause the data can't tell.
- **A padded cap hit over the limit (M4R8A-1)** is quoted short too; spaces
  are dropped before the length check, not from the quote. Now tested.
- **The round-6 wording (M4R8A-2)** now says that Admin's *error* messages
  cut quoted input. A success message names a tab whole, but only a tab
  that matched a stored sheet tab.

Round 8 found one medium, fixed with a test, so round 9 runs.

## 2026-09-26 — M4 review round 9: what changed
Reviewed at `2b70f95`. Both reviewers were asked to check every message
against round 8's lesson: say what the data shows, not a cause it can't
tell. Each found 1 medium.

Both reverted every round-8 change in their scope, and each revert failed a
test.

**Fixed:**
- **The stale note guessed "Yahoo couldn't be reached" (M4R9A-1, medium).**
  With a cache, the note said that for every failure: a refused sign-in
  (`YahooAuthError`), a changed response (`YahooParseError`), a store error
  reading the token. So a revoked token would never have pointed the owner
  at the consent flow. The note now says "Refreshing from Yahoo failed
  ({type})". For a `YahooAuthError` it adds that the sign-in may need
  renewing.
  - For the same reason, the storage page says "Reading or writing the app's
    storage failed", not "couldn't be reached": Firestore can also refuse a
    request.
  - The no-data messages drop "yet", since an outage or an unratable
    snapshot isn't "not yet".
- **Replace's cap maths when a row awaits review (M4R9B-1, medium).**
  `counts` is None when no sheet row is matched to the player. SPEC §5
  reads that as "missing from the tab: frees nothing". But when my bound
  tab has counted rows matched to no roster player (in match review, e.g.
  a misspelled name), one of them may be his. Then his cap hit is in
  PAYROLL, and "frees nothing" understated the room after every swap.
  - `Salaries.unmatched_counted` names such teams, and `TeamInfo` carries
    the flag.
  - Replace then treats the drop's cap hit as unknown: room after is "—",
    and the `swap_ok` toggle counts each row as left out.
  - The label says so and points at Admin. Without unmatched counted rows,
    the label now reads "no sheet row matched him: frees nothing", not
    "not on the sheet", which a matching miss can't tell apart.
- **"By at least the close margin" (M4R9B-3):** a category ties the margin
  when it doesn't trail.

**Recorded, not changed:**
- **"Free Agents" includes players on waivers (M4R9B-2).** Yahoo's
  `status=A` list holds both, and a waiver claim isn't a free-agent pickup.
  Whether the M2 recording carries the ownership type (W / FA) decides the
  fix, so it waits for the recording.
- **`FreeAgentError` quotes a form key whole (M4R9A-2):**
  `/admin/fa/confirm` with an unknown key. The flash's 1000-character last
  guard bounds it, alongside the CSV parser's and the engine's errors listed
  in round 6.

SPEC §5's `counts` now notes the unknown case.

Round 9 found two mediums, fixed with tests, so round 10 runs.

## 2026-09-26 — M4 review round 10: what changed
Reviewed at `d283d45`.
- **Reviewer A:** 1 medium.
- **Reviewer B:** approve with lows. It traced every consumer of `counts`
  and of a missing rostered salary, and found only Replace affected by a
  row awaiting review; the rest use the tab's official PAYROLL or the CSV
  fallback.

Both reverted every round-9 change in their scope. All but one failed a
test: the no-data page's wording, below.

**Fixed:**
- **The no-data page guessed a cause (M4R10A-1, medium).** Its hint said
  "this page comes back once Yahoo answers". That's false for a refused
  sign-in, for a response the parser doesn't know (likely on the first real
  fetch, since the M2 shapes are unrecorded), and for a snapshot that can't
  be rated (a `ViewError`, A-2). The hint now says what happens next:
  - Yahoo is asked again a minute after a failure;
  - for a `YahooAuthError`, the sign-in may need renewing;
  - for a `ViewError`, Yahoo's data arrived but can't be rated.

  The stale note and this page share `failure_kind`. The message's wording
  is now tested.
- **The IR-row case (M4R10B-1).** Only a *counted* row awaiting review can
  be the drop's, since an IR row frees nothing either way. A test now pins
  that.
- **Replace's label (M4R10B-2)** says "counted rows on my tab match no
  roster player (see Admin)" rather than "await review". A no-name row, or
  a traded player's row the commissioner hasn't removed, matches no one and
  can't be confirmed in Admin. The maths stays conservative: unknown, never
  a wrong number.

Round 10 found one medium, fixed with a test, so round 11 runs.

## 2026-09-26 — M4 review round 11: what changed
Reviewed at `cf445e1`.
- **Reviewer A:** 1 medium, in the Yahoo client (M2 code), plus 2 lows.
- **Reviewer B:** approve with 3 lows.

The first reviewer B returned a report on code that doesn't exist: files,
symbols and tests that were never in the repo. It later retracted: it had
handed back before running anything. That report was discarded, and a
fresh reviewer B ran under an evidence rule: the commit hash, and quoted
output for every claim. Both reviewers who reported reverted every round-10
change in their scope, and each revert failed a test.

**Fixed:**
- **One refused token renewal wedged a warm instance (M4R11A-1, medium).**
  `YahooClient._renew` cached a refusal and replayed it to any later
  request with the same `Token` object. `_current_token` kept that expired
  token in memory and never reloaded the store. So one non-200 from the
  token endpoint, even a one-off 503 at the hourly renewal, was replayed
  until the instance was recycled: Yahoo was never asked again, and a
  re-run consent was never loaded. The pages meanwhile said Yahoo would be
  asked again in a minute. Now:
  - a refusal is replayed only for the same refresh token and within
    `REFUSAL_REPLAY_SECONDS` (30 s), which is enough to share it across one
    fetch's concurrent requests. Later fetches, at least a minute apart
    under the refresh service's backoff, ask Yahoo again;
  - on a refusal the in-memory token is dropped, so the next request loads
    the store and picks up a new consent.

  This is M2 code, fixed on the M4 branch, since M2 (PR #4) is unmerged.
- **A snapshot that can't be rated (M4R11A-2)** is titled "Yahoo data can't
  be rated", not "No Yahoo data", which contradicted its own reason.
- **The retry note (M4R11A-3)** is on every stale note. The backoff applies
  from the failure itself, so an immediate second Refresh won't ask Yahoo
  either.
- **Replace (M4R11B-1, B-2):**
  - the label also covers two rows sharing one player ("…or share one");
  - a drop whose counted row has no cap hit (`???`) says that's why the room
    after is unknown.
- **Rosters (M4R11B-3):** the unavailable-payroll note drops "yet" and names
  a missing sheet as one cause.

**Recorded, not changed:** the token endpoint's 5xx is a `YahooAuthError`,
so a Yahoo outage during a renewal shows the "sign-in may need renewing"
hint. It says "may", and the next fetch asks again.

Round 11 found one medium, fixed with a test, so round 12 runs.

## 2026-09-26 — M4 review round 12: what changed
Reviewed at `c205e6e`. Both reviewers worked under the evidence rule.
- **Reviewer A:** 1 medium, under the rubric (a fix without a test), plus 2
  lows.
- **Reviewer B:** approve. It also raised 1 rubric medium and 1 low.

No bug a user can reach was found. Reviewer A checked the round-11 token
fix and found it sound:
- one fetch's requests share one refusal, with no stampede of token
  requests or store loads;
- the 401 path behaves with no token held;
- a stale waiter can't overwrite a newer token;
- a refusal can't be replayed across fetches, since the 30 s window is
  shorter than the refresh service's 60 s backoff.

Reviewer B walked every Replace label branch, in order, and found each true
in every state that reaches it. Its sweep of 510 odd query values over the
rated routes gave no 5xx.

**Fixed:**
- **The warm-instance refusal had no test (M4R12A-1).** Round 11's test
  covered a cold start only. A new test holds a token in memory, lets it
  expire, has the renewal refused, then stores a new consent and checks
  it's used. It fails without `self._token = None` on refusal.
- **A renewed token was lost if saving it failed (M4R12A-2).** If Yahoo
  retires the old refresh token on renewal, that loses the grant. `_renew`
  now keeps the renewed token in memory before saving, and the save error
  still propagates. Tested.
- **A clock stepping back (M4R12A-3)** doesn't extend a refusal. Tested.
- **Rosters' payroll note (M4R12B-1)** is asserted whole.
- **Two rows on one player (M4R12B-2)**, the case round 11's "or share
  one" wording was for, has its own test.

Round 12's mediums were both missing tests, now added. Under the rule,
round 13 runs, on a one-line change and tests.
