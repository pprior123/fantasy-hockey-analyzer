"""Names, NHL teams and positions as the matcher compares them (SPEC §6). Pure.

Sources spell the same player and team differently: Yahoo "Tim Stutzle" /
TB, PuckPedia "Stützle, Tim" / TBL, the league sheet "Tim Stützle" /
"Tampa Bay". Everything here maps those spellings to one comparable form,
and maps what it can't recognize to None, never to a guess.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from enum import StrEnum

# Letters NFKD doesn't decompose into a base letter plus accents.
SPECIAL_LETTERS = str.maketrans(
    {
        "ø": "o",
        "æ": "ae",
        "œ": "oe",
        "ß": "ss",
        "ł": "l",
        "đ": "d",
        "ð": "d",
        "þ": "th",
        "\N{LATIN SMALL LETTER DOTLESS I}": "i",
    }
)
# O'Reilly -> oreilly, J.T. -> jt; any other mark splits words.
DROPPED = frozenset(
    "'`.\N{RIGHT SINGLE QUOTATION MARK}\N{LEFT SINGLE QUOTATION MARK}\N{MODIFIER LETTER APOSTROPHE}"
)

# Equivalent first names. A name may sit in several groups (Cal: Callan or
# Calvin); two names match if they share a group. Mostly from the workbook's
# 57 aliases (tests/fixtures/alias_seed.json), plus the obvious short forms of
# the same names. Deliberately small: anything else falls to aliases or to
# fuzzy review.
NICKNAMES: tuple[frozenset[str], ...] = tuple(
    frozenset(group.split())
    for group in (
        "matthew matt matty mathew",
        "mitchell mitch",
        "alexander alex alexandre alexandr",
        "michael mike mikey",
        "william will",
        "cameron cam",
        "callan cal",
        "calvin cal",
        "joshua josh",
        "timothy tim",
        "nicholas nick nic nicolas nicklaus",
        "nikolai nicolai nikolay",
        "zachary zach zac zack",
        "jonathan johnathan johnny jon john",
        "jacob jake jakob",
        "thomas tom tommy",
        "theodor theodore teddy ted",
        "charles charlie",
        "joseph joey joe",
        "gabriel gabe",
        "anthony tony",
        "phillip philip phil",
        "yegor egor",
        "vasiliy vasily vasili",
        "louis louie",
        "maxwell max",
        "dimitri dmitri dmitry",
        "samuel sam",
        "benjamin ben",
        "daniel dan danny",
        "christopher chris",
        "patrick pat",
    )
)
_GROUPS_BY_NAME: dict[str, list[frozenset[str]]] = {}
for _group in NICKNAMES:
    for _name in _group:
        _GROUPS_BY_NAME.setdefault(_name, []).append(_group)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold().translate(SPECIAL_LETTERS))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def normalize_name(raw: str) -> str:
    """Casefolded words without accents or punctuation, "Last, First" as "first last"."""
    last, comma, first = raw.partition(",")
    text = f"{first} {last}" if comma else raw
    # One pass can expose more work (Ǣ -> ǣ -> æ -> ae); two always settle, checked
    # over every Unicode code point.
    text = _fold(_fold(text))
    kept = "".join("" if ch in DROPPED else ch if ch.isalnum() else " " for ch in text)
    return " ".join(kept.split())


def surname(raw: str) -> str:
    """Everything after the first name ("di giuseppe"); a single word is the surname.

    Split on the raw name's whitespace before normalizing, so a hyphenated first
    name stays one word ("Jean-Gabriel Pageau" -> "pageau").
    """
    last, comma, _ = raw.partition(",")
    if comma:
        return normalize_name(last)
    words = raw.split(None, 1)
    return normalize_name(words[-1]) if words else ""


def name_keys(raw: str) -> set[str]:
    """Comparable forms of a name: one per nickname group its first name is in."""
    normalized = normalize_name(raw)
    if not normalized:
        return set()
    first, _, rest = normalized.partition(" ")
    groups = _GROUPS_BY_NAME.get(first)
    if not groups:
        return {normalized}
    return {" ".join(filter(None, (min(g, key=_canonical_order), rest))) for g in groups}


def _canonical_order(name: str) -> tuple[int, str]:
    """A group's key is its longest name (the full form), alphabetically first on ties."""
    return (-len(name), name)


# ---------------------------------------------------------------- NHL teams

