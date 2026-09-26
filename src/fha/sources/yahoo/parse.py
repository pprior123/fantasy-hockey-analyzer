"""Parse Yahoo's ``?format=json`` responses into models. Pure: no I/O.

Yahoo's JSON is a direct translation of its XML, with two quirks handled
here once:

- an object is often split into a list of one-key fragments, some nested in
  further lists: ``[[{"player_key": ...}, {"name": {...}}], {"player_stats":
  ...}]``. ``_merge`` folds that into one dict;
- a collection is a dict of ``"0"``, ``"1"``, ... entries plus ``"count"``,
  each wrapping its item under the item's name (``{"0": {"team": ...}}``),
  and an empty collection is ``[]``. ``_collection`` turns it into a list.

Anything unexpected raises ``YahooParseError`` naming what was missing, so a
change in Yahoo's format fails loudly instead of yielding empty data.
"""

from __future__ import annotations

from typing import Any

from fha.sources.yahoo.models import (
    Game,
    LeagueSettings,
    Matchup,
    Player,
    RosterEntry,
    RosterSlot,
    Scoreboard,
    StatCategory,
    StatLine,
    Team,
)


class YahooParseError(ValueError):
    """A Yahoo response didn't have the expected shape."""


# ---------------------------------------------------------------- helpers


def _merge(node: Any, what: str) -> dict[str, Any]:
    """Fold Yahoo's list of fragments (dicts and nested lists) into one dict."""
    if isinstance(node, dict):
        return node
    if not isinstance(node, list):
        raise YahooParseError(f"{what}: expected an object or a list of fragments")
    merged: dict[str, Any] = {}
    for item in node:
        merged.update(_merge(item, what))
    return merged


def _collection(node: Any, item: str, what: str) -> list[Any]:
    """The items of a Yahoo collection, in order (``[]`` when empty)."""
    if node == []:
        return []
    if not isinstance(node, dict):
        raise YahooParseError(f"{what}: expected a collection of {item}")
    indexes = sorted(int(k) for k in node if k.isdigit())
    if indexes != list(range(len(indexes))):
        raise YahooParseError(f"{what}: collection keys are not 0..n-1")
    count = node.get("count", len(indexes))
    if _as_int(count, f"{what}.count") != len(indexes):
        raise YahooParseError(f"{what}: count {count} but {len(indexes)} entries")
    out = []
    for i in indexes:
        entry = node[str(i)]
        if not isinstance(entry, dict) or item not in entry:
            raise YahooParseError(f"{what}[{i}]: no {item!r}")
        out.append(entry[item])
    return out


