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
