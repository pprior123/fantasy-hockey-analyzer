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

## 2026-09-26 — M1: divisors use the workbook's formula (provisional; owner to confirm)
SPEC assumed `W6:AC6` were static values gone stale, and described the
divisor as the mean of the top-10 per-82 rates. Both were wrong. The cells
are live array formulas:
`AVERAGE(LARGE(stat, {1..20})) / AVERAGE(LARGE(GP, {1..20})) * 82`, i.e. the
mean of the top-20 totals over the mean of the top-20 GP (ranked
independently). The `Y4` label "mean top ten" is wrong, like the row-2
labels. Recomputing with that formula reproduces all seven divisors exactly
(golden test 2). The report (`uv run python scripts/divisor_report.py`):

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
