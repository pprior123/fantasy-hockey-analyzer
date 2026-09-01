# NHL Fantasy Management App — Build Plan

**League:** Yahoo NHL, ID 8076 · 8 managers · H2H categories · daily lineups
**Categories (10):** G, A, PPP, PIM, HIT, SOG, BLK (skater) + W, GAA, SV% (goalie)
**Constraints:** 5 acquisitions/week · keepers (~8, TBC) · roster 3C/3LW/3RW/6D/1G/9BN/3IR+
**Goal:** replace the spreadsheet's custom TTLTST ranking, with salary in the same view, usable from a phone.

---

## 1. Headline finding: Yahoo API access is now gated

Yahoo no longer hands out Fantasy API keys automatically. Access is read-only by default, every application is reviewed by the Yahoo Fantasy Sports team, and submissions that don't clearly identify the product, the data required, and the intended user base are closed without correspondence. The form explicitly accommodates personal or single-league use, and asks for an expected-user estimate (pick "Small, <1,000").

It's free, but it's the long pole. **Apply on day one** at `sports.yahoo.com/developer/access/`, describing it as a private single-league analytics tool for League 8076, read-only. Everything else can be built against cached sample data while you wait.

Read-only was your preference anyway, so the restriction costs you nothing in Phase 1.

Once approved: OAuth 2.0 three-legged flow, client ID + secret, refresh tokens. Yahoo also requires visible attribution — "Fantasy data provided by Yahoo Fantasy" with a link back — in products built on the API. Put it in the footer even for a private app.

---

## 2. Stack & hosting

You want it on your phone, private, and easy. Recommendation:

**Python 3.12 + FastAPI backend, SQLite, mobile-first HTML front end (server-rendered + HTMX), installable as a PWA.**

Why Python over C#/.NET despite your background: the Yahoo API is awkward (XML-first, odd JSON shapes, non-standard OAuth), and there are two mature wrappers that have already absorbed that pain — `yahoofantasy` (mattdodge) and `yfpy` (uberfastman), both of which explicitly support NHL. That's several days saved. If you'd rather stay in .NET, the plan is unchanged; you write ~400 lines of OAuth + XML parsing yourself.

**Hosting options, easiest first:**

| Option | Setup | Notes |
|---|---|---|
| Fly.io / Railway small instance | ~30 min | HTTPS out of the box (needed for the OAuth redirect URI), always on, ~$0–5/mo. **Recommended.** |
| Home machine + Tailscale | ~20 min | Free and totally private, but the machine must be awake when you're on the bus. OAuth callback can use a Tailscale hostname. |
| Streamlit Cloud | ~10 min | Fastest to a working UI, worst mobile ergonomics for dense tables. Fine as a throwaway prototype. |

Auth for *you*: a single hardcoded password or a signed cookie. There's one user; don't build a user system.

---

## 3. Data sources

| Data | Source | Method |
|---|---|---|
| League settings, teams, rosters | Yahoo API | `/league/{key}/settings`, `/teams`, `/team/{key}/roster` |
| Player pool + ownership status | Yahoo API | `/league/{key}/players;status=A` (available) and `;status=T` (taken), paged 25 at a time |
| Season stat totals | Yahoo API | `/players;player_keys=...;/stats;type=season` — **source of truth**, kills your HTML-scraping and NUMBERVALUE cleanup entirely |
| Salary (AAV) | PuckPedia | CSV import (see below) |
| Tonight's schedule | `api-web.nhle.com` | Free, no auth. Phase 2 only. |

**League key:** Yahoo needs `{game_key}.l.8076`, not the bare ID. Fetch the current NHL game key at startup from `/games;game_codes=nhl;seasons=2026` rather than hardcoding it — it changes every season and hardcoding it is the classic October outage.

**Salary ingestion.** PuckPedia has no public API and scraping it is a ToS question you don't need. Phase 1: an **Import Salaries** screen that accepts a pasted CSV or an uploaded file (name, team, AAV) and runs the matcher. Your league's roster/salary Google Sheet can feed this too — but I couldn't open the link you sent (it needs auth). Either **File → Share → Publish to web → CSV** and give me that URL, or export a CSV once per season. One import per season plus occasional patches for trades is realistic; don't over-engineer it.

---

## 4. The name-matching problem

Your 57-row alias table is the fragile heart of the spreadsheet. Fix it structurally:

1. Match salary rows to **Yahoo `player_id`** once, not on every calculation.
2. Match cascade: exact name+team → normalized name (strip accents, lowercase, drop punctuation, "Matt"/"Matthew" nickname table) + team → normalized name only → fuzzy (rapidfuzz, ≥90) surfaced for confirmation.
3. Anything unmatched lands in a **Review Matches** screen: unmatched salary row on the left, three candidate Yahoo players on the right, tap to bind. The binding persists in a `player_alias` table keyed on `yahoo_player_id`.
4. Import your existing 57 aliases as seed data.

