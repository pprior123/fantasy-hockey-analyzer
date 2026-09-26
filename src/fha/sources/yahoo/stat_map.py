"""Yahoo stat IDs → the engine's categories (SPEC §4 "Stat ID mapping").

Built at runtime from the league's settings, never a hardcoded table. Stats
the league doesn't score (GP, or PPG/PPA if the league lacked PPP) come from
the game's full stat list. PPP is Yahoo's own stat when it has one;
otherwise PPG + PPA (SPEC §5).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fha.domain.models import SKATER_CATEGORIES, Category, PlayerSeason
from fha.sources.yahoo.models import Player, StatCategory, StatLine

SKATER, GOALIE = "P", "G"
GOALIE_STATS = ("W", "GAA", "SV%")  # shown raw (no goalie model, SPEC §5)
NO_VALUE = ("-", "")


class StatMapError(ValueError):
    """A needed stat isn't in the league's or the game's stat categories."""


@dataclass(frozen=True)
class StatMap:
    categories: Mapping[Category, tuple[str, ...]]  # stat IDs, summed
    skater_gp: str
    goalie_gp: str | None
    goalie: Mapping[str, str]  # "W" / "GAA" / "SV%" -> stat ID, those the league has

    @property
    def ppp_direct(self) -> bool:
        """True when Yahoo supplies PPP itself (one stat ID, not PPG + PPA)."""
        return len(self.categories[Category.PPP]) == 1


def _find(
    display: str,
    position_type: str,
    sources: Sequence[Sequence[StatCategory]],
) -> str | None:
    """The stat ID named ``display`` for ``position_type``, first source first."""
    for categories in sources:
        ids = {
            c.stat_id
            for c in categories
            if c.display_name == display and position_type in c.position_types
        }
        if len(ids) > 1:
            raise StatMapError(f"{display} ({position_type}) is ambiguous: stat IDs {sorted(ids)}")
        if ids:
            return ids.pop()
    return None


def build_stat_map(
    league_categories: Sequence[StatCategory], game_categories: Sequence[StatCategory]
) -> StatMap:
    sources = (league_categories, game_categories)

    def need(display: str, position_type: str = SKATER) -> str:
        stat_id = _find(display, position_type, sources)
        if stat_id is None:
            raise StatMapError(f"no Yahoo stat {display!r} for position type {position_type}")
        return stat_id

    categories: dict[Category, tuple[str, ...]] = {}
    for cat in SKATER_CATEGORIES:
        if cat is Category.PPP:
            ppp = _find("PPP", SKATER, sources)
            categories[cat] = (ppp,) if ppp is not None else (need("PPG"), need("PPA"))
        else:
            categories[cat] = (need(cat.value),)
    goalie = {
        name: stat_id
        for name in GOALIE_STATS
        if (stat_id := _find(name, GOALIE, [league_categories])) is not None
    }
    return StatMap(
        categories=categories,
        skater_gp=need("GP"),
        goalie_gp=_find("GP", GOALIE, sources),
        goalie=goalie,
    )


def _number(line: StatLine, stat_id: str) -> float | None:
    raw = line.values.get(stat_id, "-").strip()
    if raw in NO_VALUE:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise StatMapError(f"{line.player_key}: stat {stat_id} is not a number: {raw!r}") from None
    if not math.isfinite(value):
        raise StatMapError(f"{line.player_key}: stat {stat_id} is not finite: {raw!r}")
    return value


def _count(line: StatLine, stat_id: str) -> float:
    """A counting stat; Yahoo's "-" (no games yet) is 0."""
    return _number(line, stat_id) or 0.0


def to_player_season(
    player: Player, line: StatLine | None, stat_map: StatMap, aav: int | None = None
) -> PlayerSeason:
    """The engine's input for one player. No stat line means no games played."""
    if line is not None and line.player_key != player.player_key:
        raise StatMapError(f"stat line for {line.player_key} given for {player.player_key}")
    gp_id = stat_map.goalie_gp if player.is_goalie else stat_map.skater_gp
    gp = _count(line, gp_id) if line is not None and gp_id is not None else 0.0
    if gp != int(gp):
        raise StatMapError(f"{player.player_key}: GP {gp} is not a whole number")
    stats = {
        cat: sum(_count(line, sid) for sid in ids) if line is not None else 0.0
        for cat, ids in stat_map.categories.items()
    }
    return PlayerSeason(
        player_id=player.player_id,
        name=player.name,
        gp=int(gp),
        stats=stats,
        is_goalie=player.is_goalie,
        aav=aav,
    )


def goalie_stats(line: StatLine | None, stat_map: StatMap) -> dict[str, float | None]:
    """A goalie's raw W / GAA / SV% (None where Yahoo has no value)."""
    return {
        name: _number(line, stat_id) if line is not None else None
        for name, stat_id in stat_map.goalie.items()
    }
