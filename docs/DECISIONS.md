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
unique name: a row saying D still matches the only Connor McDavid. Fuzzy
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
