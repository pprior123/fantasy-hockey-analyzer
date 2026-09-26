"""``LeagueSnapshot`` <-> JSON for the stats cache (SPEC §2 refresh model).

The cache holds the raw snapshot, never ratings, so a settings change needs
no refresh (SPEC §10 M3). ``decode`` is strict: anything that isn't exactly
what ``encode`` wrote raises ``SnapshotCodecError``, and the caller treats
the cache as missing rather than serving a half-read snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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

FORMAT = 1  # bump when the encoding changes; an old cache is then refetched


class SnapshotCodecError(ValueError):
    """The stored snapshot isn't one this version wrote."""


def encode(snap: LeagueSnapshot) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    """``(meta, lists)``: the small parts, and the long lists to chunk."""
    meta = {
        "format": FORMAT,
        "game": {"game_key": snap.game.game_key, "season": snap.game.season},
        "settings": _settings(snap.settings),
        "game_stat_categories": [_category(c) for c in snap.game_stat_categories],
        "teams": [_team(t) for t in snap.teams],
        "scoreboard": _scoreboard(snap.scoreboard),
        "next_scoreboard": None
        if snap.next_scoreboard is None
        else _scoreboard(snap.next_scoreboard),
        "has_last_season": snap.last_season_stats is not None,
    }
    lists = {
        "available": [_player(p) for p in snap.available],
        "stats": [_line(s) for s in snap.stats.values()],
        "last_season_stats": [_line(s) for s in (snap.last_season_stats or {}).values()],
    }
    return meta, lists


def decode(meta: Mapping[str, Any], lists: Mapping[str, list[Any]]) -> LeagueSnapshot:
    try:
        if meta.get("format") != FORMAT:
            raise SnapshotCodecError(f"cache format {meta.get('format')!r}, expected {FORMAT}")
        if set(lists) != {"available", "stats", "last_season_stats"}:
            raise SnapshotCodecError(f"cache lists {sorted(lists)}")
        last = [_unline(s) for s in lists["last_season_stats"]]
        if not meta["has_last_season"] and last:
            raise SnapshotCodecError("last season's stats present but not flagged")
        next_board = meta["next_scoreboard"]
        return LeagueSnapshot(
            game=Game(_str(meta["game"]["game_key"]), _int(meta["game"]["season"])),
            settings=_unsettings(meta["settings"]),
            game_stat_categories=tuple(_uncategory(c) for c in meta["game_stat_categories"]),
            teams=tuple(_unteam(t) for t in meta["teams"]),
            available=tuple(_unplayer(p) for p in lists["available"]),
            stats=_by_key(_unline(s) for s in lists["stats"]),
            last_season_stats=_by_key(last) if meta["has_last_season"] else None,
            scoreboard=_unscoreboard(meta["scoreboard"]),
            next_scoreboard=None if next_board is None else _unscoreboard(next_board),
        )
    except SnapshotCodecError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        raise SnapshotCodecError(f"malformed cached snapshot: {type(e).__name__}: {e}") from None


# ---------------------------------------------------------------- encode


def _category(c: StatCategory) -> dict[str, Any]:
    return {
        "stat_id": c.stat_id,
        "name": c.name,
        "display_name": c.display_name,
        "position_types": sorted(c.position_types),
        "is_only_display": c.is_only_display,
    }


def _settings(s: LeagueSettings) -> dict[str, Any]:
    return {
        "league_key": s.league_key,
        "num_teams": s.num_teams,
        "season": s.season,
        "current_week": s.current_week,
        "start_week": s.start_week,
        "end_week": s.end_week,
        "stat_categories": [_category(c) for c in s.stat_categories],
        "roster_slots": [
            {"position": r.position, "count": r.count, "is_starting": r.is_starting}
            for r in s.roster_slots
        ],
    }


def _player(p: Player) -> dict[str, Any]:
    return {
        "player_key": p.player_key,
        "player_id": p.player_id,
        "name": p.name,
        "nhl_team": p.nhl_team,
        "display_position": p.display_position,
        "eligible_positions": list(p.eligible_positions),
        "position_type": p.position_type,
        "status": p.status,
    }


