"""Parsing Yahoo's JSON into models (synthetic responses; see builders.py)."""

from typing import Any

import pytest

from fha.sources.yahoo import parse
from fha.sources.yahoo.models import (
    Game,
    Matchup,
    Player,
    RosterEntry,
    RosterSlot,
    StatCategory,
)
from fha.sources.yahoo.parse import YahooParseError
from tests.unit.yahoo import builders as b

KNIGHT = b.P("101", "Ada Knight", "TB", "C,LW", stats={"0": "80", "1": "30"})
STONE = b.P("102", "Bo Stone", "Edm", "D", status="IR")
WALL = b.P("103", "Cy Wall", "Bos", "G")


# ---------------------------------------------------------------- collections


def test_collection_reads_items_in_index_order() -> None:
    node = {"1": {"x": "b"}, "count": 2, "0": {"x": "a"}}
    assert parse._collection(node, "x", "t") == ["a", "b"]


def test_empty_collection_is_an_empty_list() -> None:
    assert parse._collection([], "x", "t") == []


@pytest.mark.parametrize(
    ("node", "message"),
    [
        ({"0": {"x": 1}, "2": {"x": 2}, "count": 2}, "not 0..n-1"),
        ({"0": {"x": 1}, "count": 2}, "count 2 but 1 entries"),
        ({"0": {"y": 1}, "count": 1}, "no 'x'"),
        ({"0": {"x": 1}, "count": "many"}, "expected an integer"),
        ("text", "expected a collection"),
    ],
)
def test_malformed_collections_are_rejected(node: Any, message: str) -> None:
    with pytest.raises(YahooParseError, match=message):
        parse._collection(node, "x", "t")


def test_merge_folds_nested_fragments_and_skips_empty_lists() -> None:
    assert parse._merge([[{"a": 1}, [], {"b": 2}], {"c": 3}], "t") == {"a": 1, "b": 2, "c": 3}


def test_merge_rejects_a_scalar() -> None:
    with pytest.raises(YahooParseError, match="list of fragments"):
        parse._merge([{"a": 1}, "oops"], "t")


# ---------------------------------------------------------------- games


def test_games_picks_the_latest_season() -> None:
    content = b.games(("453", 2025), ("465", 2026), ("427", 2024))
    assert parse.parse_games(content) == Game("465", 2026)


def test_games_with_no_game_is_an_error() -> None:
    with pytest.raises(YahooParseError, match="no NHL game"):
        parse.parse_games({"games": []})


def test_games_response_without_games_is_an_error() -> None:
    with pytest.raises(YahooParseError, match="missing 'games'"):
        parse.parse_games({"error": {}})


# ---------------------------------------------------------------- settings


def test_league_settings() -> None:
    settings = parse.parse_league_settings(b.league_settings(current_week=3))
    assert settings.league_key == b.LEAGUE_KEY
    assert (settings.num_teams, settings.season) == (8, b.SEASON)
    assert (settings.current_week, settings.start_week, settings.end_week) == (3, 1, 25)
    assert settings.roster_slots[0] == RosterSlot("C", 3, is_starting=True)
    assert RosterSlot("IR+", 1, is_starting=False) in settings.roster_slots
    assert [c.display_name for c in settings.stat_categories] == [
        "G", "A", "PPP", "PIM", "HIT", "SOG", "BLK", "W", "GAA", "SV%",
    ]  # fmt: skip
    assert settings.stat_categories[2] == StatCategory(
        "8", "Powerplay Points", "PPP", frozenset("P")
    )


def test_display_only_stat_is_flagged_either_way_yahoo_marks_it() -> None:
    top_level = b.stat_entry("0", "Games Played", "GP", "P", is_only_display_stat="1")
    nested = b.stat_entry("4", "Plus/Minus", "+/-", "P")
    nested["stat"]["stat_position_types"][0]["stat_position_type"]["is_only_display_stat"] = "1"
    plain = b.stat_entry("1", "Goals", "G", "P")
    content = b.league_settings()
    content["league"][1]["settings"][0]["stat_categories"]["stats"] = [top_level, nested, plain]
    cats = parse.parse_league_settings(content).stat_categories
    assert [c.is_only_display for c in cats] == [True, True, False]


def test_league_settings_missing_week_is_an_error() -> None:
    content = b.league_settings()
    del content["league"][0]["current_week"]
    with pytest.raises(YahooParseError, match="current_week"):
        parse.parse_league_settings(content)


