"""The Players screen's filters and sort (SPEC §7.1)."""

from dataclasses import replace
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
from fha.sources.yahoo.demo import demo_snapshot
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


def _demo_view() -> LeagueView:
    """The demo league: many teams, positions and NHL teams, a few salaries."""
    snap = demo_snapshot()
    teamless = replace(snap.available[0], nhl_team="")  # Yahoo's "" for no NHL team
    snap = replace(snap, available=(teamless, *snap.available[1:]))
    ids_ = [p.player_id for p in snap.pool if not p.is_goalie]
    salaries = Salaries(
        rostered={ids_[0]: RosteredSalary(5_000_000, True, "t")},
        free_agents={pid: 750_000 * (i + 1) for i, pid in enumerate(ids_[-6:])},
    )
    return build_view(snap, DEFAULT_CONFIG, salaries)


DEMO_VIEW = _demo_view()
NAMES = {t.team_key: t.name for t in DEMO_VIEW.teams}
# Each column's value, read straight off the row (not through the code under test).
COLUMN: dict[SortKey | Category, Any] = {
    SortKey.NAME: lambda r: r.name.casefold(),
    SortKey.POS: lambda r: r.position.casefold(),
    SortKey.TEAM: lambda r: r.nhl_team.casefold() or None,
    SortKey.OWNER: lambda r: NAMES[r.owner_key].casefold() if r.owner_key else None,
    SortKey.GP: lambda r: r.gp,
    SortKey.TTLTST: lambda r: r.ttltst,
    SortKey.PCTL: lambda r: r.percentile,
    SortKey.AAV: lambda r: r.aav,
    SortKey.VALUE: lambda r: r.value,
    Category.HIT: lambda r: r.norms.get(Category.HIT) if r.rating and r.rating.rated else None,
}


@pytest.mark.parametrize("descending", [True, False])
@pytest.mark.parametrize("key", list(COLUMN))
def test_every_column_sorts_in_order_with_ties_by_name_and_blanks_last(
    key: SortKey | Category, descending: bool
) -> None:
    """The exact order (M4R1B-5): by the column's value in the asked direction, ties by
    name then id, and rows with no value last, in display order."""
    value = COLUMN[key]
    rows = select(DEMO_VIEW, PlayersQuery(sort=key, descending=descending))
    present = [r for r in DEMO_VIEW.rows if value(r) is not None]
    blank = [r for r in DEMO_VIEW.rows if value(r) is None]
    assert len({value(r) for r in present}) > 2  # a real sort, not a trivial one
    expected = sorted(present, key=lambda r: (r.name, r.player_id))
    expected.sort(key=value, reverse=descending)
    assert ids(rows) == ids(expected) + ids(blank)


def test_blank_columns_are_blanks_not_values() -> None:
    """Free agents have no owner, one has no NHL team, unrated rows have no TTLTST."""
    for key in (SortKey.TEAM, SortKey.OWNER, SortKey.TTLTST, SortKey.AAV, SortKey.VALUE):
        rows = select(DEMO_VIEW, PlayersQuery(sort=key))
        assert any(COLUMN[key](r) is None for r in rows), key


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
        ({"min_gp": "1" * 4301}, r"min_gp must be a whole number up to 9999, got '111111111111…'"),
        ({"min_gp": "10000"}, "up to 9999"),
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


async def test_my_team_chip_is_empty_without_my_team() -> None:
    """Without the owner's team in the data, "My Team" lists nobody (not the free
    agents, whose owner is also None)."""
    snap = await synthetic_snapshot()
    orphan = replace(snap, teams=tuple(replace(t, is_mine=False) for t in snap.teams))
    view = build_view(orphan, DEFAULT_CONFIG, Salaries())
    assert select(view, PlayersQuery(owner=Owner.MINE)) == []


def test_the_position_filter_uses_eligible_positions_not_the_display_text() -> None:
    snap = demo_snapshot()
    target = next(p for p in snap.available if p.eligible_positions == ("C",))
    wider = replace(target, eligible_positions=("C", "LW"))  # display still "C"
    snap = replace(snap, available=tuple(wider if p is target else p for p in snap.available))
    view = build_view(snap, DEFAULT_CONFIG, Salaries())
    assert target.player_id in ids(select(view, PlayersQuery(position="LW")))


def test_equal_names_break_ties_by_player_id() -> None:
    """Two players with one name (like the two Elias Petterssons) and equal values:
    player_id decides, in the sort's direction-independent tie order."""
    snap = demo_snapshot()
    a, b = snap.available[0], snap.available[1]
    twins = (replace(a, name="Elias Twin"), replace(b, name="Elias Twin"))
    snap = replace(snap, available=(*twins, *snap.available[2:]))
    view = build_view(snap, DEFAULT_CONFIG, Salaries())
    rows = [r for r in select(view, PlayersQuery(sort=SortKey.NAME, descending=False))]
    got = [r.player_id for r in rows if r.name == "Elias Twin"]
    assert got == sorted([a.player_id, b.player_id])
