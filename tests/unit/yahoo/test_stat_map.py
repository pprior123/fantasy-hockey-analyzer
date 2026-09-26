"""Stat-ID mapping from league settings (SPEC §4) and conversion to PlayerSeason."""

import pytest

from fha.domain.models import Category, PlayerSeason
from fha.sources.yahoo.models import Player, StatCategory, StatLine
from fha.sources.yahoo.stat_map import (
    StatMap,
    StatMapError,
    build_stat_map,
    goalie_stats,
    to_player_season,
)


def cat(stat_id: str, display: str, *types: str) -> StatCategory:
    return StatCategory(stat_id, display, display, frozenset(types or "P"))


LEAGUE = [
    cat("1", "G"),
    cat("2", "A"),
    cat("8", "PPP"),
    cat("5", "PIM"),
    cat("31", "HIT"),
    cat("14", "SOG"),
    cat("32", "BLK"),
    cat("19", "W", "G"),
    cat("23", "GAA", "G"),
    cat("26", "SV%", "G"),
]
GAME = [cat("0", "GP"), cat("29", "GP", "G"), cat("6", "PPG"), cat("7", "PPA"), *LEAGUE]
MAP = build_stat_map(LEAGUE, GAME)

SKATER = Player("465.p.1", "1", "Ada Knight", "TB", "C", ("C",), "P")
GOALIE = Player("465.p.2", "2", "Cy Wall", "Bos", "G", ("G",), "G")


def line(player: Player, **values: str) -> StatLine:
    return StatLine(player.player_key, 2026, {k.removeprefix("s"): v for k, v in values.items()})


def test_league_categories_map_to_their_stat_ids() -> None:
    assert MAP.categories == {
        Category.G: ("1",),
        Category.A: ("2",),
        Category.PPP: ("8",),
        Category.PIM: ("5",),
        Category.HIT: ("31",),
        Category.SOG: ("14",),
        Category.BLK: ("32",),
    }
    assert MAP.ppp_direct
    assert (MAP.skater_gp, MAP.goalie_gp) == ("0", "29")
    assert MAP.goalie == {"W": "19", "GAA": "23", "SV%": "26"}


def test_without_ppp_the_map_sums_ppg_and_ppa() -> None:
    league = [c for c in LEAGUE if c.display_name != "PPP"]
    game = [c for c in GAME if c.display_name != "PPP"]
    stat_map = build_stat_map(league, game)
    assert stat_map.categories[Category.PPP] == ("6", "7")
    assert not stat_map.ppp_direct


def test_league_wins_over_game_for_the_same_display_name() -> None:
    game = [cat("99", "G"), *GAME]
    assert build_stat_map(LEAGUE, game).categories[Category.G] == ("1",)


def test_a_skater_stat_is_not_taken_from_a_goalie_category() -> None:
    league = [c for c in LEAGUE if c.display_name != "HIT"] + [cat("77", "HIT", "G")]
    game = [c for c in GAME if c.display_name != "HIT"]
    with pytest.raises(StatMapError, match="'HIT' for position type P"):
        build_stat_map(league, game)


def test_one_gp_stat_for_both_position_types_serves_both() -> None:
    game = [cat("0", "GP", "P", "G"), *(c for c in GAME if c.display_name != "GP")]
    stat_map = build_stat_map(LEAGUE, game)
    assert (stat_map.skater_gp, stat_map.goalie_gp) == ("0", "0")


def test_missing_goalie_gp_is_none() -> None:
    game = [c for c in GAME if c.stat_id != "29"]
    assert build_stat_map(LEAGUE, game).goalie_gp is None


def test_ambiguous_goalie_gp_prefers_the_goalie_only_stat() -> None:
    game = [cat("0", "GP", "P", "G"), cat("29", "GP", "G"), *GAME[2:]]
    assert (build_stat_map(LEAGUE, game).skater_gp, build_stat_map(LEAGUE, game).goalie_gp) == (
        "0",
        "29",
    )


def test_goalie_gp_still_ambiguous_is_left_out_not_an_error() -> None:
    game = [cat("0", "GP"), cat("28", "GP", "G"), cat("29", "GP", "G"), *GAME[2:]]
    assert build_stat_map(LEAGUE, game).goalie_gp is None


