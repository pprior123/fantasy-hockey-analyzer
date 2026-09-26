"""The stats cache's snapshot encoding: exact round trips, strict decoding."""

import copy
from typing import Any

import pytest

from fha.services import snapshot_codec
from fha.services.snapshot_codec import SnapshotCodecError, decode, encode
from tests.unit.services.snapshots import synthetic_snapshot


async def test_a_full_snapshot_round_trips_exactly() -> None:
    snap = await synthetic_snapshot()
    assert snap.last_season_stats is not None
    assert snap.next_scoreboard is not None
    meta, lists = encode(snap)
    assert decode(copy.deepcopy(meta), copy.deepcopy(lists)) == snap


async def test_without_last_season_or_a_next_week() -> None:
    snap = await synthetic_snapshot(last_season=False, current_week=25)
    assert snap.next_scoreboard is None
    meta, lists = encode(snap)
    assert lists["last_season_stats"] == []
    assert decode(meta, lists) == snap


async def encoded() -> tuple[dict[str, Any], dict[str, list[Any]]]:
    return encode(await synthetic_snapshot())


async def test_an_older_format_is_refused() -> None:
    meta, lists = await encoded()
    meta["format"] = snapshot_codec.FORMAT - 1
    with pytest.raises(SnapshotCodecError, match="cache format 0, expected 1"):
        decode(meta, lists)


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda m, ls: m.pop("teams"), r"\['teams'\] missing"),
        (lambda m, ls: m["game"].update(season="2026"), "expected an integer, got str"),
        (lambda m, ls: m["settings"].update(num_teams=True), "expected an integer, got bool"),
        (lambda m, ls: m["teams"][0].update(is_mine=1), "expected a boolean, got int"),
        (lambda m, ls: ls["available"][0].update(status=3), "expected a string, got int"),
        (lambda m, ls: ls.update(extra=[]), "cache lists"),
        (lambda m, ls: ls["stats"].append(ls["stats"][0]), "two stat lines for"),
        (lambda m, ls: ls["stats"][0].update(values=[]), "stat values must be an object"),
        (lambda m, ls: m["scoreboard"]["matchups"][0].update(team_keys=["a"]), "two teams"),
        (lambda m, ls: m.update(has_last_season=False), "present but not flagged"),
        (lambda m, ls: m["teams"][0].update(roster=None), "TypeError"),
        (lambda m, ls: m.update(has_last_season=1), "expected a boolean, got int"),
        (lambda m, ls: m.update(surprise=1), r"cache meta keys \['surprise'\] unexpected"),
    ],
)
async def test_damaged_caches_are_refused(damage: Any, message: str) -> None:
    meta, lists = await encoded()
    damage(meta, lists)
    with pytest.raises(SnapshotCodecError, match=message):
        decode(meta, lists)


async def test_position_types_and_positions_keep_their_values() -> None:
    snap = await synthetic_snapshot()
    meta, _ = encode(snap)
    categories = {c["stat_id"]: c for c in meta["game_stat_categories"]}
    assert (categories["0"]["display_name"], categories["0"]["position_types"]) == ("GP", ["P"])
    assert decode(*encode(snap)).game_stat_categories == snap.game_stat_categories
