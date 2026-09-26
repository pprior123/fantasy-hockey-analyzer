"""Name normalization, nicknames, NHL team codes and position groups (SPEC §6)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fha.domain.names import (
    NHL_TEAMS,
    PositionGroup,
    canonical_team,
    name_keys,
    normalize_name,
    position_group,
    surname,
    team_spellings,
)

# ---------------------------------------------------------------- normalize_name


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("Connor McDavid", "connor mcdavid"),
        ("  Connor   McDavid ", "connor mcdavid"),
        ("Tim Stützle", "tim stutzle"),
        ("Nils Åman", "nils aman"),
        ("Alexis Lafrenière", "alexis lafreniere"),
        ("Mikael Pyyhtiä", "mikael pyyhtia"),
        ("Frederik Andersen", "frederik andersen"),
        ("Jonas Røndbjerg", "jonas rondbjerg"),
        ("Oliver Ekman-Larsson", "oliver ekman larsson"),
        ("Hardy Haman Aktell", "hardy haman aktell"),
        ("J.T. Compher", "jt compher"),
        ("J.T Compher", "jt compher"),
        ("Ryan O'Reilly", "ryan oreilly"),
        ("Ryan O\N{RIGHT SINGLE QUOTATION MARK}Reilly", "ryan oreilly"),
        ("Ryan O\N{MODIFIER LETTER APOSTROPHE}Reilly", "ryan oreilly"),
        ("McDavid, Connor", "connor mcdavid"),
        ("Carlsson,\N{NO-BREAK SPACE}Leo", "leo carlsson"),
        ("Di Giuseppe, Phillip", "phillip di giuseppe"),
        ("Leo\N{NO-BREAK SPACE}Carlsson", "leo carlsson"),
        ("Andersen", "andersen"),
        ("Andersen,", "andersen"),
        ("", ""),
        ("  ", ""),
    ],
)
def test_normalize_name(raw: str, normalized: str) -> None:
    assert normalize_name(raw) == normalized


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("\N{LATIN CAPITAL LETTER AE WITH MACRON}", "ae"),  # needs two folds: Ǣ -> ǣ -> æ -> ae
        ("\N{GREEK UPSILON WITH HOOK SYMBOL}", "\N{GREEK SMALL LETTER UPSILON}"),
    ],
)
def test_letters_that_need_two_folds_settle(raw: str, normalized: str) -> None:
    assert normalize_name(raw) == normalized


@given(st.text())
def test_normalize_name_is_idempotent(raw: str) -> None:
    once = normalize_name(raw)
    assert normalize_name(once) == once


NAME_PART = st.text(alphabet=st.characters(categories=["L"]), min_size=1, max_size=12)


@given(NAME_PART, NAME_PART)
def test_last_first_and_first_last_normalize_alike(first: str, last: str) -> None:
    assert normalize_name(f"{last}, {first}") == normalize_name(f"{first} {last}")


@given(st.text())
def test_normalized_names_are_plain_casefolded_words(raw: str) -> None:
    normalized = normalize_name(raw)
    assert normalized == " ".join(normalized.split())
    assert all(ch.isalnum() or ch == " " for ch in normalized)
    # Casefolded, not lowercased: casefold() maps lowercase Cherokee to uppercase.
    assert normalized == normalized.casefold()
    assert normalize_name(normalized) == normalized
    if raw.isascii():
        assert normalize_name(raw.upper()) == normalize_name(raw.lower()) == normalized


def test_cherokee_casefolds_to_uppercase() -> None:
    assert normalize_name("\uab70") == normalize_name("\u13a0") == "\u13a0"


# ---------------------------------------------------------------- name_keys (nicknames)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Matt Boldy", "Matthew Boldy"),
        ("Matty Beniers", "Matthew Beniers"),
        ("Matt Dumba", "Mathew Dumba"),
        ("Mitch Marner", "Mitchell Marner"),
        ("Alex Kerfoot", "Alexander Kerfoot"),
        ("Alexandre Carrier", "Alex Carrier"),
        ("Mike Matheson", "Michael Matheson"),
        ("Mikey Anderson", "Michael Anderson"),
        ("Will Borgen", "William Borgen"),
        ("Cam York", "Cameron York"),
        ("Cal Foote", "Callan Foote"),
        ("Josh Mahura", "Joshua Mahura"),
        ("Tim Stutzle", "Timothy Stützle"),
        ("Nick Paul", "Nicholas Paul"),
        ("Nicolas Petan", "Nic Petan"),
        ("Nick Perbix", "Nicklaus Perbix"),
        ("Zac Jones", "Zachary Jones"),
        ("Zach Werenski", "Zachary Werenski"),
        ("Johnny Gruden", "Jonathan Gruden"),
        ("Johnny Kovacevic", "Johnathan Kovacevic"),
        ("Jake Christiansen", "Jacob Christiansen"),
        ("Tom Wilson", "Thomas Wilson"),
        ("Teddy Blueger", "Theodor Blueger"),
        ("Charlie Mcavoy", "Charles McAvoy"),
        ("Joey Anderson", "Joseph Anderson"),
        ("Gabriel Vilardi", "Gabe Vilardi"),
        ("Tony Deangelo", "Anthony DeAngelo"),
        ("Phil Di Giuseppe", "Phillip Di Giuseppe"),
        ("Egor Zamula", "Yegor Zamula"),
        ("Vasily Ponomarev", "Vasiliy Ponomarev"),
        ("Louis Belpedio", "Louie Belpedio"),
        ("Nikolai Knyzhov", "Nicolai Knyzhov"),
        ("Max Willman", "Maxwell Willman"),
        ("Dmitri Voronkov", "Dimitri Voronkov"),
        ("Boldy, Matt", "Matthew Boldy"),
    ],
)
def test_nicknames_share_a_key(a: str, b: str) -> None:
    assert name_keys(a) & name_keys(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Matt Boldy", "Mitchell Boldy"),
        ("Alex Kerfoot", "Andrew Kerfoot"),
        ("Jack Hughes", "Quinn Hughes"),
        ("Cal Foote", "Nolan Foote"),
        ("Elias Pettersson", "Marcus Pettersson"),
    ],
)
def test_different_first_names_do_not_share_a_key(a: str, b: str) -> None:
    assert not name_keys(a) & name_keys(b)


def test_a_first_name_in_two_groups_has_both_keys() -> None:
    # Cal is short for Callan (Foote) and for Calvin (Clutterbuck).
    assert name_keys("Cal Foote") == {"callan foote", "calvin foote"}
    assert name_keys("Calvin Clutterbuck") & name_keys("Cal Clutterbuck")


def test_a_name_without_a_nickname_is_its_own_key() -> None:
    assert name_keys("Connor McDavid") == {"connor mcdavid"}
    assert name_keys("Andersen") == {"andersen"}
    assert name_keys("") == set()


# ---------------------------------------------------------------- surname


@pytest.mark.parametrize(
    ("raw", "last"),
    [
        ("Frederik Andersen", "andersen"),
        ("Andersen", "andersen"),
        ("Phillip Di Giuseppe", "di giuseppe"),
        ("Di Giuseppe, Phillip", "di giuseppe"),
        ("James van Riemsdyk", "van riemsdyk"),
        ("", ""),
    ],
)
def test_surname(raw: str, last: str) -> None:
    assert surname(raw) == last


# ---------------------------------------------------------------- canonical_team


def test_there_are_32_teams() -> None:
    assert len(NHL_TEAMS) == 32
    assert "UTA" in NHL_TEAMS


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("TB", "TBL"),
        ("TBL", "TBL"),
        ("tbl", "TBL"),
        ("Tampa Bay", "TBL"),
        ("Tampa", "TBL"),
        ("Lightning", "TBL"),
        ("Tampa Bay Lightning", "TBL"),
        ("NJ", "NJD"),
        ("NJD", "NJD"),
        ("New Jersey", "NJD"),
        ("SJ", "SJS"),
        ("SJS", "SJS"),
        ("San Jose", "SJS"),
        ("LA", "LAK"),
        ("LAK", "LAK"),
        ("Los Angeles", "LAK"),
        ("Montréal", "MTL"),
        ("Montreal", "MTL"),
        ("MTL", "MTL"),
        ("Mon", "MTL"),
        ("Habs", "MTL"),
        ("St. Louis", "STL"),
        ("St Louis", "STL"),
        ("StL", "STL"),
        ("Was", "WSH"),
        ("WSH", "WSH"),
        ("Nsh", "NSH"),
        ("CBJ", "CBJ"),
        ("CLS", "CBJ"),
        ("Columbus", "CBJ"),
        ("Blue Jackets", "CBJ"),
        ("VGK", "VGK"),
        ("Vegas", "VGK"),
        ("Golden Knights", "VGK"),
        ("UTA", "UTA"),
        ("Utah", "UTA"),
        ("Utah Mammoth", "UTA"),
        ("Utah Hockey Club", "UTA"),
        ("NYR", "NYR"),
        ("NY Rangers", "NYR"),
        ("Rangers", "NYR"),
        ("NYI", "NYI"),
        ("Islanders", "NYI"),
        ("Edm", "EDM"),
        ("  edmonton  ", "EDM"),
        ("Maple Leafs", "TOR"),
        ("Leafs", "TOR"),
    ],
)
def test_canonical_team(raw: str, code: str) -> None:
    assert canonical_team(raw) == code


@pytest.mark.parametrize(
    "raw", ["New York", "NY", "", "  ", "XYZ", "Arizona", "ARI", "Hartford", "Free Agent", None]
)
def test_unknown_or_ambiguous_teams_are_none(raw: str | None) -> None:
    assert canonical_team(raw) is None


def test_a_spelling_shared_by_two_teams_is_refused() -> None:
    teams = {"AAA": ("Springfield", "Cats", ()), "BBB": ("Shelbyville", "Dogs", ("Springfield",))}
    with pytest.raises(ValueError, match="team spelling 'Springfield' is ambiguous"):
        team_spellings(teams)


def test_every_canonical_code_maps_to_itself() -> None:
    assert all(canonical_team(code) == code for code in NHL_TEAMS)


# ---------------------------------------------------------------- position_group


@pytest.mark.parametrize(
    ("raw", "group"),
    [
        ("C", PositionGroup.FORWARD),
        ("L", PositionGroup.FORWARD),
        ("R", PositionGroup.FORWARD),
        ("LW", PositionGroup.FORWARD),
        ("RW", PositionGroup.FORWARD),
        ("W", PositionGroup.FORWARD),
        ("F", PositionGroup.FORWARD),
        ("C/LW", PositionGroup.FORWARD),
        ("LW,RW", PositionGroup.FORWARD),
        ("c, lw", PositionGroup.FORWARD),
        ("Center", PositionGroup.FORWARD),
        ("Centre", PositionGroup.FORWARD),
        ("Left Wing", PositionGroup.FORWARD),
        ("Winger", PositionGroup.FORWARD),
        ("Forward", PositionGroup.FORWARD),
        ("D", PositionGroup.D),
        ("LD", PositionGroup.D),
        ("Defense", PositionGroup.D),
        ("Defence", PositionGroup.D),
        ("Defenseman", PositionGroup.D),
        ("G", PositionGroup.G),
        ("Goalie", PositionGroup.G),
        ("Goaltender", PositionGroup.G),
    ],
)
def test_position_group(raw: str, group: PositionGroup) -> None:
    assert position_group(raw) is group


@pytest.mark.parametrize("raw", ["", "  ", "BN", "IR", "Util", "C/D", "F,G", "X", None])
def test_unknown_or_mixed_positions_are_none(raw: str | None) -> None:
    assert position_group(raw) is None
