# Pending for DECISIONS (M4 screens S2: Admin)

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
- **Uploads.** The `.xlsx` is capped at 5 MB and the CSV at 2 MB; the file
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
