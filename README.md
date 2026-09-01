# Fantasy Hockey Roster Analyzer

A private, personal tool for managing a single 8-team Yahoo NHL fantasy
league (league ID 8076). Replaces a spreadsheet I currently maintain by
hand.

## What it does

- Reads league settings, team rosters, and season stat totals via the
  Yahoo Fantasy Sports API (read-only)
- Combines those with NHL salary data (AAV) imported from a CSV
- Ranks players on a custom composite metric across the league's seven
  skater categories (G, A, PPP, PIM, HIT, SOG, BLK), normalized per 82
  games against the top-ten average in each category
- Surfaces salary and custom rating side by side for waiver, trade, and
  lineup decisions

## Scope

Single user. Single league. Read-only. Not distributed, not monetized,
no app store presence.

Fantasy data provided by Yahoo Fantasy.
