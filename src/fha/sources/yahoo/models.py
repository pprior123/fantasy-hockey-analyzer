"""What the Yahoo source returns (SPEC §4): plain, immutable, Yahoo-shaped.

The refresh service (M3) turns these into the engine's ``PlayerSeason``
through ``StatMap`` (see ``stat_map.py``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

IR_SLOTS = frozenset({"IR", "IR+"})


@dataclass(frozen=True)
class Game:
    """One season of Yahoo's NHL game. ``game_key`` changes every season."""

    game_key: str
    season: int


@dataclass(frozen=True)
class StatCategory:
    stat_id: str
    name: str
    display_name: str
    position_types: frozenset[str]  # "P" (skaters), "G" (goalies)
    is_only_display: bool = False  # shown by Yahoo but not scored


@dataclass(frozen=True)
class RosterSlot:
    position: str  # "C", "LW", "D", "G", "BN", "IR", "IR+", ...
    count: int
    is_starting: bool


@dataclass(frozen=True)
class LeagueSettings:
    league_key: str
    num_teams: int
    season: int
    current_week: int
    start_week: int
    end_week: int
    stat_categories: tuple[StatCategory, ...]  # the league's categories
    roster_slots: tuple[RosterSlot, ...]


@dataclass(frozen=True)
class Player:
    player_key: str  # "{game_key}.p.{player_id}"
    player_id: str  # stable across seasons
    name: str
    nhl_team: str  # Yahoo's abbreviation, e.g. "TB"; "" for a free agent
    display_position: str  # e.g. "C,LW"
    eligible_positions: tuple[str, ...]
    position_type: str  # "P" or "G"
    status: str | None = None  # injury designation, e.g. "IR", "DTD"

    @property
    def is_goalie(self) -> bool:
        return self.position_type == "G"


@dataclass(frozen=True)
class RosterEntry:
    player: Player
    selected_position: str  # the slot the player sits in today

    @property
    def in_ir_slot(self) -> bool:
        return self.selected_position in IR_SLOTS


@dataclass(frozen=True)
class Team:
    team_key: str
    name: str
    is_mine: bool  # Yahoo's is_owned_by_current_login
    roster: tuple[RosterEntry, ...]


@dataclass(frozen=True)
class StatLine:
    """One player's season totals as Yahoo sends them: raw strings by stat ID.

    Yahoo writes ``"-"`` for a stat with no value (e.g. a skater's GAA, or
    anything before the player's first game).
    """

    player_key: str
    season: int
    values: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Matchup:
    week: int
    team_keys: tuple[str, str]


@dataclass(frozen=True)
class Scoreboard:
    week: int
    week_start: str | None  # ISO date, from the matchups; None with no matchups
    week_end: str | None
    matchups: tuple[Matchup, ...]

    def opponent(self, team_key: str) -> str | None:
        """The team ``team_key`` plays this week, or None (bye / not scheduled)."""
        for matchup in self.matchups:
            first, second = matchup.team_keys
            if team_key == first:
                return second
            if team_key == second:
                return first
        return None


@dataclass(frozen=True)
class LeagueSnapshot:
    """Everything one full refresh reads from Yahoo."""

    game: Game
    settings: LeagueSettings
    game_stat_categories: tuple[StatCategory, ...]  # every NHL stat Yahoo tracks
    teams: tuple[Team, ...]
    available: tuple[Player, ...]  # top available (free agents + waivers), by rank
    stats: Mapping[str, StatLine]  # current season, by player_key, whole pool
    last_season_stats: Mapping[str, StatLine] | None  # None when not fetched
    scoreboard: Scoreboard  # current week
    next_scoreboard: Scoreboard | None  # None after the last week

    @property
    def my_team(self) -> Team | None:
        return next((t for t in self.teams if t.is_mine), None)

    @property
    def pool(self) -> tuple[Player, ...]:
        """Rostered players, then available ones not already rostered."""
        rostered = [entry.player for team in self.teams for entry in team.roster]
        seen = {p.player_key for p in rostered}
        return (*rostered, *(p for p in self.available if p.player_key not in seen))

    def owner_of(self, player_key: str) -> Team | None:
        for team in self.teams:
            if any(entry.player.player_key == player_key for entry in team.roster):
                return team
        return None