After the first season, this is a two-minute chore at import time and never again.

---

## 5. Metric engine (parity with the spreadsheet)

Faithful port of your current logic, per your Phase-1-parity instruction:

```
per82(cat, player)   = stat / GP * 82
divisor(cat)         = mean of the top 10 per82 values for that cat, league-wide player pool
norm(cat, player)    = per82(cat, player) / divisor(cat)
TTLTST(player)       = mean(norm) over G, A, PPP, PIM, HIT, SOG, BLK
percentile(player)   = (1 - rank / N) * 100
value(player)        = AAV / TTLTST / 1e6
```

Preserved from the sheet:
- **PPP = ppGoals + ppAssists** (Yahoo may expose PPP directly; if so, use it and drop the addition).
- **GP floor:** you currently exclude players below 2% of max GP. Keep it — it stops a one-game callup with a hat trick from owning the divisors. Make it a config value, not a magic number.
- **Injured players keep their rate stats** and stay ranked, per your call.

**Recompute cost:** trivial. 900 players × 7 categories is microseconds; the expense is the ~20–35 Yahoo calls to refresh stats. Plan: cache stats in SQLite with a TTL of 30–60 minutes, recompute metrics on every page load from cache, and put a manual **Refresh** button in the header for when you want it now. Hourly is entirely fine.

**Bugs to fix in the port** (found in the workbook):
- Row-2 header labels drift from what the formulas pull — column Q is labelled `hits` but pulls `blocks` from `Stats Data Source!AE`. The numbers downstream are right; the labels lie. Don't carry that over.
- The divisors in `W6:AC6` are static values, so category weights silently go stale as the season progresses. In the app they're computed every run.
- `Table2!AK3` has an `IF(ISNA(...))` whose true and false branches are identical — it does nothing. Drop it.

**Goalies:** out of TTLTST for now, per your call. But show your goalies in the roster view with Yahoo's own O-Rank and raw W/GAA/SV%, so lineup decisions aren't blind on 3 of 10 categories.

---

## 6. Screens (mobile-first, in build order)

**1. Player Table** — the core, and the thing that replaces the spreadsheet.
Columns: Name · Pos · Team · Owner · GP · TTLTST · Pctl · AAV · $/TTLTST.
Sticky header, sort by tapping any column, filter chips across the top: `All` `My Team` `Free Agents` `Taken` · position filters · min-GP slider. Tap a row to expand the 7-category norm breakdown (the AV/AX/AZ... columns) so you can see *why* a player rates.

**2. My Roster** — your 27 slots with the same metrics, plus team category profile (your `Table10` row: mean norm per category + stdev) so you can see at a glance that you're carrying HIT and light on BLK.

**3. Waiver Targets** — free agents sorted by TTLTST, with a "would upgrade" column comparing against your weakest rostered player at that position. Shows acquisitions used this week against the 5 cap.

**4. Trade Evaluator** — pick players from two teams, see both category profiles before and after. This is your `Table10` comparison made interactive.

**5. Admin** — salary import, match review, refresh, config (GP floor, category list, keeper count).

---

## 7. Phases

**Phase 0 — unblock (this week)**
Submit the Yahoo API application. Export the salary CSV from PuckPedia and get the league sheet published as CSV. Nothing else is blocked by these.

**Phase 1 — spreadsheet parity (the real deliverable)**
Yahoo ingestion + SQLite cache · salary import + matcher + review screen · metric engine · Player Table and My Roster screens · deploy to phone. When this ships you stop opening Excel.

**Phase 2 — the things Excel can't do**
Waiver Targets · Trade Evaluator · tonight's-games lineup helper from the NHL schedule API · acquisition counter.

**Phase 3 — open questions deferred by choice**
Daily history snapshots and trend lines · rest-of-season / games-remaining weighting (the injured-star question you deferred) · a goalie model to replace Yahoo's ranking · keeper-value view (TTLTST vs AAV vs contract term).

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| Yahoo application rejected or slow | Apply immediately with a specific, honest description. Fallback: build against a cached export and keep the current scrape as a stat source. |
| Yahoo rate limits on full-pool refresh | Fetch taken players + top ~300 FAs only, not all 900. 30–60 min cache. Never refresh on every page load. |
| Name matching breaks at import | Review screen + persistent ID bindings, not a formula chain. |
| Token expiry while you're on your phone | Store the refresh token server-side, refresh transparently, and fail to a "re-link Yahoo" banner rather than a stack trace. |
| Yahoo stat definitions differ from your scrape | Do a one-time reconciliation of last season's numbers for ~20 players before trusting the port. |

---

## 9. Still open

1. Keeper count — confirm (you guessed 8) and whether keeper cost relates to AAV.
2. The league Google Sheet link needs to be published-to-web or exported before I can use it.
3. Confirm Yahoo exposes PPP directly for your league, or whether we compute ppG + ppA.
4. Preference on stack: Python (faster, better wrappers) vs C#/.NET (your home turf)?
