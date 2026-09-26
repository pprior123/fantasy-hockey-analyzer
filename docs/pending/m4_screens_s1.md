# Pending for DECISIONS (M4 screens S1: Players, Rosters, League, Matchup)

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
- **Bad parameters** (an unknown owner, position, view, sort, week, team or
  toggle value) render a 400 page in the layout with a plain message, never
  a 500.
- **Formatting (`fha.web.format`):**
  - Money is `$7.25M`, with a minus sign for negative room. Unknown money is
    always "—", never $0 (SPEC §5).
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
