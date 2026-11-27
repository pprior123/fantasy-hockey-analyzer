"""The NHL teams that play tonight and the two sides of a game."""

import pytest

from fha.domain.schedule import Game, Mark, mark, tonight_games


def test_tonight_games_indexes_both_teams_in_regular_season_and_playoff_games() -> None:
    preseason = Game("CHI", "DET", 300, 1)
    regular = Game("TBL", "BOS", 400, 2)
    playoffs = Game("EDM", "VAN", 500, 3)
    other = Game("ANA", "SJS", 600, 4)
    assert tonight_games((preseason, regular, playoffs, other)) == {
        "TBL": regular,
        "BOS": regular,
        "EDM": playoffs,
        "VAN": playoffs,
    }


def test_an_empty_slate_has_no_teams_playing() -> None:
    assert tonight_games(()) == {}


@pytest.mark.parametrize(
    ("team", "expected"),
    [("TBL", Mark("at", "BOS", 400)), ("BOS", Mark("vs", "TBL", 400))],
    ids=["away", "home"],
)
def test_mark_gives_the_players_side_opponent_and_time(team: str, expected: Mark) -> None:
    assert mark(team, Game("TBL", "BOS", 400, 2)) == expected


def test_mark_keeps_the_opponent_if_the_start_time_is_unknown() -> None:
    assert mark("BOS", Game("TBL", "BOS", None, 2)) == Mark("vs", "TBL", None)
