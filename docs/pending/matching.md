# Pending DECISIONS entries: name matching and free-agent salaries (M3 stream C)

To be folded into docs/DECISIONS.md by the lead.

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