# Canonical code -> (city, nickname, other spellings). Codes are the NHL's
# own three-letter codes (PuckPedia uses them); Yahoo's abbreviations and
# the sheet's city names and nicknames map onto them.
_TEAMS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "ANA": ("Anaheim", "Ducks", ("ANH",)),
    "BOS": ("Boston", "Bruins", ()),
    "BUF": ("Buffalo", "Sabres", ()),
    "CGY": ("Calgary", "Flames", ("CAL",)),
    "CAR": ("Carolina", "Hurricanes", ("Canes",)),
    "CHI": ("Chicago", "Blackhawks", ()),
    "COL": ("Colorado", "Avalanche", ("Avs",)),
    "CBJ": ("Columbus", "Blue Jackets", ("CLB", "CLS")),
    "DAL": ("Dallas", "Stars", ()),
    "DET": ("Detroit", "Red Wings", ()),
    "EDM": ("Edmonton", "Oilers", ()),
    "FLA": ("Florida", "Panthers", ("FLO",)),
    "LAK": ("Los Angeles", "Kings", ("LA",)),
    "MIN": ("Minnesota", "Wild", ()),
    "MTL": ("Montreal", "Canadiens", ("MON", "Habs")),
    "NSH": ("Nashville", "Predators", ("NAS", "Preds")),
    "NJD": ("New Jersey", "Devils", ("NJ",)),
    "NYI": ("NY Islanders", "Islanders", ("New York Islanders", "Isles")),
    "NYR": ("NY Rangers", "Rangers", ("New York Rangers",)),
    "OTT": ("Ottawa", "Senators", ("Sens",)),
    "PHI": ("Philadelphia", "Flyers", ("Philly",)),
    "PIT": ("Pittsburgh", "Penguins", ("Pens",)),
    "SJS": ("San Jose", "Sharks", ("SJ",)),
    "SEA": ("Seattle", "Kraken", ()),
    "STL": ("St Louis", "Blues", ("Saint Louis",)),
    "TBL": ("Tampa Bay", "Lightning", ("TB", "Tampa", "Bolts")),
    "TOR": ("Toronto", "Maple Leafs", ("Leafs",)),
    "UTA": ("Utah", "Mammoth", ("UTAH", "Utah Hockey Club", "Utah HC")),
    "VAN": ("Vancouver", "Canucks", ()),
    "VGK": ("Vegas", "Golden Knights", ("VEG", "Las Vegas")),
    "WSH": ("Washington", "Capitals", ("WAS", "Caps")),
    "WPG": ("Winnipeg", "Jets", ("WIN",)),
}
NHL_TEAMS: frozenset[str] = frozenset(_TEAMS)


def _team_key(raw: str) -> str:
    return normalize_name(raw)


def team_spellings(teams: Mapping[str, tuple[str, str, tuple[str, ...]]]) -> dict[str, str]:
    """Every spelling of every team -> its code; a spelling shared by two teams is an error."""
    table: dict[str, str] = {}
    for code, (city, nickname, others) in teams.items():
        codes = [code, *(o for o in others if o.isupper())]  # other codes: "TB", "LA", "CLS"
        with_nickname = [f"{c} {nickname}" for c in codes]  # "LA Kings", "TBL Lightning"
        for spelling in (code, city, nickname, f"{city} {nickname}", *others, *with_nickname):
            if table.setdefault(_team_key(spelling), code) != code:
                raise ValueError(f"team spelling {spelling!r} is ambiguous")
    return table


TEAM_SPELLINGS: dict[str, str] = team_spellings(_TEAMS)


def canonical_team(raw: str | None) -> str | None:
    """The NHL code for a team as any source writes it; None if unknown or ambiguous.

    "New York" is ambiguous (Rangers or Islanders) and so is None; Arizona
    moved to Utah as a new franchise, so it isn't mapped either.
    """
    if raw is None:
        return None
    return TEAM_SPELLINGS.get(_team_key(raw))


# ---------------------------------------------------------------- positions


class PositionGroup(StrEnum):
    """What breaks ties between players with one name (SPEC §6, owner-approved)."""

    FORWARD = "F"
    D = "D"
    G = "G"


_POSITIONS: dict[str, PositionGroup] = {
    **dict.fromkeys(
        ("c", "l", "r", "lw", "rw", "w", "f", "center", "centre", "wing", "winger", "forward"),
        PositionGroup.FORWARD,
    ),
    **dict.fromkeys(
        ("d", "ld", "rd", "defense", "defence", "defenseman", "defenceman"), PositionGroup.D
    ),
    **dict.fromkeys(("g", "goalie", "goaltender", "goalkeeper"), PositionGroup.G),
}
_POSITION_PHRASES = {"left wing": "lw", "right wing": "rw"}


def position_group(raw: str | None) -> PositionGroup | None:
    """F, D or G for a position string ("C/LW", "LW,RW", "Defense"); None if unknown or mixed."""
    if raw is None:
        return None
    text = normalize_name(raw.replace("/", " ").replace(",", " "))
    for phrase, code in _POSITION_PHRASES.items():
        text = text.replace(phrase, code)
    groups = {_POSITIONS.get(token) for token in text.split()}
    if len(groups) != 1 or None in groups:
        return None
    return groups.pop()
