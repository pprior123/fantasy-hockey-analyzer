"""Domain models for the metric engine (SPEC §5)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class Category(StrEnum):
    """The seven skater scoring categories that make up TTLTST."""

    G = "G"
    A = "A"
    PPP = "PPP"
    PIM = "PIM"
    HIT = "HIT"
    SOG = "SOG"
    BLK = "BLK"


SKATER_CATEGORIES: tuple[Category, ...] = tuple(Category)


@dataclass(frozen=True)
class PlayerSeason:
    """One player's season totals, the engine's only input.

    ``stats`` holds season totals per category (PPP already summed from PPG +
    PPA where the source splits them). ``aav`` is the cap hit in dollars, or
    None when unknown.
    """

    player_id: str
    name: str
    gp: int
    stats: Mapping[Category, float]
    is_goalie: bool = False
    aav: int | None = None

    def __post_init__(self) -> None:
        if self.gp < 0:
            raise ValueError(f"{self.player_id}: gp must be >= 0, got {self.gp}")
        for cat, total in self.stats.items():
            if not math.isfinite(total):
                raise ValueError(f"{self.player_id}: {cat} must be finite, got {total}")
            if total < 0:
                raise ValueError(f"{self.player_id}: {cat} is negative ({total})")
        if self.aav is not None and self.aav < 0:
            raise ValueError(f"{self.player_id}: aav must be >= 0, got {self.aav}")