def test_goalie_stats_only_come_from_the_league() -> None:
    league = [c for c in LEAGUE if c.display_name != "GAA"]
    assert build_stat_map(league, GAME).goalie == {"W": "19", "SV%": "26"}


def test_missing_skater_gp_is_an_error() -> None:
    with pytest.raises(StatMapError, match="'GP'"):
        build_stat_map(LEAGUE, [c for c in GAME if c.stat_id != "0"])


def test_missing_ppp_and_ppa_is_an_error() -> None:
    league = [c for c in LEAGUE if c.display_name != "PPP"]
    game = [c for c in GAME if c.display_name not in {"PPP", "PPA"}]
    with pytest.raises(StatMapError, match="'PPA'"):
        build_stat_map(league, game)


def test_two_stats_with_one_name_are_ambiguous() -> None:
    with pytest.raises(StatMapError, match=r"SOG \(P\) is ambiguous: stat IDs \['14', '15'\]"):
        build_stat_map([*LEAGUE, cat("15", "SOG")], GAME)


# ---------------------------------------------------------------- conversion


def test_skater_line_becomes_a_player_season() -> None:
    stats = line(SKATER, s0="80", s1="30", s2="40", s8="25", s5="12", s31="90", s14="250", s32="30")
    season = to_player_season(SKATER, stats, MAP, aav=9_500_000)
    assert season == PlayerSeason(
        player_id="1",
        name="Ada Knight",
        gp=80,
        stats={
            Category.G: 30,
            Category.A: 40,
            Category.PPP: 25,
            Category.PIM: 12,
            Category.HIT: 90,
            Category.SOG: 250,
            Category.BLK: 30,
        },
        is_goalie=False,
        aav=9_500_000,
    )


def test_ppp_sums_its_parts_when_split() -> None:
    split = StatMap({**MAP.categories, Category.PPP: ("6", "7")}, "0", "29", {})
    season = to_player_season(SKATER, line(SKATER, s0="10", s6="3", s7="4"), split)
    assert season.stats[Category.PPP] == 7


def test_dashes_and_missing_stats_count_as_zero() -> None:
    season = to_player_season(SKATER, line(SKATER, s0="-", s1="-", s2=""), MAP)
    assert season.gp == 0
    assert set(season.stats.values()) == {0}


def test_no_stat_line_means_no_games() -> None:
    season = to_player_season(SKATER, None, MAP)
    assert season.gp == 0
    assert set(season.stats.values()) == {0}
    assert len(season.stats) == 7


def test_goalie_uses_goalie_gp() -> None:
    season = to_player_season(GOALIE, line(GOALIE, s0="3", s29="55"), MAP)
    assert (season.gp, season.is_goalie) == (55, True)


def test_goalie_without_a_gp_stat_has_no_games() -> None:
    no_goalie_gp = StatMap(MAP.categories, "0", None, {})
    assert to_player_season(GOALIE, line(GOALIE, s0="3"), no_goalie_gp).gp == 0


@pytest.mark.parametrize(
    ("value", "message"),
    [("12a", "not a number"), ("inf", "not finite"), ("nan", "not finite")],
)
def test_bad_numbers_are_errors(value: str, message: str) -> None:
    with pytest.raises(StatMapError, match=message):
        to_player_season(SKATER, line(SKATER, s0="10", s1=value), MAP)


def test_fractional_gp_is_an_error() -> None:
    with pytest.raises(StatMapError, match="not a whole number"):
        to_player_season(SKATER, line(SKATER, s0="10.5"), MAP)


def test_whitespace_around_a_value_is_ignored() -> None:
    assert to_player_season(SKATER, line(SKATER, s0=" 12 "), MAP).gp == 12


def test_a_stat_line_for_another_player_is_refused() -> None:
    with pytest.raises(StatMapError, match=r"given for 465\.p\.2"):
        to_player_season(GOALIE, line(SKATER, s0="1"), MAP)


def test_goalie_stats_are_raw_with_none_for_no_value() -> None:
    stats = goalie_stats(line(GOALIE, s19="30", s23="2.45", s26="-"), MAP)
    assert stats == {"W": 30, "GAA": 2.45, "SV%": None}
    assert goalie_stats(None, MAP) == {"W": None, "GAA": None, "SV%": None}
