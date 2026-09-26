"""A synthetic league for local dev: the app runs before Yahoo access exists (M4).

``demo_snapshot()`` builds a deterministic ``LeagueSnapshot`` shaped like the
real one: 8 teams of 27 (every slot, IR and IR+ included), ~300 free agents,
both seasons' stat lines under Yahoo's stat IDs, and the current and next
week's scoreboards. Names are made up from syllables, never real people's;
NHL-style team codes are used. Wired by ``FHA_DEMO=1`` (``fha.web.context``).
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import replace

from fha.sources.yahoo.models import (
    Game,
    LeagueSettings,
    LeagueSnapshot,
    Matchup,
    Player,
    RosterEntry,
    RosterSlot,
    Scoreboard,
    StatCategory,
    StatLine,
    Team,
)

GAME_KEY = "465"
SEASON = 2026
LEAGUE_KEY = f"{GAME_KEY}.l.8076"
TEAMS = 8
FREE_AGENTS = 300
WEEK = 3  # early season: the current season's GP is small (the baseline view applies)
GAMES_SO_FAR = 9

NHL = [
    "Ana",
    "Bos",
    "Buf",
    "Cgy",
    "Car",
    "Chi",
    "Col",
    "CBJ",
    "Dal",
    "Det",
    "Edm",
    "Fla",
    "LA",
    "Min",
    "Mon",
    "Nsh",
    "NJ",
    "NYI",
    "NYR",
    "Ott",
    "Phi",
    "Pit",
    "SJ",
    "Sea",
    "StL",
    "TB",
    "Tor",
    "Uta",
    "Van",
    "VGK",
    "Was",
    "Wpg",
]

# (stat_id, name, display_name, position types): the league's categories first.
_LEAGUE = [
    ("1", "Goals", "G", "P"),
    ("2", "Assists", "A", "P"),
    ("8", "Powerplay Points", "PPP", "P"),
    ("5", "Penalty Minutes", "PIM", "P"),
    ("31", "Hits", "HIT", "P"),
    ("14", "Shots on Goal", "SOG", "P"),
    ("32", "Blocks", "BLK", "P"),
    ("19", "Wins", "W", "G"),
    ("23", "Goals Against Average", "GAA", "G"),
    ("26", "Save Percentage", "SV%", "G"),
]
_GAME_ONLY = [
    ("0", "Games Played", "GP", "P"),
    ("6", "Powerplay Goals", "PPG", "P"),
    ("7", "Powerplay Assists", "PPA", "P"),
    ("29", "Games Played", "GP", "G"),
]
# 16 active slots, 9 bench, IR, IR+: 27 players a team.
SLOTS = (("C", 3), ("LW", 3), ("RW", 3), ("D", 6), ("G", 1), ("BN", 9), ("IR", 1), ("IR+", 1))
ROSTER = ("C",) * 5 + ("LW",) * 5 + ("RW",) * 5 + ("D",) * 8 + ("G",) * 4
OPENING_DAY = dt.date(2026, 10, 5)  # a Monday: week 1 starts here

_FIRST = [
    "Arlo",
    "Bram",
    "Cade",
    "Dax",
    "Ezra",
    "Finn",
    "Gage",
    "Hollis",
    "Ivo",
    "Jory",
    "Kellan",
    "Lars",
    "Milo",
    "Nash",
    "Oren",
    "Pax",
    "Quill",
    "Rook",
    "Soren",
    "Tate",
    "Ulf",
    "Vance",
    "Wren",
    "Yael",
    "Zeke",
    "Anders",
    "Beck",
    "Cyrus",
    "Dorian",
    "Emrys",
]
_START = [
    "Bal",
    "Cor",
    "Dru",
    "Fen",
    "Gar",
    "Hol",
    "Ist",
    "Jar",
    "Kel",
    "Lom",
    "Mar",
    "Nor",
    "Orr",
    "Pel",
    "Quen",
    "Ros",
    "Sta",
    "Tor",
    "Vel",
    "Wyn",
]
_END = [
    "berg",
    "by",
    "croft",
    "dale",
    "ford",
    "gard",
    "holm",
    "ley",
    "more",
    "ov",
    "rick",
    "sen",
    "son",
    "stad",
    "ton",
    "vik",
    "wood",
]


def _name(rng: random.Random, taken: set[str]) -> str:
    while True:
        name = f"{rng.choice(_FIRST)} {rng.choice(_START)}{rng.choice(_END)}"
        if name not in taken:
            taken.add(name)
            return name


def _categories(rows: list[tuple[str, str, str, str]]) -> tuple[StatCategory, ...]:
    return tuple(StatCategory(i, n, d, frozenset(t)) for i, n, d, t in rows)


def _skater_line(rng: random.Random, pos: str, quality: float, gp: int) -> dict[str, str]:
    d = pos == "D"
    rates = {  # per game at quality 1.0
        "1": 0.12 if d else 0.42,
        "2": 0.40 if d else 0.55,
        "6": 0.03 if d else 0.12,
        "7": 0.12 if d else 0.18,
        "5": 0.5,
        "31": 1.9 if d else 1.3,
        "14": 1.8 if d else 3.0,
        "32": 1.7 if d else 0.5,
    }
    values = {"0": str(gp)}
    for stat_id, rate in rates.items():
        scale = quality if stat_id in ("1", "2", "6", "7", "14") else 0.6 + 0.4 * quality
        values[stat_id] = str(round(rate * scale * gp * rng.uniform(0.7, 1.3)))
    values["8"] = str(int(values["6"]) + int(values["7"]))
    return values if gp else {k: "-" for k in values} | {"0": "0"}


def _goalie_line(rng: random.Random, quality: float, gp: int) -> dict[str, str]:
    if not gp:
        return {"29": "0", "19": "-", "23": "-", "26": "-"}
    wins = round(gp * (0.35 + 0.3 * quality) * rng.uniform(0.8, 1.2))
    gaa = 3.4 - 1.0 * quality + rng.uniform(-0.2, 0.2)
    sv = 0.885 + 0.03 * quality + rng.uniform(-0.004, 0.004)
    return {"29": str(gp), "19": str(min(wins, gp)), "23": f"{gaa:.2f}", "26": f"{sv:.3f}"[1:]}


def demo_snapshot(seed: int = 8076) -> LeagueSnapshot:
    """The same synthetic league every time for a given ``seed``."""
    rng = random.Random(seed)  # noqa: S311 - demo data, not security
    taken: set[str] = set()
    next_id = iter(range(9000, 100_000))
    quality: dict[str, float] = {}

    def player(pos: str, q: float, status: str | None = None) -> Player:
        pid = str(next(next_id))
        quality[pid] = q
        eligible = (pos,) if pos in ("C", "D", "G") or rng.random() < 0.7 else (pos, "C")
        return Player(
            player_key=f"{GAME_KEY}.p.{pid}",
            player_id=pid,
            name=_name(rng, taken),
            nhl_team=rng.choice(NHL),
            display_position=",".join(eligible),
            eligible_positions=eligible,
            position_type="G" if pos == "G" else "P",
            status=status,
        )

    teams = []
    for t in range(1, TEAMS + 1):
        members = [player(pos, rng.uniform(0.45, 1.0)) for pos in ROSTER]
        skaters = [m for m in members if m.position_type == "P"]
        injured = {skaters[-2].player_id: "IR", skaters[-1].player_id: "IR+"}
        roster: list[RosterEntry] = []
        open_slots = dict(SLOTS)
        for m in members:
            if m.player_id in injured:
                roster.append(RosterEntry(replace(m, status="IR"), injured[m.player_id]))
                continue
            primary = m.eligible_positions[0]
            slot = primary if open_slots.get(primary, 0) > 0 else "BN"
            open_slots[slot] -= 1
            roster.append(RosterEntry(m, slot))
        teams.append(Team(f"{LEAGUE_KEY}.t.{t}", f"Team {t}", t == 1, tuple(roster)))

    positions = ("C", "LW", "RW", "D", "D", "G")
    available = tuple(
        player(
            rng.choice(positions), rng.uniform(0.05, 0.7), "DTD" if rng.random() < 0.05 else None
        )
        for _ in range(FREE_AGENTS)
    )

    pool = [e.player for team in teams for e in team.roster] + list(available)
    stats, last = {}, {}
    for p in pool:
        q = quality[p.player_id]
        rookie = rng.random() < 0.08
        gp_now = rng.randint(0, GAMES_SO_FAR) if p.position_type == "P" else rng.randint(0, 4)
        gp_last = 0 if rookie else rng.randint(10, 82 if p.position_type == "P" else 60)
        pos = p.eligible_positions[0]
        if p.position_type == "G":
            now, before = _goalie_line(rng, q, gp_now), _goalie_line(rng, q, gp_last)
        else:
            now, before = _skater_line(rng, pos, q, gp_now), _skater_line(rng, pos, q, gp_last)
        stats[p.player_key] = StatLine(p.player_key, SEASON, now)
        last[p.player_key] = StatLine(p.player_key, SEASON - 1, before)

    keys = [t.team_key for t in teams]

    def board(week: int) -> Scoreboard:
        order = keys[:1] + [keys[1 + (i + week) % (TEAMS - 1)] for i in range(TEAMS - 1)]
        pairs = tuple(Matchup(week, (order[i], order[TEAMS - 1 - i])) for i in range(TEAMS // 2))
        start = OPENING_DAY + dt.timedelta(weeks=week - 1)
        end = start + dt.timedelta(days=6)
        return Scoreboard(week, start.isoformat(), end.isoformat(), pairs)

    settings = LeagueSettings(
        league_key=LEAGUE_KEY,
        num_teams=TEAMS,
        season=SEASON,
        current_week=WEEK,
        start_week=1,
        end_week=25,
        stat_categories=_categories(_LEAGUE),
        roster_slots=tuple(
            RosterSlot(slot, n, slot not in ("BN", "IR", "IR+")) for slot, n in SLOTS
        ),
    )
    return LeagueSnapshot(
        game=Game(GAME_KEY, SEASON),
        settings=settings,
        game_stat_categories=_categories([*_GAME_ONLY, *_LEAGUE]),
        teams=tuple(teams),
        available=available,
        stats=stats,
        last_season_stats=last,
        scoreboard=board(WEEK),
        next_scoreboard=board(WEEK + 1),
    )