def test_game_stat_categories_collect_position_types() -> None:
    cats = parse.parse_game_stat_categories(b.game_stat_categories())
    by_id = {c.stat_id: c for c in cats}
    assert by_id["0"] == StatCategory("0", "Games Played", "GP", frozenset("P"))
    assert by_id["29"].position_types == frozenset("G")
    assert by_id["6"].display_name == "PPG"


def test_stat_categories_must_be_a_list() -> None:
    content = b.game_stat_categories()
    content["game"][1]["stat_categories"]["stats"] = {"stat": {}}
    with pytest.raises(YahooParseError, match="expected a list"):
        parse.parse_game_stat_categories(content)


# ---------------------------------------------------------------- players


def test_league_players_page() -> None:
    players = parse.parse_league_players(b.league_players([KNIGHT, STONE, WALL]))
    assert players[0] == Player(
        player_key="465.p.101",
        player_id="101",
        name="Ada Knight",
        nhl_team="TB",
        display_position="C,LW",
        eligible_positions=("C", "LW"),
        position_type="P",
        status=None,
    )
    assert players[1].status == "IR"
    assert players[2].is_goalie
    assert not players[0].is_goalie


def test_past_the_last_page_yahoo_sends_an_empty_list() -> None:
    assert parse.parse_league_players(b.league_players([])) == ()


def test_player_without_a_team_has_an_empty_team() -> None:
    content = b.league_players([KNIGHT])
    meta = content["league"][1]["players"]["0"]["player"][0]
    meta.remove({"editorial_team_abbr": "TB"})
    assert parse.parse_league_players(content)[0].nhl_team == ""


def test_player_name_must_have_a_full_name() -> None:
    content = b.league_players([KNIGHT])
    content["league"][1]["players"]["0"]["player"][0][2] = {"name": "Ada Knight"}
    with pytest.raises(YahooParseError, match=r"name\.full"):
        parse.parse_league_players(content)


def test_player_stats_are_raw_strings_by_stat_id() -> None:
    lines = parse.parse_player_stats(b.player_stats([KNIGHT, STONE]), b.SEASON)
    assert lines["465.p.101"].values == {"0": "80", "1": "30"}
    assert lines["465.p.101"].season == b.SEASON
    assert lines["465.p.102"].values == {}


def test_player_stats_accept_a_flat_coverage_block() -> None:
    content = b.player_stats([KNIGHT])
    block = content["players"]["0"]["player"][1]["player_stats"]
    block.update(block.pop("0"))
    assert parse.parse_player_stats(content, b.SEASON)["465.p.101"].values["1"] == "30"


def test_player_stats_for_the_wrong_season_are_refused() -> None:
    content = b.player_stats([KNIGHT], season=2026)
    with pytest.raises(YahooParseError, match="asked for 2025 stats, got 2026"):
        parse.parse_player_stats(content, 2025)


def test_a_null_stat_value_reads_as_no_value() -> None:
    content = b.player_stats([KNIGHT])
    content["players"]["0"]["player"][1]["player_stats"]["stats"][0]["stat"]["value"] = None
    assert parse.parse_player_stats(content, b.SEASON)["465.p.101"].values["0"] == "-"


def test_player_stats_must_be_an_object() -> None:
    content = b.player_stats([KNIGHT])
    content["players"]["0"]["player"][1]["player_stats"] = []
    with pytest.raises(YahooParseError, match="not an object"):
        parse.parse_player_stats(content, b.SEASON)


# ---------------------------------------------------------------- rosters


def test_teams_rosters_with_slots_and_my_team() -> None:
    teams = parse.parse_teams_rosters(
        b.teams_roster(
            [
                b.T(1, [(KNIGHT, "C"), (STONE, "IR+")], is_mine=True, name="Mine"),
                b.T(2, [(WALL, "G")]),
            ]
        )
    )
    mine, other = teams
    assert (mine.team_key, mine.name, mine.is_mine) == (f"{b.LEAGUE_KEY}.t.1", "Mine", True)
    assert not other.is_mine
    assert [e.selected_position for e in mine.roster] == ["C", "IR+"]
    assert [e.in_ir_slot for e in mine.roster] == [False, True]
    assert other.roster == (
        RosterEntry(parse.parse_league_players(b.league_players([WALL]))[0], "G"),
    )


def test_roster_players_directly_under_roster_are_read_too() -> None:
    content = b.teams_roster([b.T(1, [(KNIGHT, "BN")])])
    roster = content["league"][1]["teams"]["0"]["team"][1]["roster"]
    roster["players"] = roster.pop("0")["players"]
    (team,) = parse.parse_teams_rosters(content)
    assert team.roster[0].selected_position == "BN"


