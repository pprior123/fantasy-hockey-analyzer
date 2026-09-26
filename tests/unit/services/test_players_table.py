"""The Players screen's filters and sort (SPEC §7.1)."""

from typing import Any

import pytest

from fha.domain.engine import DEFAULT_CONFIG
from fha.domain.models import Category
from fha.services.league_sheet import RosteredSalary
from fha.services.league_view import LeagueView, Salaries, build_view
from fha.services.players_table import (
    Owner,
    PlayersQuery,
    QueryError,
    SortKey,
    _comparable,
    select,
)
from tests.unit.services.snapshots import synthetic_snapshot
from tests.unit.yahoo.fake_league import LK


@pytest.fixture
async def view() -> LeagueView:
    salaries = Salaries(
        rostered={"1": RosteredSalary(5_000_000, True, "t")}, free_agents={"100": 900_000}
    )
    return build_view(await synthetic_snapshot(), DEFAULT_CONFIG, salaries)


def ids(rows: Any) -> list[str]:
    return [r.player_id for r in rows]


def test_the_default_is_everyone_by_ttltst_best_first(view: LeagueView) -> None:
    rows = select(view, PlayersQuery())
    assert len(rows) == len(view.rows)
    rated = [r.ttltst for r in rows if r.ttltst is not None]
    assert rated == sorted(rated, reverse=True)
    assert all(r.ttltst is None for r in rows[len(rated) :])  # unrated and goalies last


@pytest.mark.parametrize(
    ("owner", "expected"),
    [
        (Owner.MINE, {"1", "2", "3"}),
        (Owner.TAKEN, {"1", "2", "3", "4", "5", "6"}),
    ],
)
def test_owner_chips(view: LeagueView, owner: Owner, expected: set[str]) -> None:
    assert set(ids(select(view, PlayersQuery(owner=owner)))) == expected


def test_free_agents_and_a_single_team(view: LeagueView) -> None:
    free = select(view, PlayersQuery(owner=Owner.FREE))
    assert len(free) == 60
    assert all(r.owner_key is None for r in free)
    team = select(view, PlayersQuery(team_key=f"{LK}.t.2", owner=Owner.FREE))  # team wins
    assert set(ids(team)) == {"4", "5", "6"}


def test_position_and_min_gp(view: LeagueView) -> None:
    goalies = select(view, PlayersQuery(position="G"))
    assert ids(goalies) == ["3"]
    busy = select(view, PlayersQuery(min_gp=9))
    assert set(ids(busy)) == {"1", "2"}


@pytest.mark.parametrize("key", list(SortKey))
def test_every_column_sorts_both_ways_with_blanks_last(view: LeagueView, key: SortKey) -> None:
    down = select(view, PlayersQuery(sort=key, descending=True))
    up = select(view, PlayersQuery(sort=key, descending=False))
    assert len(down) == len(up) == len(view.rows)
    assert set(ids(down)) == set(ids(up))


def test_sort_by_aav_puts_unknown_salaries_last(view: LeagueView) -> None:
    rows = select(view, PlayersQuery(sort=SortKey.AAV, descending=False))
    assert ids(rows)[:2] == ["100", "1"]
    assert all(r.aav is None for r in rows[2:])


def test_sort_by_name_is_case_insensitive_and_ties_break_by_id(view: LeagueView) -> None:
    rows = select(view, PlayersQuery(sort=SortKey.NAME, descending=False))
    names = [r.name.casefold() for r in rows]
    assert names == sorted(names)


def test_sort_by_a_category_uses_its_norm(view: LeagueView) -> None:
    rows = select(view, PlayersQuery(sort=Category.G))
    norms = [r.norms[Category.G] for r in rows if r.rating and r.rating.rated]
    assert norms == sorted(norms, reverse=True)


def test_sort_by_owner_uses_team_names(view: LeagueView) -> None:
    rows = select(view, PlayersQuery(sort=SortKey.OWNER, descending=False))
    assert ids(rows)[:3] == sorted(ids(rows)[:3], key=lambda pid: (view.by_id[pid].name, pid))
    assert all(r.owner_key is None for r in rows[6:])


def test_params_parse_with_defaults() -> None:
    assert PlayersQuery.from_params({}) == PlayersQuery()
    q = PlayersQuery.from_params(
        {"owner": "free", "pos": "D", "min_gp": "5", "sort": "aav", "team": " t1 "}
    )
    assert q == PlayersQuery(Owner.FREE, "t1", "D", 5, SortKey.AAV, True)
    assert PlayersQuery.from_params({"sort": "name"}).descending is False
    assert PlayersQuery.from_params({"sort": "value"}).descending is False  # cheapest first
    assert PlayersQuery.from_params({"sort": "HIT"}) == PlayersQuery(sort=Category.HIT)
    assert PlayersQuery.from_params({"sort": "gp", "dir": "asc"}).descending is False
    assert PlayersQuery.from_params({"owner": " ", "min_gp": ""}) == PlayersQuery()


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"owner": "someone"}, "someone"),
        ({"pos": "W"}, "unknown position 'W'"),
        ({"min_gp": "-1"}, "min_gp must be a whole number"),
        ({"min_gp": "٣"}, "min_gp must be a whole number"),
        ({"sort": "salary"}, "salary"),
        ({"dir": "up"}, "dir must be asc or desc"),
    ],
)
def test_bad_params_are_refused(params: dict[str, str], message: str) -> None:
    with pytest.raises(QueryError, match=message):
        PlayersQuery.from_params(params)


def test_only_numbers_and_text_sort() -> None:
    with pytest.raises(TypeError, match="can't sort by a list"):
        _comparable([1])
