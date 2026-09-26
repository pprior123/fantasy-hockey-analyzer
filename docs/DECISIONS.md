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
- The committed test fixture is synthetic, in the same shape. The real CSV
  is never committed (hard rule 3).
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
  details). The parser never reads past the last IR row. Contact cells sit
  outside every range it reads, so they never enter a parsed result.
The synthetic parser fixtures reproduce each of these variants.
