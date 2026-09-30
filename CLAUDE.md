# CLAUDE.md

Guidance for Claude Code sessions in this repo. Read this first, every session.

## What this is

A private, single-user web app for one Yahoo NHL fantasy team (league 8076).
Core feature: a custom player rating, **TTLTST**, shown next to each player's
salary (AAV). Replaces an Excel workbook. Used from a phone browser.

**The full specification is `docs/SPEC.md`. It is the source of truth.** If
this file and the spec disagree, the spec wins; flag the conflict.

## Stack

Python 3.12 · `uv` · FastAPI · Jinja2 + HTMX · Firestore (server-only) ·
Vercel (serverless) · pytest / hypothesis / respx / mutmut · ruff · mypy --strict

## Commands

```
uv sync                         # install
uv run pytest                   # tests
uv run pytest --cov --cov-report=json && uv run python scripts/check_coverage.py
                                # tests + per-package coverage gates
uv run ruff check . && uv run ruff format --check .
uv run mypy src scripts
uv run mutmut run               # mutation testing on src/fha/domain (rm -rf mutants first after test changes)
npx -y firebase-tools@15.31.0 emulators:exec --only firestore --project demo-fha \
  "FHA_REQUIRE_EMULATOR=1 uv run pytest tests/unit/storage -q"
                                # storage contract against the Firestore emulator (Java 21+ on PATH)
uv run python -m scripts.check_league_sheet private/<sheet>.xlsx
                                # parse the owner's downloaded league sheet (statuses and sums only)
FHA_DEMO=1 FHA_INSECURE_COOKIES=1 APP_PASSWORD=dev \
  SESSION_SECRET=dev-only-secret-at-least-32-characters \
  uv run uvicorn fha.web.main:app --reload
                                # the app on http://127.0.0.1:8000 with the demo league
uv run python -m scripts.make_icons   # redraw the PWA icons
uv run --env-file .env python -m scripts.yahoo_auth --firestore private/<key>.json
                                # OWNER ONLY: Yahoo consent, token saved to production Firestore
```

## How to work

- **Milestones are gated.** Work milestone by milestone (`docs/SPEC.md` §10).
  Do not start the next milestone until the current one's acceptance criteria
  are met and CI is green. Stop and report at each milestone boundary.
- **Test-first** for `domain/` (engine, matcher): failing test, then code.
- **Respect the seams.** Domain code is pure — no I/O, no network, no
  Firestore, no clock. External things go behind the Protocols in SPEC §3.
- **Record decisions** in `docs/DECISIONS.md`: what, why, alternatives, date.
- **Stay in scope.** Phase 1 only. No write access to Yahoo, no goalie model,
  no projections, no history, no waiver *recommendation* or trade-evaluation
  screens (the Replace view and the Matchup free-agent filter, SPEC §7, are
  in scope: the owner picks, the app filters and sorts). If something out of
  scope seems necessary, ask.
- Small, focused commits with clear messages. Don't push to `main` without
  the owner's go-ahead unless they've said otherwise for the session.

## Hard rules

1. **Secrets.** Never read, print, log, echo, or commit the contents of
   `.env`, tokens, or credentials. Reference secrets by variable name only.
   If you need to know whether a secret is set, check existence, not value.
2. **No live network in tests.** Yahoo is mocked from committed, sanitized
   fixtures. A test that hits the network is a bug.
3. **Never commit the owner's data.** `private/`, `*.xlsx`, `*.csv` are
   ignored. Only derived JSON fixtures under `tests/fixtures/` are committed.
   The repo is public.
4. **Don't run the Yahoo OAuth consent flow.** It needs the owner in a
   browser. Prepare the script; the owner runs it.
5. **Don't deploy** or create cloud resources (Vercel, Firestore, GCP)
   without the owner's explicit go-ahead.
6. **Don't game coverage.** Every test asserts behaviour. Reviewers reject
   tests that only execute code. Surviving mutants in `domain/` are killed or
   justified.

## Key facts (details in the spec)

- Categories in TTLTST: G, A, PPP, PIM, HIT, SOG, BLK. Goalies excluded.
- `per82 = stat / GP * 82`; divisor = mean of the top-20 totals over mean of
  the top-20 GP (× 82) among eligible players (the workbook's formula, the
  owner-confirmed default; method and count are owner settings, SPEC §5);
  `TTLTST` = mean of
  `per82 / divisor` across the 7.
- GP floor: rate only players above 2% of the pool's max GP (config).
- Percentile divides by the pool's skaters with GP > 0, rated or not; ties
  share a rank.
- Divisors recompute every refresh over the app's pool.
- Injured players stay ranked on rate stats.
- The league's shared salary sheet is the source of truth for rostered
  salaries, payrolls and the cap (SPEC §4a). Parse by each tab's PAYROLL
  formula, never by labels. Free-agent salaries come from a PuckPedia CSV.
- The sheet holds managers' contact details: never persist, log, render or
  commit any cell outside the parsed ranges (SPEC §4a). Tests use synthetic
  sheets only.
- Yahoo league key = `{game_key}.l.8076`; resolve `game_key` at runtime.
- Serverless: concurrent Yahoo calls (bounded), no filesystem state, light
  imports.

## Golden data

The owner's workbook goes at `private/2025_2026_stats.xlsx` (ignored by git).
It holds 2025-26 season data; the app targets 2026-27.
`scripts/extract_golden.py` turns it into the committed fixtures that the
metric engine must reproduce; `scripts/divisor_report.py` prints the divisor
comparison recorded in DECISIONS. Workbook layout notes are in SPEC §5 — trust
formulas over the row-2 labels, which are wrong in places.

## Suggested agent roles

- **Lead:** owns the milestone, breaks it into tasks, checks acceptance.
- **Builder:** writes tests then code for one task.
- **Verifier:** runs the full gate suite; checks golden parity and coverage.
- **Reviewer:** reads the diff for spec conformance, test quality, secrets
  leakage, and scope creep.

M1 is a tight sequential loop — a team adds little there. M2–M4 have
independent workstreams (Yahoo source, storage/salaries, web) that
parallelize well.

M2–M4 are merged. M4.5 (Yahoo verification) waits on Yahoo API access; see
SPEC §10. M5 (deploy) setup started before it, by the owner's decision
(2026-09-30): the Vercel entry is `app.py` (`vercel.json`, `.vercelignore`).
