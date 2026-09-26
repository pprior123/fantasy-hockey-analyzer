"""Synthetic Yahoo ``?format=json`` responses, shaped like the real API.

Each builder returns a response's ``fantasy_content``; ``envelope`` wraps it
as the full body. Names are made up. Stat IDs follow Yahoo's NHL numbering
as far as we know it; the recorded fixtures (tests/fixtures/yahoo/) are the
real reference.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

GAME_KEY = "465"
SEASON = 2026
LEAGUE_KEY = f"{GAME_KEY}.l.8076"

# (stat_id, name, display_name, position_type)
LEAGUE_CATEGORIES: list[tuple[str, str, str, str]] = [
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
GAME_CATEGORIES: list[tuple[str, str, str, str]] = [
    ("0", "Games Played", "GP", "P"),
    *[c for c in LEAGUE_CATEGORIES if c[3] == "P"],
    ("6", "Powerplay Goals", "PPG", "P"),
    ("7", "Powerplay Assists", "PPA", "P"),
    ("29", "Games Played", "GP", "G"),
    *[c for c in LEAGUE_CATEGORIES if c[3] == "G"],
]
ROSTER_SLOTS: list[tuple[str, int, int]] = [
    ("C", 3, 1),
    ("LW", 3, 1),
    ("RW", 3, 1),
    ("D", 6, 1),
    ("G", 1, 1),
    ("BN", 4, 0),
    ("IR", 1, 0),
    ("IR+", 1, 0),
]


def envelope(content: Mapping[str, Any]) -> dict[str, Any]:
    return {"fantasy_content": {"xml:lang": "en-US", **content, "refresh_rate": "60"}}


def collection(item: str, items: Sequence[Any]) -> Any:
    if not items:
        return []
    out: dict[str, Any] = {str(i): {item: x} for i, x in enumerate(items)}
    out["count"] = len(items)
    return out


def games(*seasons: tuple[str, int]) -> dict[str, Any]:
    return {
        "games": collection(
            "game",
            [
                [
                    {
                        "game_key": key,
                        "game_id": key,
                        "name": "Hockey",
                        "code": "nhl",
                        "season": str(s),
                    }
                ]
                for key, s in seasons
            ],
        )
    }


def league_meta(league_key: str = LEAGUE_KEY, **overrides: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "league_key": league_key,
        "league_id": league_key.rsplit(".", 1)[-1],
        "name": "Synthetic League",
        "num_teams": 8,
        "current_week": 1,
        "start_week": "1",
        "end_week": "25",
        "game_code": "nhl",
        "season": str(SEASON),
    }
    return meta | overrides


def stat_entry(stat_id: str, name: str, display: str, pos_type: str, **extra: Any) -> Any:
    return {
        "stat": {
            "stat_id": int(stat_id),
            "enabled": "1",
            "name": name,
            "display_name": display,
            "sort_order": "1",
            "position_type": pos_type,
            "stat_position_types": [{"stat_position_type": {"position_type": pos_type}}],
            **extra,
        }
    }


def league_settings(
    categories: Sequence[tuple[str, str, str, str]] = LEAGUE_CATEGORIES,
    slots: Sequence[tuple[str, int, int]] = ROSTER_SLOTS,
    **meta: Any,
) -> dict[str, Any]:
    settings = {
        "draft_type": "live",
        "roster_positions": [
            {
                "roster_position": {
                    "position": pos,
                    "position_type": "G" if pos == "G" else "P",
                    "count": count,
                    "is_starting_position": starting,
                }
            }
            for pos, count, starting in slots
        ],
        "stat_categories": {"stats": [stat_entry(*c) for c in categories]},
    }
    return {"league": [league_meta(**meta), {"settings": [settings]}]}


def game_stat_categories(
    categories: Sequence[tuple[str, str, str, str]] = GAME_CATEGORIES,
) -> dict[str, Any]:
    stats = [
        {
            "stat": {
                "stat_id": int(sid),
                "name": name,
                "display_name": display,
                "sort_order": "1",
                "position_types": [{"position_type": pos}],
            }
        }
        for sid, name, display, pos in categories
    ]
    return {
        "game": [
            {"game_key": GAME_KEY, "code": "nhl", "season": str(SEASON)},
            {"stat_categories": {"stats": stats}},
        ]
    }


@dataclass(frozen=True)
class P:
    """A synthetic player."""

    pid: str
    name: str
    team: str = "TB"
    pos: str = "C"
    status: str | None = None
    game_key: str = GAME_KEY
    stats: Mapping[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.game_key}.p.{self.pid}"

    @property
    def position_type(self) -> str:
        return "G" if self.pos == "G" else "P"

    def meta(self) -> list[Any]:
        first, _, last = self.name.partition(" ")
        fragments: list[Any] = [
            {"player_key": self.key},
            {"player_id": self.pid},
            {"name": {"full": self.name, "first": first, "last": last}},
            {"url": f"https://sports.yahoo.com/nhl/players/{self.pid}"},
            {"editorial_player_key": f"nhl.p.{self.pid}"},
            {"editorial_team_abbr": self.team},
            {"display_position": self.pos},
            {"position_type": self.position_type},
            {"primary_position": self.pos.split(",")[0]},
            {"eligible_positions": [{"position": p} for p in self.pos.split(",")]},
            [],
        ]
        if self.status:
            fragments[5:5] = [{"status": self.status}, {"status_full": "Injured"}]
        return fragments


def team_meta(team_key: str, name: str, is_mine: bool = False) -> list[Any]:
    meta: list[Any] = [
        {"team_key": team_key},
        {"team_id": team_key.rsplit(".", 1)[-1]},
        {"name": name},
        [],
        {"url": f"https://hockey.fantasysports.yahoo.com/hockey/8076/{team_key[-1]}"},
        {"team_logos": [{"team_logo": {"size": "large", "url": "https://example.test/l.png"}}]},
        [],
        {"number_of_moves": 3},
        {
            "managers": [
                {
                    "manager": {
                        "manager_id": "1",
                        "nickname": "Someone",
                        "guid": "GUIDGUIDGUID",
                        "email": "someone@example.com",
                    }
                }
            ]
        },
    ]
    if is_mine:
        meta.insert(3, {"is_owned_by_current_login": 1})
    return meta


@dataclass(frozen=True)
class T:
    """A synthetic team: (player, slot) pairs."""

    team_id: int
    roster: Sequence[tuple[P, str]] = ()
    is_mine: bool = False
    name: str = ""

    @property
    def key(self) -> str:
        return f"{LEAGUE_KEY}.t.{self.team_id}"


def teams_roster(teams: Sequence[T]) -> dict[str, Any]:
    items = []
    for team in teams:
        players = [
            [
                player.meta(),
                {"selected_position": [{"coverage_type": "date"}, {"position": slot}]},
                {"is_editable": 0},
            ]
            for player, slot in team.roster
        ]
        roster = {
            "coverage_type": "date",
            "date": "2026-10-07",
            "0": {"players": collection("player", players)},
        }
        items.append(
            [
                team_meta(team.key, team.name or f"Name {team.team_id}", team.is_mine),
                {"roster": roster},
            ]
        )
    return {"league": [league_meta(), {"teams": collection("team", items)}]}


def league_players(players: Sequence[P]) -> dict[str, Any]:
    return {
        "league": [
            league_meta(),
            {"players": collection("player", [[p.meta()] for p in players])},
        ]
    }


def player_stats(players: Sequence[P], season: int = SEASON) -> dict[str, Any]:
    items = [
        [
            p.meta(),
            {
                "player_stats": {
                    "0": {"coverage_type": "season", "season": str(season)},
                    "stats": [{"stat": {"stat_id": k, "value": v}} for k, v in p.stats.items()],
                }
            },
        ]
        for p in players
    ]
    return {"players": collection("player", items)}


def scoreboard(
    week: int, pairs: Sequence[tuple[int, int]], start: str = "2026-10-05", end: str = "2026-10-11"
) -> dict[str, Any]:
    matchups = [
        {
            "week": str(week),
            "week_start": start,
            "week_end": end,
            "status": "preevent",
            "0": {
                "teams": collection(
                    "team",
                    [
                        [team_meta(f"{LEAGUE_KEY}.t.{a}", f"Name {a}"), {"team_points": {}}],
                        [team_meta(f"{LEAGUE_KEY}.t.{b}", f"Name {b}"), {"team_points": {}}],
                    ],
                )
            },
        }
        for a, b in pairs
    ]
    return {
        "league": [
            league_meta(),
            {"scoreboard": {"0": {"matchups": collection("matchup", matchups)}, "week": str(week)}},
        ]
    }