def test_empty_roster() -> None:
    (team,) = parse.parse_teams_rosters(b.teams_roster([b.T(1)]))
    assert team.roster == ()


def test_team_given_as_one_flat_object() -> None:
    content = b.teams_roster([b.T(1, [(KNIGHT, "C")])])
    entry = content["league"][1]["teams"]["0"]
    entry["team"] = parse._merge(entry["team"], "t")
    (team,) = parse.parse_teams_rosters(content)
    assert team.roster[0].player.name == "Ada Knight"


def test_roster_without_players_is_an_error() -> None:
    content = b.teams_roster([b.T(1)])
    content["league"][1]["teams"]["0"]["team"][1]["roster"] = {"coverage_type": "date"}
    with pytest.raises(YahooParseError, match="missing 'players'"):
        parse.parse_teams_rosters(content)


# ---------------------------------------------------------------- scoreboard


def test_scoreboard_pairs_and_dates() -> None:
    board = parse.parse_scoreboard(b.scoreboard(2, [(1, 2), (3, 4)]))
    assert board.week == 2
    assert (board.week_start, board.week_end) == ("2026-10-05", "2026-10-11")
    assert board.matchups[0] == Matchup(2, (f"{b.LEAGUE_KEY}.t.1", f"{b.LEAGUE_KEY}.t.2"))
    assert board.opponent(f"{b.LEAGUE_KEY}.t.4") == f"{b.LEAGUE_KEY}.t.3"
    assert board.opponent(f"{b.LEAGUE_KEY}.t.1") == f"{b.LEAGUE_KEY}.t.2"
    assert board.opponent(f"{b.LEAGUE_KEY}.t.7") is None


def test_scoreboard_with_no_matchups() -> None:
    board = parse.parse_scoreboard(b.scoreboard(1, []))
    assert (board.matchups, board.week_start, board.week_end) == ((), None, None)


def test_matchup_needs_two_teams() -> None:
    content = b.scoreboard(1, [(1, 2)])
    teams = content["league"][1]["scoreboard"]["0"]["matchups"]["0"]["matchup"]["0"]["teams"]
    del teams["1"]
    teams["count"] = 1
    with pytest.raises(YahooParseError, match="expected 2 teams, got 1"):
        parse.parse_scoreboard(content)


def test_matchup_from_another_week_is_an_error() -> None:
    content = b.scoreboard(1, [(1, 2)])
    content["league"][1]["scoreboard"]["0"]["matchups"]["0"]["matchup"]["week"] = "2"
    with pytest.raises(YahooParseError, match="week 2 on the week 1"):
        parse.parse_scoreboard(content)


def test_matchups_disagreeing_on_dates_is_an_error() -> None:
    content = b.scoreboard(1, [(1, 2), (3, 4)])
    content["league"][1]["scoreboard"]["0"]["matchups"]["1"]["matchup"]["week_end"] = "2026-10-12"
    with pytest.raises(YahooParseError, match="disagree"):
        parse.parse_scoreboard(content)


@pytest.mark.parametrize("bad", [[], "x"])
def test_scoreboard_must_be_an_object(bad: Any) -> None:
    content = b.scoreboard(1, [])
    content["league"][1]["scoreboard"] = bad
    with pytest.raises(YahooParseError, match="scoreboard"):
        parse.parse_scoreboard(content)


def test_matchup_must_be_an_object() -> None:
    content = b.scoreboard(1, [(1, 2)])
    content["league"][1]["scoreboard"]["0"]["matchups"]["0"]["matchup"] = ["x"]
    with pytest.raises(YahooParseError, match="matchup: expected an object"):
        parse.parse_scoreboard(content)


# ---------------------------------------------------------------- scalars


@pytest.mark.parametrize("value", [True, None, "x", 1.5j])
def test_as_int_rejects_non_integers(value: Any) -> None:
    with pytest.raises(YahooParseError, match="expected an integer"):
        parse._as_int(value, "t")


@pytest.mark.parametrize("value", [True, None, 1.5, {"a": 1}])
def test_as_str_rejects_non_strings(value: Any) -> None:
    with pytest.raises(YahooParseError, match="expected a string"):
        parse._as_str(value, "t")


def test_as_str_accepts_yahoo_integer_ids() -> None:
    assert parse._as_str(465, "t") == "465"
