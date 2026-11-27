# Plan: players who play tonight

Status: draft, 2026-11-27, awaiting quality-mgr's plan review (task PT-P1). The owner needs no
review of plans (2026-11-27); team-lead approves after the review's triage.

The owner's idea (2026-11-27): show which players play tonight. Team-lead decides the source and
what "tonight" shows (work queue item 3; the owner asked that design choices not wait on him).

## 0. Decisions

| Question | Decision |
|---|---|
| Source | The NHL's public schedule, `GET https://api-web.nhle.com/v1/schedule/{YYYY-MM-DD}` (no key, no auth). Checked 2026-11-27: JSON with `gameWeek[]` (`date`, `games[]`), each game with `startTimeUTC`, `gameState` (`FUT`, `LIVE`, `OFF`, `FINAL`), `gameType`, `awayTeam.abbrev`, `homeTeam.abbrev`. Yahoo is not used: the recorded Yahoo responses (league, rosters, players, scoreboard; `tests/fixtures/yahoo/`) carry no game, opponent or schedule field, and the app's Yahoo use stays as it is. |
| A new outbound host | Yes, the first besides Yahoo and Google. Behind a new seam (`ScheduleSource`, SPEC §3), mocked from a committed fixture in tests (SPEC §9; no live network). The endpoint is undocumented: when it fails or changes shape, the pages show no tonight marks and a one-line note, never an error page. |
| "Tonight" | Today's date in US/Eastern (the NHL's schedule date), from the `Clock`, so a West-coast late game still counts on its night. All of that date's games with `gameType` 2 (regular season) or 3 (playoffs); preseason (1) and others are ignored, as Yahoo's stats ignore them. |
| Who plays | A skater or goalie whose NHL team (Yahoo's `nhl_team`, through `canonical_team`) is the away or home team (the NHL's `abbrev`, through `canonical_team`) of one of tonight's games. A free agent with no NHL team never plays. An injured player is still marked (his team plays); his Yahoo status shows next to it as today. |
| What it shows | **Players:** a "Tonight" chip that keeps only players who play tonight, combined with every other filter (owner chip, team, position, min GP, search); and under the Team code of each player who plays, the game: `@BOS 7:00p` or `vs BOS`, or `live` / `final` once started. **Rosters:** the same mark per player, and above the table "Tonight: 9 of 25 play" (the team's players outside IR whose team plays). |
| Time shown | The start time in US/Eastern with "ET" in the chip's title (the owner's league is North American; the app has no user timezone). Short: `7:00p`. |
| Caching | One request per date, stored in Firestore (`schedule/<date>`: only the date's games, the five fields above), reused for 6 hours (start times rarely move). Live states (`LIVE`, `final`) are as fresh as the cache; the mark is a guide, not a scoreboard. After a failure, the source is not asked again for 5 minutes (as the Yahoo refresh does). Not part of the Yahoo refresh: one small call, independent of its TTL. |
| Not in scope | Games left this week, matchup games-played counts, start/sit or lineup advice (SPEC §1: no recommendations), notifications. |

## 1. Strategy critique

**A. NHL public schedule, cached per date (chosen).** One call per 6 hours, a tiny stored document,
pure matching by canonical team code. *Against:* an undocumented API; mitigated by the seam, the
tolerant parser and the note.

**B. Yahoo.** Rejected: nothing in the recorded responses carries games, and finding an
undocumented Yahoo sub-resource would need live calls the team can't make (Yahoo runs on the
owner's token in production only).

**C. A static season schedule committed to the repo.** Rejected: postponements and the playoffs
change it, and it would need refreshing by hand.

**D. Mark on the client from the NHL API.** Rejected: the app is server-rendered (SPEC §2) and CORS
is unknown.

## 2. What changes

- `sources/nhl/schedule.py`: `NhlScheduleSource.games_on(date) -> tuple[Game, ...]` over the shared
  `httpx` client (timeout as the other sources). The parser reads only the five fields, from the
  `gameWeek` entry whose `date` is the asked date; a missing entry means no games; a malformed
  response raises `ScheduleError` (never a raw `KeyError`). `Game` (frozen): `away`, `home`
  (canonical codes; a code `canonical_team` doesn't know drops the game and is logged by code),
  `start_utc` (epoch seconds), `state` (`upcoming` | `live` | `final`, from `FUT`/`PRE` |
  `LIVE`/`CRIT` | `OFF`/`FINAL`; anything else: `upcoming`), `game_type`.
- `ScheduleSource` Protocol in SPEC §3's list; a fake in tests; the demo uses a fixed fake
  schedule that plays some demo teams tonight, so the demo shows the feature without the network.
- `domain/schedule.py` (pure, test-first, mutmut): `tonight_games(games) -> dict[str, Game]` (team
  code → its game, regular season and playoffs only) and `opponent(team, game) -> Mark`
  (`at`/`vs`, the other code, state, start) for the page.
- `services/schedule.py`: `tonight(repo, source, clock) -> Tonight` (the date, the team→game map,
  and a note when unavailable): today's US/Eastern date from the clock (`zoneinfo`, stdlib), the
  stored document if under 6 h old, else the source, stored; on failure, the stored copy if any
  (with a note), else empty with a note; a 5-minute backoff in memory per process.
- `page_data` gains the `Tonight` (Players and Rosters only, to keep the call off the other pages);
  failure never raises.
- Players: `PlayersQuery.tonight: bool` from `tonight=1`; the chip; the filter; the mark in the
  Team cell (`_player_rows.html`); every link that carries filters carries `tonight` (sort, toggles,
  Apply, paging both ways, Clear drops it with the other filters).
- Rosters: the mark and the count.
- SPEC: §2 diagram and §4 table (the NHL schedule row); §3 the seam; §7.1, §7.2. DECISIONS: a dated
  entry (source, alternatives, the new host, "tonight" in US/Eastern, the cache). README if it lists
  outbound hosts.

## 3. Phases

Each phase one commit, gates green; mutmut on `domain/schedule.py` in phase 1 (every non-`killed`
status hand-checked).

1. **Domain and source.** Tests first: `tonight_games` (types 2 and 3 kept, 1 dropped; both teams
   mapped; a double-header impossible but the first game kept), `opponent` (away `@`, home `vs`,
   each state). The parser against a committed fixture trimmed from the real response (two dates,
   one game of each state, a preseason game, an unknown code), and against malformed shapes
   (missing `gameWeek`, a game without `awayTeam`, a non-string time): `ScheduleError` or the game
   dropped, never another exception. respx for the HTTP path (a 500, a timeout, non-JSON).
2. **Service and screens.** The service: the Eastern date at 23:30 ET and 00:30 ET (UTC across
   midnight), the cache hit, the 6-hour expiry, the failure with and without a stored copy, the
   backoff (no second call within 5 minutes). Players: the chip filters with each owner chip and a
   search; the mark's text for each state; every ownerless or owner link keeps `tonight=1`; a
   free agent with no NHL team never shows under the chip; the note when unavailable. Rosters: the
   marks and the count (IR excluded); a Yahoo code like `TB` matches the NHL's `TBL`.
3. **Docs.** SPEC, DECISIONS, this plan's status.

## 4. Risks

| Risk | Mitigation |
|---|---|
| The NHL changes or drops the endpoint | Tolerant parser, the note, no error page; the seam allows another source. |
| A team-code mismatch hides a game | `canonical_team` on both sides; a test over every code in the fixture; unknown codes logged. |
| A slow NHL API slows Players and Rosters | The shared client's timeout; 6-hour cache; 5-minute backoff. One call at most per 6 hours. |
| Time zone edge (a game after midnight UTC) | Date by US/Eastern; tests across UTC midnight. |

## 5. Open questions

None (team-lead's decisions above; the review may challenge them).

## 6. Review

(quality-mgr, task PT-P1: pending.)