def _team(t: Team) -> dict[str, Any]:
    return {
        "team_key": t.team_key,
        "name": t.name,
        "is_mine": t.is_mine,
        "roster": [
            {"player": _player(e.player), "selected_position": e.selected_position}
            for e in t.roster
        ],
    }


def _line(s: StatLine) -> dict[str, Any]:
    return {"player_key": s.player_key, "season": s.season, "values": dict(s.values)}


def _scoreboard(b: Scoreboard) -> dict[str, Any]:
    return {
        "week": b.week,
        "week_start": b.week_start,
        "week_end": b.week_end,
        "matchups": [{"week": m.week, "team_keys": list(m.team_keys)} for m in b.matchups],
    }


# ---------------------------------------------------------------- decode


def _str(value: Any) -> str:
    if not isinstance(value, str):
        raise SnapshotCodecError(f"expected a string, got {type(value).__name__}")
    return value


def _opt_str(value: Any) -> str | None:
    return None if value is None else _str(value)


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotCodecError(f"expected an integer, got {type(value).__name__}")
    return value


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise SnapshotCodecError(f"expected a boolean, got {type(value).__name__}")
    return value


def _uncategory(c: Mapping[str, Any]) -> StatCategory:
    return StatCategory(
        stat_id=_str(c["stat_id"]),
        name=_str(c["name"]),
        display_name=_str(c["display_name"]),
        position_types=frozenset(_str(t) for t in c["position_types"]),
        is_only_display=_bool(c["is_only_display"]),
    )


def _unsettings(s: Mapping[str, Any]) -> LeagueSettings:
    return LeagueSettings(
        league_key=_str(s["league_key"]),
        num_teams=_int(s["num_teams"]),
        season=_int(s["season"]),
        current_week=_int(s["current_week"]),
        start_week=_int(s["start_week"]),
        end_week=_int(s["end_week"]),
        stat_categories=tuple(_uncategory(c) for c in s["stat_categories"]),
        roster_slots=tuple(
            RosterSlot(_str(r["position"]), _int(r["count"]), _bool(r["is_starting"]))
            for r in s["roster_slots"]
        ),
    )


def _unplayer(p: Mapping[str, Any]) -> Player:
    return Player(
        player_key=_str(p["player_key"]),
        player_id=_str(p["player_id"]),
        name=_str(p["name"]),
        nhl_team=_str(p["nhl_team"]),
        display_position=_str(p["display_position"]),
        eligible_positions=tuple(_str(e) for e in p["eligible_positions"]),
        position_type=_str(p["position_type"]),
        status=_opt_str(p["status"]),
    )


def _unteam(t: Mapping[str, Any]) -> Team:
    return Team(
        team_key=_str(t["team_key"]),
        name=_str(t["name"]),
        is_mine=_bool(t["is_mine"]),
        roster=tuple(
            RosterEntry(_unplayer(e["player"]), _str(e["selected_position"])) for e in t["roster"]
        ),
    )


def _unline(s: Mapping[str, Any]) -> StatLine:
    values = s["values"]
    if not isinstance(values, dict):
        raise SnapshotCodecError("stat values must be an object")
    return StatLine(
        player_key=_str(s["player_key"]),
        season=_int(s["season"]),
        values={_str(k): _str(v) for k, v in values.items()},
    )


def _by_key(lines: Any) -> dict[str, StatLine]:
    out: dict[str, StatLine] = {}
    for line in lines:
        if line.player_key in out:
            raise SnapshotCodecError(f"two stat lines for {line.player_key}")
        out[line.player_key] = line
    return out


def _unscoreboard(b: Mapping[str, Any]) -> Scoreboard:
    matchups = []
    for m in b["matchups"]:
        keys = tuple(_str(k) for k in m["team_keys"])
        if len(keys) != 2:
            raise SnapshotCodecError(f"a matchup needs two teams, got {len(keys)}")
        matchups.append(Matchup(_int(m["week"]), (keys[0], keys[1])))
    return Scoreboard(
        week=_int(b["week"]),
        week_start=_opt_str(b["week_start"]),
        week_end=_opt_str(b["week_end"]),
        matchups=tuple(matchups),
    )
