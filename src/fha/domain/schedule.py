"""Game assignments and opponent marks for an NHL schedule slate."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Game:
    away: str
    home: str
    start_utc: float | None
    game_type: int


@dataclass(frozen=True)
class Mark:
    venue: Literal["at", "vs"]
    opponent: str
    start_utc: float | None


def tonight_games(games: Iterable[Game]) -> dict[str, Game]:
    """Index the slate's regular-season and playoff games by both team codes."""
    by_team: dict[str, Game] = {}
    for game in games:
        if game.game_type in (2, 3):
            by_team[game.away] = game
            by_team[game.home] = game
    return by_team


def mark(team: str, game: Game) -> Mark:
    """Describe the matchup for a participating team, using its canonical code."""
    if team == game.away:
        return Mark("at", game.home, game.start_utc)
    return Mark("vs", game.away, game.start_utc)
