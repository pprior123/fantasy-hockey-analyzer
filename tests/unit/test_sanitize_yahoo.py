"""scripts/sanitize_yahoo.py: what may and may not reach committed fixtures."""

import copy
import json
from typing import Any

import pytest

from scripts.sanitize_yahoo import TEAM_LOGO, private_names, problems, sanitize, team_name
from tests.unit.yahoo import builders as b
from tests.unit.yahoo.fake_league import MINE, THEIRS


def dump(node: Any) -> str:
    return json.dumps(node)


def test_team_name_uses_the_team_number() -> None:
    assert team_name("465.l.8076.t.7") == "Team 7"
    with pytest.raises(ValueError, match="not a team key"):
        team_name("465.l.8076")
    with pytest.raises(ValueError, match="not a team key"):
        team_name("465.l.8076.t.x")


def test_rosters_lose_managers_team_names_and_logos() -> None:
    raw = b.teams_roster([MINE, THEIRS])
    raw_text = dump(raw)
    assert "someone@example.com" in raw_text  # the synthetic data does carry them
    clean = sanitize(raw)
    text = dump(clean)
    for gone in ("managers", "someone@example.com", "GUIDGUIDGUID", "Someone", "Name 1", "l.png"):
        assert gone not in text
    meta = clean["league"][1]["teams"]["0"]["team"][0]
    assert {"name": "Team 1"} in meta
    assert {"team_key": f"{b.LEAGUE_KEY}.t.1"} in meta
    assert {"team_logos": TEAM_LOGO} in meta
    assert {"is_owned_by_current_login": 1} in meta
    assert problems(clean) == []


def test_player_data_is_kept() -> None:
    raw = b.teams_roster([MINE])
    clean = sanitize(raw)
    player = clean["league"][1]["teams"]["0"]["team"][1]["roster"]["0"]["players"]["0"]["player"]
    original = raw["league"][1]["teams"]["0"]["team"][1]["roster"]["0"]["players"]["0"]["player"]
    assert player == original


def test_scoreboard_teams_are_sanitized_too() -> None:
    clean = sanitize(b.scoreboard(1, [(3, 4)]))
    matchup = clean["league"][1]["scoreboard"]["0"]["matchups"]["0"]["matchup"]
    names = [
        next(f["name"] for f in matchup["0"]["teams"][i]["team"][0] if "name" in f)
        for i in ("0", "1")
    ]
    assert names == ["Team 3", "Team 4"]
    assert problems(clean) == []


def test_league_name_and_invites_are_replaced_or_dropped() -> None:
    meta = b.league_meta(
        name="Real League Name",
        logo_url="https://example.test/logo.png",
        short_invitation_url="https://y.ahoo.it/join",
        iris_group_chat_id="chat-1",
        password="hunter2",  # noqa: S106 - a league join password, dropped
    )
    clean = sanitize({"league": [meta]})
    assert clean["league"][0]["name"] == "League"
    for key in ("logo_url", "short_invitation_url", "iris_group_chat_id", "password"):
        assert key not in clean["league"][0]
    assert clean["league"][0]["league_key"] == b.LEAGUE_KEY


def test_a_flat_team_object_is_sanitized() -> None:
    team = {"team_key": "465.l.8076.t.2", "name": "Real", "team_logos": [1], "managers": [2]}
    assert sanitize(team) == {
        "team_key": "465.l.8076.t.2",
        "name": "Team 2",
        "team_logos": TEAM_LOGO,
    }


def test_token_fields_are_dropped_anywhere() -> None:
    clean = sanitize({"a": [{"access_token": "x", "refresh_token": "y", "id_token": "z", "k": 1}]})
    assert clean == {"a": [{"k": 1}]}


def test_sanitize_does_not_modify_its_input() -> None:
    raw = b.teams_roster([MINE])
    before = copy.deepcopy(raw)
    sanitize(raw)
    assert raw == before


def test_player_fragment_names_are_not_mistaken_for_team_names() -> None:
    clean = sanitize(b.league_players([MINE.roster[0][0]]))
    meta = clean["league"][1]["players"]["0"]["player"][0]
    assert {"name": {"full": "Player 1", "first": "Player", "last": "1"}} in meta


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        ({"a": {"guid": "x"}}, ["$.a.guid: forbidden key"]),
        ({"a": [{"email": "x"}]}, ["$.a[0].email: forbidden key"]),
        ({"managers": []}, ["$.managers: forbidden key"]),
        ({"nickname": "n"}, ["$.nickname: forbidden key"]),
        ({"refresh_token": "t"}, ["$.refresh_token: forbidden key"]),
        ({"x": ["contact me: a.b+c@mail.example.org"]}, ["$.x[0]: email address"]),
        ({"x": "https://sports.yahoo.com/nhl/players/1"}, []),
        ({"x": "@handle"}, []),
        ({"n": 5, "b": None}, []),
    ],
)
def test_problems_finds_forbidden_keys_and_emails(node: Any, expected: list[str]) -> None:
    assert problems(node) == expected


def test_raw_synthetic_rosters_have_problems() -> None:
    found = problems(b.teams_roster([MINE]))
    assert any(p.endswith(".managers: forbidden key") for p in found)


def test_private_names_are_the_team_and_league_names() -> None:
    raw = {
        "league": [
            b.league_meta(name="Real League"),
            b.teams_roster([b.T(1, name="Bunch"), b.T(2, name="Team 2")])["league"][1],
        ],
        "flat": {"team_key": "465.l.8076.t.3", "name": "Flat Out"},
    }
    # "Team 2" is our placeholder; "Someone" is the synthetic managers' nickname
    assert private_names(raw) == {"Real League", "Bunch", "Flat Out", "Someone"}


def test_player_names_are_not_private_names() -> None:
    names = private_names(b.league_players([MINE.roster[0][0]]))
    assert names == {"Synthetic League"}  # the page's league, not "Player 1"


@pytest.mark.parametrize(
    ("text", "leaks"),
    [
        ("Week 3 recap: the bunch rolls on", True),  # case-insensitive, anywhere
        ("Bunch", True),
        ("Bun", False),
        ("Ab", True),  # a short name only matches the whole string
        ("Abs and more", False),
    ],
)
def test_a_private_name_anywhere_is_a_problem(text: str, leaks: bool) -> None:
    found = problems({"recap": {"title": text}}, frozenset({"Bunch", "Ab"}))
    assert found == (["$.recap.title: a team or league name"] if leaks else [])


def test_manager_nicknames_are_private_names_and_names_are_trimmed() -> None:
    raw = {
        "managers": [{"manager": {"nickname": "Someone"}}],
        "t": [{"team_key": "k.t.1"}, {"name": " Bunch "}],
    }
    assert private_names(raw) == {"Someone", "Bunch"}


def test_public_player_fields_may_repeat_a_team_name() -> None:
    kings = b.P("8", "Anze Kings", "LA")  # a surname that is also a fantasy team's name
    page = b.league_players([kings])
    meta = page["league"][1]["players"]["0"]["player"][0]
    meta.append({"editorial_team_full_name": "Los Angeles Kings"})
    assert problems(sanitize(page), frozenset({"Kings"})) == []
    assert problems({"recap": "Kings win"}, frozenset({"Kings"})) == [
        "$.recap: a team or league name"
    ]


def test_sanitized_rosters_hold_no_private_name() -> None:
    raw = b.teams_roster([b.T(1, name="Bunch"), THEIRS])
    assert problems(sanitize(raw), frozenset(private_names(raw))) == []
