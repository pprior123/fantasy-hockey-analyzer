# Fantasy Hockey Roster Analyzer

A private, personal tool for managing a single 8-team Yahoo NHL fantasy
league (league ID 8076). Replaces a spreadsheet I currently maintain by
hand.

## What it does

- Reads league settings, team rosters, and season stat totals via the
  Yahoo Fantasy Sports API (read-only)
- Combines those with salaries (AAV) from the league's shared salary sheet,
  plus a PuckPedia CSV for free agents, and tracks each team's cap room
- Ranks players on a custom composite metric across the league's seven
  skater categories (G, A, PPP, PIM, HIT, SOG, BLK), each normalized per 82
  games against the league's top performers in that category
- Surfaces salary and custom rating side by side for waiver, trade, and
  lineup decisions

## Status

The metric engine (M1), the Yahoo source (M2), storage and salaries (M3) and
the web UI (M4) are built; M2–M4 are accepted on synthetic data. Verification
against real Yahoo data (M4.5) waits on Yahoo API access. Deployment to Vercel
and Firestore (M5) is being set up.

## Development

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you).

```
uv sync                                   # install
uv run pytest                             # tests (network is blocked)
uv run pytest --cov --cov-report=json && uv run python scripts/check_coverage.py
uv run ruff check . && uv run ruff format --check .
uv run mypy src scripts
```

Specification: [`docs/SPEC.md`](docs/SPEC.md). Decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Scope

Single user. Single league. Read-only. Not distributed, not monetized,
no app store presence.

Fantasy data provided by Yahoo Fantasy.
