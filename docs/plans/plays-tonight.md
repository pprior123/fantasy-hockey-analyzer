# Plan: players who play tonight

Status: **implemented**, 2026-11-27, after quality-mgr's plan review (PT-P1: ready with
changes, 8 findings, all accepted; triage in §6). The owner needs no review of plans (2026-11-27).

The owner's idea (2026-11-27): show which players play tonight. Team-lead decides the source and
what "tonight" shows (work queue item 3; the owner asked that design choices not wait on him).

## 0. Decisions

| Question | Decision |
|---|---|
| Source | The NHL's public schedule, `GET https://api-web.nhle.com/v1/schedule/{YYYY-MM-DD}` (no key, no auth). Checked 2026-11-27: JSON with `gameWeek[]` (`date`, `games[]`), each game with `startTimeUTC`, `gameState`, `gameType`, `awayTeam.abbrev`, `homeTeam.abbrev`. Yahoo is not used: the recorded Yahoo responses (league, rosters, players, scoreboard; `tests/fixtures/yahoo/`) carry no game, opponent or schedule field. |
| A new outbound host | Yes, the first besides Yahoo and Google: a fixed host, behind a new seam (`ScheduleSource`, SPEC §3), mocked with respx from a committed, trimmed fixture (SPEC §9; the socket guard catches any live call). The endpoint is undocumented: when it fails or changes shape, the pages say so in one line, never an error page. |
| "Tonight" | Today's date in US/Eastern (the NHL's schedule date), from the `Clock`. That date's games with `gameType` 2 (regular season) or 3 (playoffs). |
| Who plays | A skater or goalie whose NHL team (Yahoo's `nhl_team`, through `canonical_team`) is the away or home team of one of tonight's games. A free agent with no NHL team never plays. An injured player's team may play: the mark says the team plays, not that he does. |
| What it shows | **Players:** a "Tonight" chip (`tonight=1`): only players on teams with a game tonight, a filter like position, applied before counts and paging; and under each such player's Team code, the game: `@BOS 7:00p` or `vs BOS`. **Rosters:** the same mark for skaters and goalies, and above the table "Tonight: 9 of 25 on teams with a game" (players outside IR slots). A line above each table: "Tonight's games (times ET)" so the time zone is visible on a phone. Only the scheduled time, never a live or final state (the cache can be hours old). |
| Schedule states | **Fresh** (the date's slate, under 6 h old), including an off day ("No games tonight."). **Stale** (the stored slate for the same date, older, because the source failed): used, with "Schedule from 7 h ago." **Unavailable** (nothing for the date): no marks, "Tonight's schedule is unavailable.", the Rosters count hidden, and a `tonight=1` link shows the normal list with that note and the chip unlit, never an empty "No players match". |
| Caching and cost | Firestore `schedule/<date>`: the date's games (four fields each) and `fetched_at`, reused for 6 hours; and `failed_at` after a failure, so every instance waits 5 minutes before asking again. In one process, concurrent misses share one call (a lock). The call has its own short budget: 3 s in total. A cache read or write failure leaves the page usable (no marks, the unavailable note). Called only by Players (full page and `/players/rows`) and Rosters' main page; not by `/rosters/replace`, League, Matchup or Admin. With a warm cache, one call per 6 hours. |
| Not in scope | Games left this week, matchup game counts, start/sit or lineup advice (SPEC §1: no recommendations), notifications, live scores. |

## 1. Strategy critique

**A. NHL public schedule, cached per date (chosen).** One small call per 6 hours, a tiny stored
document, pure matching by canonical team code. *Against:* an undocumented API; mitigated by the
seam, the tolerant parser, the three states and the note.

**B. Yahoo.** Rejected: nothing in the recorded responses carries games, and finding an
undocumented Yahoo sub-resource would need live calls the team can't make.

**C. A static season schedule committed to the repo.** Rejected: postponements and the playoffs
change it.

**D. Mark on the client from the NHL API.** Rejected: the app is server-rendered (SPEC §2) and CORS
is unknown.

## 2. What changes

- `domain/schedule.py` (pure, test-first, mutmut; imports only what `test_architecture.py`
  allows): the types `Game` (frozen: `away`, `home` — canonical codes, or the provider's code when
  `canonical_team` doesn't know it; `start_utc` — epoch seconds or none; `game_type`) and `Mark`
  (`at` | `vs`, the opponent's code, `start_utc`); `tonight_games(games) -> dict[str, Game]` (team
  code → its game, types 2 and 3 only) and `mark(team, game) -> Mark`. No dates, no time zones, no
  formatting.
- `sources/nhl/schedule.py`: `NhlScheduleSource.games_on(date) -> tuple[Game, ...]` over the shared
  `httpx` client with a 3 s total timeout. The parser reads only the `gameWeek` entry whose `date`
  is the asked date. That entry missing, or the response malformed: `ScheduleError` (unavailable),
  never another exception. The entry present with `games: []`: an empty slate (an off day). A game
  without both team codes: dropped. A team code is used as given when `canonical_team` doesn't map
  it, if it looks like a code (`^[A-Z]{2,3}$`, then logged by code); otherwise the game is dropped
  and the slate still used. `startTimeUTC` must parse as a timezone-aware time (`Z` or an offset);
  otherwise `start_utc` is none and the mark shows the opponent only.
- `ScheduleSource` Protocol in SPEC §3; a fake in tests; the demo's fake builds its games for
  whatever date it is asked (some demo teams play every day), so the demo works on any date with no
  NHL call.
- `services/schedule.py`: `tonight(repo, source, clock, *, night=None) -> Tonight` with `date`,
  `games` (team → game), `state` (`fresh` | `stale` | `unavailable`), `age` (seconds, for stale).
  `night` defaults to today's US/Eastern date (`zoneinfo`); a fragment passes the page's date. It
  reads the stored document, calls the source when needed (single-flight lock, the 5-minute
  `failed_at` backoff), stores, and catches every `ScheduleError` and `RepositoryError` into the
  unavailable or stale state.
- Players: `PlayersQuery.tonight: bool` from `tonight` (`1` or absent; anything else is a 400, as
  other bad values); the predicate in `_filtered` (`players_table.py`), so counts, the 0-GP note
  and paging follow it. Links: the chip toggles it; sort headers, season and view toggles, show/hide
  0 GP and both paging paths carry it; the filter form (Apply) carries it as a hidden field (added
  to `players.html`'s list); Team "Any" keeps it. **A new name search and Clear drop it**, like the
  other filters (the search form carries display state only; DECISIONS "Players: name search"); the
  chips then narrow the results, Tonight included. A bare `?tonight=1` keeps PR #28's default owner
  (Free Agents): `tonight` is not a search, so it doesn't turn the default to All.
- **Show more across Eastern midnight:** the page renders its schedule date into the Show more
  link (`night=YYYY-MM-DD`). `/players/rows` uses that date's schedule, so appended rows match the
  rows above. If that date is no longer today, the fragment returns one row: "It's a new night:
  reload the list." linking to the full page with the same parameters (no rows from another slate).
- `page_data` doesn't change; the Players and Rosters routes call `tonight()` themselves.
- Rosters: the marks (skaters and the goalie table), the count, the notes.
- SPEC: §1 "Added after Phase 1" (the tonight marks and chip; schedule weighting and
  recommendations still out); §2 diagram and §4 table (the NHL schedule row); §3 the seam; §7.1,
  §7.2. DECISIONS: a dated entry (source, alternatives, the new host, "tonight" in US/Eastern, the
  three states, the cache and budget).

## 3. Phases

Each phase one commit, gates green; mutmut on `domain/schedule.py` in phase 1 (every non-`killed`
status hand-checked, per the mutmut-pitfalls rules).

1. **Domain and source.** Tests first: `tonight_games` (types 2 and 3 kept, 1 dropped; both teams
   mapped), `mark` (away `@`, home `vs`, no start time). The parser against a committed fixture
   trimmed from the real response (the asked date and another, a preseason game, an unknown
   well-formed code paired with a known club — the known club's players still marked, a malformed
   code), and against: the asked date missing (unavailable) versus present with `games: []` (empty
   slate); missing `gameWeek`; a game without `awayTeam`; a start time with no offset, a bad
   offset, a non-string. respx for the HTTP path (a 500, a timeout, non-JSON, a slow answer past
   3 s).
2. **Service and screens.** The service: the Eastern date at Eastern midnight with the UTC date
   unchanged (e.g. 04:59Z → 05:00Z in standard time) and at UTC midnight with the Eastern date
   unchanged (23:59Z → 00:00Z), in both standard and daylight time; fresh, stale (with age) and
   unavailable; the 6-hour expiry; `failed_at` backoff across two service instances on one store;
   concurrent misses in one process make one call; a store read and write failure; the 3 s budget.
   Players: the chip with each owner chip and after a search; the count, the 0-GP note and both
   paging paths under the chip; Apply, Team "Any", sort, toggles carry it; a new search and Clear
   drop it; a bare `?tonight=1` is Free Agents; `tonight=2` is a 400; a free agent with no NHL team
   never shows; the unavailable state with `tonight=1` shows the normal list and the note; an off
   day shows "No games tonight."; Show more after Eastern midnight returns the reload row; the
   marks' text. Rosters: marks for skaters and goalies, the count (an injured player outside IR
   counts, an IR-slot player doesn't, a goalie does), hidden when unavailable; `TB` matches `TBL`.
   Routes that must never call the source: `/rosters/replace`, League, Matchup, Admin. The demo:
   Players and Rosters on two consecutive dates with the fake, no NHL call.
3. **Docs.** SPEC, DECISIONS, this plan's status.

## 4. Risks

| Risk | Mitigation |
|---|---|
| The NHL changes or drops the endpoint | Tolerant parser, the unavailable state and note, no error page; the seam allows another source. |
| An outage read as "nobody plays" | Three states; unavailable never filters to an empty list and hides the count. |
| A team-code mismatch hides a game | `canonical_team` on both sides; unknown well-formed codes kept; a test over every code in the fixture. |
| A slow NHL API slows Players and Rosters | 3 s budget; 6-hour cache; 5-minute shared backoff; single-flight. |
| Mixed slates across midnight | The page's date travels with Show more; a new night asks for a reload. |
| Time zone edges | Date by US/Eastern; tests at both midnights, both seasons. |

## 5. Open questions

None (team-lead's decisions above).

## 6. Review

### PT-P1 (quality-mgr, gpt-6.2-sol, at `6dfab5f`): ready with changes, 8 findings (5 Medium, 3 Low)

The report is posted verbatim on PR #30. Each finding was checked against the code; all accepted.

| # | Sev | Finding | Triage |
|---|---|---|---|
| 1 | Medium | An outage and an off day look alike ("No players match") | **Accepted.** Three states (fresh, stale, unavailable); unavailable never filters and hides the count; an off day says so (checked: `players.html:63` renders "No players match.") |
| 2 | Medium | The schedule call can spend the shared 10 s timeout, repeat on cold instances and race misses | **Accepted.** Checked: `DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)` (`client.py:31`); `/rosters/replace` calls `page_data` (`rosters.py:62`). A 3 s budget, a stored `failed_at` backoff shared by instances, a single-flight lock, store failures caught; called only by Players and Rosters' main page; "one call per 6 h" qualified as warm-cache |
| 3 | Medium | Tonight must share the row and count path; form behaviour undefined | **Accepted.** Checked: Apply's hidden-field list (`players.html:25`), the 0-GP count from `_filtered` (`players_table.py:154`). The predicate sits in `_filtered`; Apply and Team "Any" keep it; a new search and Clear drop it; a bare `?tonight=1` keeps PR #28's Free Agents default; tests |
| 4 | Medium | Show more can append another evening's rows | **Accepted.** The page's date travels in the Show more link; a new night returns a reload row; a clock-controlled test |
| 5 | Medium | The parser can cache source drift or an unknown team as "no games" | **Accepted.** A missing asked date is unavailable, `games: []` is an off day; an unknown well-formed code is kept (the known club's players still marked); a start time must be timezone-aware, else no time is shown; tests for each |
| 6 | Low | Labels imply the player plays; ET only on hover; cached live/final states | **Accepted.** "on teams with a game"; a visible "(times ET)" line; scheduled time only, no live or final state; count rules for injured, IR and goalies tested; goalie table marked |
| 7 | Low | `Game` in `sources/` would make `domain/` import it | **Accepted.** Checked: the architecture test allows only `fha.domain` (`test_architecture.py:36`). The types live in `domain/schedule.py`; the adapter builds them |
| 8 | Low | The date tests don't separate the Eastern and UTC boundaries; the demo's fake must follow the date | **Accepted.** Tests at each midnight with the other date fixed, in standard and daylight time; the demo's fake builds games for any date (checked: the demo uses `SystemClock`, `context.py:250`) |

The plan is approved after this triage (team-lead, 2026-11-27).
