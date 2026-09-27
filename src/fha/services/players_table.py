"""The Players screen's filters and sort (SPEC §7.1), over a ``LeagueView``.

Pure: the query (from the URL) in, the rows to show out. Ranks and
percentiles never change with a filter (SPEC §5: they are over the whole
pool); filters only choose which rows are shown.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from fha.domain.models import Category
from fha.services.league_view import LeagueView, PlayerRow


class Owner(StrEnum):
    ALL = "all"
    MINE = "mine"
    FREE = "free"  # free agents and waivers: not on any roster
    TAKEN = "taken"  # on some roster


class SortKey(StrEnum):
    NAME = "name"
    POS = "pos"
    TEAM = "team"
    OWNER = "owner"
    GP = "gp"
    TTLTST = "ttltst"
    PCTL = "pctl"
    AAV = "aav"
    VALUE = "value"  # $/TTLTST


POSITIONS = ("C", "LW", "RW", "D", "G")
MAX_MIN_GP_DIGITS = 4  # a season is 82 games; this keeps int() off huge strings
# Sensible first direction per column: best first for ratings, cheapest first for money.
DESCENDING_FIRST = frozenset({SortKey.GP, SortKey.TTLTST, SortKey.PCTL, SortKey.AAV})


class QueryError(ValueError):
    """A filter or sort the Players screen doesn't know."""


@dataclass(frozen=True)
class PlayersQuery:
    owner: Owner = Owner.ALL
    team_key: str | None = None  # a single team (the team picker); overrides ``owner``
    position: str | None = None  # one of POSITIONS
    min_gp: int = 0
    sort: SortKey | Category = SortKey.TTLTST  # a category sorts by its norm
    descending: bool = True

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> PlayersQuery:
        """From query-string values; blank means the default. Unknown values are refused."""

        def get(name: str) -> str | None:
            value = (params.get(name) or "").strip()
            return value or None

        for name in ("owner", "pos", "sort", "dir"):  # the 400 page quotes 12 characters at most
            if len(value := get(name) or "") > 12:
                raise QueryError(f"unknown {name} {value[:12] + '…'!r}")
        try:
            owner = Owner(get("owner") or Owner.ALL)
            position = get("pos")
            if position is not None and position not in POSITIONS:
                raise QueryError(f"unknown position {position!r}")
            min_gp_text = get("min_gp") or "0"
            digits = min_gp_text.isascii() and min_gp_text.isdigit()
            if not digits or len(min_gp_text) > MAX_MIN_GP_DIGITS:
                shown = min_gp_text if len(min_gp_text) <= 12 else min_gp_text[:12] + "…"
                raise QueryError(f"min_gp must be a whole number up to 9999, got {shown!r}")
            min_gp = int(min_gp_text)
            sort_text = get("sort") or SortKey.TTLTST.value
            sort: SortKey | Category
            if sort_text in {c.value for c in Category}:
                sort = Category(sort_text)
            else:
                sort = SortKey(sort_text)
            direction = get("dir")
            if direction not in (None, "asc", "desc"):
                raise QueryError(f"dir must be asc or desc, got {direction!r}")
        except ValueError as e:
            raise QueryError(str(e)) from None
        descending = (
            direction == "desc"
            if direction
            else (isinstance(sort, Category) or sort in DESCENDING_FIRST)
        )
        return cls(owner, get("team"), position, min_gp, sort, descending)


def select(view: LeagueView, query: PlayersQuery) -> list[PlayerRow]:
    """The rows the query shows, sorted; players with no value in the sort column last."""
    mine = view.my_team.team_key if view.my_team else None

    def shown(row: PlayerRow) -> bool:
        if not _owned_as_asked(row, query, mine):
            return False
        if query.position is not None and query.position not in row.eligible_positions:
            return False
        return row.gp >= query.min_gp

    rows = [r for r in view.rows if shown(r)]
    names = {t.team_key: t.name for t in view.teams}
    key = _sort_value(query.sort, names)
    present = [r for r in rows if key(r) is not None]
    missing = [r for r in rows if key(r) is None]
    present.sort(key=lambda r: (r.name, r.player_id))  # stable tie order: name, then id
    present.sort(key=lambda r: _comparable(key(r)), reverse=query.descending)
    return [*present, *missing]


def _owned_as_asked(row: PlayerRow, query: PlayersQuery, mine: str | None) -> bool:
    if query.team_key is not None:  # the team picker wins over the chips
        return row.owner_key == query.team_key
    match query.owner:
        case Owner.MINE:
            return mine is not None and row.owner_key == mine
        case Owner.FREE:
            return row.owner_key is None
        case Owner.TAKEN:
            return row.owner_key is not None
        case _:
            return True


def _comparable(value: object) -> tuple[int, float | str]:
    if isinstance(value, str):
        return (1, value.casefold())
    if isinstance(value, int | float):
        return (0, float(value))
    raise TypeError(f"can't sort by a {type(value).__name__}")


def _sort_value(
    sort: SortKey | Category, team_names: Mapping[str, str]
) -> Callable[[PlayerRow], object | None]:
    if isinstance(sort, Category):
        return lambda r: r.norms.get(sort) if r.rating and r.rating.rated else None
    by: dict[SortKey, Callable[[PlayerRow], object | None]] = {
        SortKey.NAME: lambda r: r.name,
        SortKey.POS: lambda r: r.position,
        SortKey.TEAM: lambda r: r.nhl_team or None,
        SortKey.OWNER: lambda r: team_names.get(r.owner_key) if r.owner_key else None,
        SortKey.GP: lambda r: r.gp,
        SortKey.TTLTST: lambda r: r.ttltst,
        SortKey.PCTL: lambda r: r.percentile,
        SortKey.AAV: lambda r: r.aav,
        SortKey.VALUE: lambda r: (
            r.value if r.value is not None and math.isfinite(r.value) else None
        ),
    }
    return by[sort]
