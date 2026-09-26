"""Team profiles, matchup comparison and need_score (SPEC §5). Pure.

Built on the engine's per-player norms::

    profile_players(team) = the team's rated skaters, excluding IR / IR+ slots
    mean_norm(team, cat)  = mean of norm(cat, p) over profile_players(team)
    sd_norm(team, cat)    = population std dev of the same values
    team_ttltst(team)     = mean TTLTST over profile_players(team)
    matchup(me, opp, cat) = mean_norm(me, cat) - mean_norm(opp, cat)
    trailing(cat)         = matchup(me, opp, cat) < close_margin   # "behind or close"
    need_score(p)         = sum of norm(cat, p) over trailing categories

A team with no profile players has None for every mean, std dev and its
TTLTST, and no category is trailing against or for it. These compare
profiles (rates), not projected weekly totals.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from fha.domain.engine import Rating
from fha.domain.models import SKATER_CATEGORIES, Category


@dataclass(frozen=True)
class ProfileConfig:
    # "Behind or close": a category trails when me - opp is below this. Finite and
    # >= 0: 0 means strictly behind; a negative margin would stop counting a level
    # category as trailing, which SPEC's "behind or close" rules out.
    close_margin: float = 0.05
    categories: tuple[Category, ...] = SKATER_CATEGORIES  # the engine's categories

    def __post_init__(self) -> None:
        margin = self.close_margin
        if isinstance(margin, bool) or not isinstance(margin, int | float):
            raise ValueError(f"close_margin must be a number, got {margin!r}")
        if not math.isfinite(margin) or margin < 0:
            raise ValueError(f"close_margin must be finite and >= 0, got {margin}")
        if not self.categories or len(set(self.categories)) != len(self.categories):
            raise ValueError(f"categories must be non-empty and unique, got {self.categories}")


DEFAULT_PROFILE_CONFIG = ProfileConfig()


@dataclass(frozen=True)
class Member:
    """One roster entry: the player, whether he sits in an IR / IR+ slot, and his
    rating (None for a goalie, or a player the engine didn't rate at all)."""

    player_id: str
    in_ir_slot: bool
    rating: Rating | None


@dataclass(frozen=True)
class TeamProfile:
    players: tuple[Rating, ...]  # the profile players
    mean_norm: Mapping[Category, float | None]
    sd_norm: Mapping[Category, float | None]
    ttltst: float | None
    categories: tuple[Category, ...] = field(default=SKATER_CATEGORIES)

    @property
    def size(self) -> int:
        return len(self.players)


@dataclass(frozen=True)
class Matchup:
    diff: Mapping[Category, float | None]  # me - opp; None when either team is empty
    trailing: frozenset[Category]


def profile_players(members: Iterable[Member]) -> tuple[Rating, ...]:
    """The team's rated skaters outside IR / IR+ slots, in roster order."""
    seen: set[str] = set()
    players = []
    for m in members:
        if m.player_id in seen:
            raise ValueError(f"player {m.player_id} is listed twice")
        seen.add(m.player_id)
        if not m.in_ir_slot and m.rating is not None and m.rating.rated:
            players.append(m.rating)
    return tuple(players)


def team_profile(
    members: Iterable[Member], config: ProfileConfig = DEFAULT_PROFILE_CONFIG
) -> TeamProfile:
    players = profile_players(members)
    cats = config.categories
    if not players:
        empty: dict[Category, float | None] = dict.fromkeys(cats)
        return TeamProfile((), empty, dict(empty), None, cats)
    columns = {cat: [_norm(p, cat) for p in players] for cat in cats}
    return TeamProfile(
        players=players,
        mean_norm={cat: statistics.fmean(v) for cat, v in columns.items()},
        sd_norm={cat: statistics.pstdev(v) for cat, v in columns.items()},
        ttltst=statistics.fmean(p.ttltst for p in players if p.ttltst is not None),
        categories=cats,
    )


def compare(
    me: TeamProfile, opp: TeamProfile, config: ProfileConfig = DEFAULT_PROFILE_CONFIG
) -> Matchup:
    """Per-category me - opp, and the categories where I'm behind or close."""
    if me.categories != opp.categories:
        raise ValueError("the teams were profiled on different categories")
    diff: dict[Category, float | None] = {}
    for cat in me.categories:
        mine, theirs = me.mean_norm[cat], opp.mean_norm[cat]
        diff[cat] = None if mine is None or theirs is None else mine - theirs
    trailing = frozenset(
        cat for cat, d in diff.items() if d is not None and d < config.close_margin
    )
    return Matchup(diff, trailing)


def need_score(rating: Rating, trailing: Iterable[Category]) -> float | None:
    """How much a player adds where I trail: his norms summed over those categories.

    None for an unrated player (no norms: nothing to rank him by, and 0 would
    rank him with players who genuinely add nothing).
    """
    if not rating.rated:
        return None
    return math.fsum(_norm(rating, cat) for cat in trailing)


def _norm(rating: Rating, cat: Category) -> float:
    if cat not in rating.norms:
        raise ValueError(f"rating for {rating.player_id} has no {cat} norm")
    return rating.norms[cat]