def _object(obj: Any, what: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise YahooParseError(f"{what}: expected an object, got {type(obj).__name__}")
    return obj


def _get(obj: dict[str, Any], key: str, what: str) -> Any:
    if key not in _object(obj, what):
        raise YahooParseError(f"{what}: missing {key!r}")
    return obj[key]


def _sub(obj: dict[str, Any], key: str, what: str) -> Any:
    """``obj[key]``, or ``obj["0"][key]``: Yahoo nests some subresources under "0"."""
    if key in _object(obj, what):
        return obj[key]
    inner = obj.get("0")
    if isinstance(inner, dict) and key in inner:
        return inner[key]
    raise YahooParseError(f"{what}: missing {key!r}")


def _as_int(value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise YahooParseError(f"{what}: expected an integer, got a bool")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise YahooParseError(f"{what}: expected an integer, got {value!r}") from None


def _as_str(value: Any, what: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise YahooParseError(f"{what}: expected a string, got {value!r}")
    return str(value)


def _flag(value: Any) -> bool:
    return str(value) == "1"


def _league(content: dict[str, Any]) -> dict[str, Any]:
    return _merge(_get(content, "league", "response"), "league")


# ---------------------------------------------------------------- game


def parse_games(content: dict[str, Any]) -> Game:
    """The current NHL game from ``games;game_codes=nhl``: the latest season listed."""
    games = []
    for raw in _collection(_get(content, "games", "response"), "game", "games"):
        game = _merge(raw, "game")
        games.append(
            Game(
                game_key=_as_str(_get(game, "game_key", "game"), "game_key"),
                season=_as_int(_get(game, "season", "game"), "season"),
            )
        )
    if not games:
        raise YahooParseError("games: no NHL game listed")
    return max(games, key=lambda g: g.season)


# ---------------------------------------------------------------- stat categories


def _position_types(stat: dict[str, Any]) -> frozenset[str]:
    types = set()
    if "position_type" in stat:
        types.add(_as_str(stat["position_type"], "stat.position_type"))
    for entry in stat.get("position_types", []):
        types.add(_as_str(_get(entry, "position_type", "stat.position_types"), "position_type"))
    for entry in stat.get("stat_position_types", []):
        inner = _get(entry, "stat_position_type", "stat.stat_position_types")
        types.add(_as_str(_get(inner, "position_type", "stat_position_type"), "position_type"))
    return frozenset(types)


def _only_display(stat: dict[str, Any]) -> bool:
    if _flag(stat.get("is_only_display_stat")):
        return True
    return any(
        _flag(entry.get("stat_position_type", {}).get("is_only_display_stat"))
        for entry in stat.get("stat_position_types", [])
    )


def _stat_categories(node: Any, what: str) -> tuple[StatCategory, ...]:
    stats = _get(_merge(node, what), "stats", what)
    if not isinstance(stats, list):
        raise YahooParseError(f"{what}.stats: expected a list")
    out = []
    for entry in stats:
        stat = _get(entry, "stat", what)
        out.append(
            StatCategory(
                stat_id=_as_str(_get(stat, "stat_id", what), "stat_id"),
                name=_as_str(_get(stat, "name", what), "stat.name"),
                display_name=_as_str(_get(stat, "display_name", what), "stat.display_name"),
                position_types=_position_types(stat),
                is_only_display=_only_display(stat),
            )
        )
    return tuple(out)


def parse_game_stat_categories(content: dict[str, Any]) -> tuple[StatCategory, ...]:
    """Every stat Yahoo tracks for the game (``game/{key}/stat_categories``)."""
    game = _merge(_get(content, "game", "response"), "game")
    return _stat_categories(_get(game, "stat_categories", "game"), "game.stat_categories")


# ---------------------------------------------------------------- league settings


def parse_league_settings(content: dict[str, Any]) -> LeagueSettings:
    """``league/{key}/settings``: metadata, the league's categories, roster slots."""
    league = _league(content)
    settings = _merge(_get(league, "settings", "league"), "league.settings")
    slots = []
    for entry in _get(settings, "roster_positions", "settings"):
        pos = _get(entry, "roster_position", "roster_positions")
        slots.append(
            RosterSlot(
                position=_as_str(_get(pos, "position", "roster_position"), "position"),
                count=_as_int(_get(pos, "count", "roster_position"), "roster_position.count"),
                is_starting=_flag(pos.get("is_starting_position", "0")),
            )
        )
    return LeagueSettings(
        league_key=_as_str(_get(league, "league_key", "league"), "league_key"),
        num_teams=_as_int(_get(league, "num_teams", "league"), "num_teams"),
        season=_as_int(_get(league, "season", "league"), "season"),
        current_week=_as_int(_get(league, "current_week", "league"), "current_week"),
        start_week=_as_int(_get(league, "start_week", "league"), "start_week"),
        end_week=_as_int(_get(league, "end_week", "league"), "end_week"),
        stat_categories=_stat_categories(
            _get(settings, "stat_categories", "settings"), "settings.stat_categories"
        ),
        roster_slots=tuple(slots),
    )


# ---------------------------------------------------------------- players


def _player(meta: dict[str, Any]) -> Player:
    name = _get(meta, "name", "player")
    full = name.get("full") if isinstance(name, dict) else None
    positions = []
    for entry in meta.get("eligible_positions", []):
        positions.append(_as_str(_get(entry, "position", "eligible_positions"), "position"))
    status = meta.get("status")
    return Player(
        player_key=_as_str(_get(meta, "player_key", "player"), "player_key"),
        player_id=_as_str(_get(meta, "player_id", "player"), "player_id"),
        name=_as_str(full, "player.name.full"),
        nhl_team=_as_str(meta.get("editorial_team_abbr", ""), "editorial_team_abbr"),
        display_position=_as_str(_get(meta, "display_position", "player"), "display_position"),
        eligible_positions=tuple(positions),
        position_type=_as_str(_get(meta, "position_type", "player"), "position_type"),
        status=_as_str(status, "status") if status else None,
    )


def parse_league_players(content: dict[str, Any]) -> tuple[Player, ...]:
    """One page of ``league/{key}/players;...`` (``[]`` past the last page)."""
    league = _league(content)
    raw = _collection(_get(league, "players", "league"), "player", "league.players")
    return tuple(_player(_merge(p, "player")) for p in raw)


def parse_player_stats(content: dict[str, Any], season: int) -> dict[str, StatLine]:
    """``players;player_keys=.../stats;type=season[;season=Y]``, by player_key.

    Refuses stats for a season other than ``season``, so a request whose
    season parameter Yahoo ignored can't pass off one season as another.
    """
    out = {}
    for raw in _collection(_get(content, "players", "response"), "player", "players"):
        player = _merge(raw, "player")
        key = _as_str(_get(player, "player_key", "player"), "player_key")
        block = _get(player, "player_stats", f"player {key}")
        if not isinstance(block, dict):
            raise YahooParseError(f"player {key}: player_stats is not an object")
        coverage = block.get("0", block)
        if isinstance(coverage, dict) and "season" in coverage:
            got = _as_int(coverage["season"], f"player {key}: season")
            if got != season:
                raise YahooParseError(f"player {key}: asked for {season} stats, got {got}")
        values = {}
        for entry in _get(block, "stats", f"player {key}: player_stats"):
            stat = _get(entry, "stat", f"player {key}: stats")
            stat_id = _as_str(_get(stat, "stat_id", f"player {key}: stat"), "stat_id")
            value = stat.get("value")
            values[stat_id] = "-" if value is None else _as_str(value, f"stat {stat_id}")
        out[key] = StatLine(player_key=key, season=season, values=values)
    return out


# ---------------------------------------------------------------- teams / rosters


def _team_meta(raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a team into (metadata, subresources): ``[[meta...], {sub...}]``."""
    if isinstance(raw, list) and raw and isinstance(raw[0], list):
        return _merge(raw[0], "team"), _merge(raw[1:], "team")
    merged = _merge(raw, "team")
    return merged, merged


def parse_teams_rosters(content: dict[str, Any]) -> tuple[Team, ...]:
    """``league/{key}/teams/roster``: every team with today's roster and slots."""
    league = _league(content)
    teams = []
    for raw in _collection(_get(league, "teams", "league"), "team", "league.teams"):
        meta, sub = _team_meta(raw)
        team_key = _as_str(_get(meta, "team_key", "team"), "team_key")
        roster = _get(sub, "roster", f"team {team_key}")
        players = _sub(roster, "players", f"team {team_key}: roster")
        entries = []
        for p in _collection(players, "player", f"team {team_key}: roster.players"):
            player = _merge(p, "player")
            slot = _merge(_get(player, "selected_position", "player"), "selected_position")
            entries.append(
                RosterEntry(
                    player=_player(player),
                    selected_position=_as_str(
                        _get(slot, "position", "selected_position"), "selected_position"
                    ),
                )
            )
        teams.append(
            Team(
                team_key=team_key,
                name=_as_str(_get(meta, "name", f"team {team_key}"), "team name"),
                is_mine=_flag(meta.get("is_owned_by_current_login", "0")),
                roster=tuple(entries),
            )
        )
    return tuple(teams)


# ---------------------------------------------------------------- scoreboard


def parse_scoreboard(content: dict[str, Any]) -> Scoreboard:
    """``league/{key}/scoreboard;week=N``: who plays whom that week."""
    league = _league(content)
    board = _get(league, "scoreboard", "league")
    if not isinstance(board, dict):
        raise YahooParseError("scoreboard: expected an object")
    week = _as_int(_get(board, "week", "scoreboard"), "scoreboard.week")
    matchups = []
    starts, ends = set(), set()
    for m in _collection(_sub(board, "matchups", "scoreboard"), "matchup", "scoreboard.matchups"):
        if not isinstance(m, dict):
            raise YahooParseError("matchup: expected an object")
        keys = []
        for raw in _collection(_sub(m, "teams", "matchup"), "team", "matchup.teams"):
            meta, _ = _team_meta(raw)
            keys.append(_as_str(_get(meta, "team_key", "matchup team"), "team_key"))
        if len(keys) != 2:
            raise YahooParseError(f"matchup: expected 2 teams, got {len(keys)}")
        m_week = _as_int(m.get("week", week), "matchup.week")
        if m_week != week:
            raise YahooParseError(f"matchup for week {m_week} on the week {week} scoreboard")
        matchups.append(Matchup(week=week, team_keys=(keys[0], keys[1])))
        if "week_start" in m:
            starts.add(_as_str(m["week_start"], "week_start"))
        if "week_end" in m:
            ends.add(_as_str(m["week_end"], "week_end"))
    if len(starts) > 1 or len(ends) > 1:
        raise YahooParseError(f"scoreboard week {week}: matchups disagree on the week's dates")
    return Scoreboard(
        week=week,
        week_start=next(iter(starts), None),
        week_end=next(iter(ends), None),
        matchups=tuple(matchups),
    )
