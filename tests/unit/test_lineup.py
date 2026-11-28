"""The daily lineup optimizer and the week gain of a swap (plans/weekly-suggester.md §0, §2).

The direct re-solve is the oracle: ``best_lineup`` against brute force over every
assignment, then ``day_thresholds`` / ``week_gain`` against ``best_lineup`` re-run on the
roster with the swap made."""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fha.domain.lineup import (
    SCALE,
    LineupPlayer,
    SwapGain,
    best_lineup,
    day_thresholds,
    drop_week,
    units,
    week_gain,
)

SLOTS = {"C": 3, "LW": 3, "RW": 3, "D": 6}
DETERMINISTIC = settings(derandomize=True, deadline=None, max_examples=400)


def p(player_id: str, positions: str, value: float) -> LineupPlayer:
    return LineupPlayer(player_id, frozenset(positions.split("/")), value)


def brute_force(players: Sequence[LineupPlayer], slots: Mapping[str, int]) -> tuple[int, int]:
    """The best (value, starts) over every assignment of players to slots or the bench."""
    best = (0, 0)
    options = [[None, *(s for s in slots if s in player.positions)] for player in players]
    for choice in itertools.product(*options):
        used = {s: 0 for s in slots}
        for slot in choice:
            if slot is not None:
                used[slot] += 1
        if any(used[s] > slots[s] for s in slots):
            continue
        value = sum(units(pl.value) for pl, slot in zip(players, choice, strict=True) if slot)
        starts = sum(slot is not None for slot in choice)
        best = max(best, (value, starts))
    return best


def check_valid(players: Sequence[LineupPlayer], slots: Mapping[str, int], lineup: object) -> None:
    by_id = {pl.player_id: pl for pl in players}
    assignment = lineup.assignment  # type: ignore[attr-defined]
    for player_id, slot in assignment.items():
        assert slot in by_id[player_id].positions
        assert slot in slots
    for slot, count in slots.items():
        assert sum(1 for s in assignment.values() if s == slot) <= count
    assert lineup.starts == len(assignment)  # type: ignore[attr-defined]
    assert lineup.value == sum(units(by_id[i].value) for i in assignment)  # type: ignore[attr-defined]


# best_lineup


def test_an_empty_roster_starts_nobody() -> None:
    lineup = best_lineup([], SLOTS)
    assert (lineup.value, lineup.starts, dict(lineup.assignment)) == (0, 0, {})


def test_more_players_than_slots_at_one_position_starts_the_best() -> None:
    players = [p("1", "C", 0.5), p("2", "C", 0.9), p("3", "C", 0.7), p("4", "C", 0.6)]
    lineup = best_lineup(players, {"C": 2})
    assert dict(lineup.assignment) == {"2": "C", "3": "C"}
    assert lineup.value == units(0.9) + units(0.7)
    assert lineup.starts == 2


def test_a_multi_position_player_moves_to_the_other_slot() -> None:
    """The greedy trap: the best player is C/LW and takes C first; a pure C then needs
    that slot, so the solver moves the C/LW to LW rather than benching the C."""
    players = [p("a", "C/LW", 1.0), p("b", "C", 0.8)]
    lineup = best_lineup(players, {"C": 1, "LW": 1})
    assert dict(lineup.assignment) == {"a": "LW", "b": "C"}
    assert lineup.starts == 2


def test_a_chain_of_moves_seats_the_last_player() -> None:
    players = [p("a", "C/LW", 0.9), p("b", "LW/RW", 0.8), p("c", "C", 0.7)]
    lineup = best_lineup(players, {"C": 1, "LW": 1, "RW": 1})
    assert dict(lineup.assignment) == {"a": "LW", "b": "RW", "c": "C"}


def test_an_unrated_player_is_benched_for_a_rated_one_but_fills_an_empty_slot() -> None:
    players = [p("rated", "C", 0.4), p("unrated", "C", 0.0), p("winger", "LW/C", 0.0)]
    lineup = best_lineup(players, {"C": 1, "LW": 1})
    assert lineup.assignment["rated"] == "C"
    assert "unrated" not in lineup.assignment
    assert lineup.assignment["winger"] == "LW"
    assert (lineup.value, lineup.starts) == (units(0.4), 2)


def test_value_ties_go_to_more_starts_then_the_lower_player_id() -> None:
    # The same value with big at C alone, or at LW beside an unrated C: two starts win.
    pair = [p("big", "C/LW", 1.0), p("zero", "C", 0.0)]
    lineup = best_lineup(pair, {"C": 1, "LW": 1})
    assert (lineup.value, lineup.starts) == (units(1.0), 2)
    assert dict(lineup.assignment) == {"big": "LW", "zero": "C"}
    # Equal value and starts: the lower id starts, whatever the input order.
    for players in ([p("b", "C", 0.3), p("a", "C", 0.3)], [p("a", "C", 0.3), p("b", "C", 0.3)]):
        assert dict(best_lineup(players, {"C": 1}).assignment) == {"a": "C"}


def test_slot_counts_come_from_the_mapping() -> None:
    players = [p(str(i), "D", 0.1 * (i + 1)) for i in range(5)]
    assert best_lineup(players, {"D": 2}).starts == 2
    assert best_lineup(players, {"D": 4}).starts == 4
    assert best_lineup(players, {"D": 0}).starts == 0
    assert best_lineup(players, {"Util": 3}).starts == 0  # no player is a Util
    assert best_lineup(players, {}).starts == 0


def test_bad_inputs_are_refused() -> None:
    with pytest.raises(ValueError, match="value"):
        best_lineup([p("a", "C", -0.1)], SLOTS)
    with pytest.raises(ValueError, match="value"):
        best_lineup([p("a", "C", math.nan)], SLOTS)
    with pytest.raises(ValueError, match="value"):
        best_lineup([p("a", "C", math.inf)], SLOTS)
    with pytest.raises(ValueError, match=r"^a player is listed twice$"):
        best_lineup([p("a", "C", 0.1), p("a", "LW", 0.2)], SLOTS)
    with pytest.raises(ValueError, match="slot count"):
        best_lineup([], {"C": -1})
    with pytest.raises(ValueError, match="slot count"):
        best_lineup([], {"C": True})


def test_units_are_millionths_rounded() -> None:
    assert SCALE == 1_000_000
    assert units(0.1234564) == 123456
    assert units(0.1234566) == 123457
    assert units(0) == 0
    assert isinstance(units(1.5), int)


SLOT_TYPES = ("C", "LW", "RW", "D")
VALUES = st.sampled_from([0.0, 0.0, 0.25, 0.5, 0.5, 0.75, 1.0])  # zeros and repeats: ties


@st.composite
def instances(draw: st.DrawFn, max_players: int = 6) -> tuple[list[LineupPlayer], dict[str, int]]:
    kinds = draw(st.lists(st.sampled_from(SLOT_TYPES), min_size=1, max_size=4, unique=True))
    slots = {s: draw(st.integers(0, 2)) for s in kinds}
    count = draw(st.integers(0, max_players))
    players = [
        LineupPlayer(
            f"p{i:02d}",
            frozenset(draw(st.lists(st.sampled_from(SLOT_TYPES), min_size=1, max_size=3))),
            draw(VALUES),
        )
        for i in range(count)
    ]
    return players, slots


@DETERMINISTIC
@given(instances(), st.randoms(use_true_random=False))
def test_best_lineup_matches_brute_force_and_ignores_input_order(
    instance: tuple[list[LineupPlayer], dict[str, int]], rnd: object
) -> None:
    players, slots = instance
    lineup = best_lineup(players, slots)
    check_valid(players, slots, lineup)
    assert (lineup.value, lineup.starts) == brute_force(players, slots)
    shuffled = list(players)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    assert best_lineup(shuffled, slots) == lineup


# day_thresholds


def test_day_thresholds_hold_the_best_with_one_fewer_slot_of_each_type() -> None:
    players = [p("a", "C", 0.9), p("b", "C/LW", 0.6), p("c", "D", 0.2)]
    slots = {"C": 1, "LW": 1, "D": 1, "RW": 0}
    t = day_thresholds(players, slots)
    assert t.base == (units(0.9) + units(0.6) + units(0.2), 3)
    assert t.without["C"] == (units(0.6) + units(0.2), 2)  # no C slot: a (C only) sits
    assert t.without["LW"] == (units(0.9) + units(0.2), 2)
    assert t.without["D"] == (units(0.9) + units(0.6), 2)
    assert t.without["RW"] is None  # no RW slot to give up
    assert list(t.without) == ["C", "LW", "D", "RW"]  # the mapping's order


# week_gain


def oracle(
    days: Sequence[Sequence[LineupPlayer]],
    slots: Mapping[str, int],
    add: LineupPlayer,
    add_days: frozenset[int],
    drop: str | None,
) -> tuple[int, int, list[tuple[int, int]]]:
    """The direct re-solve: the swap's gain, the add's starts, and each start's lineup."""
    gain = 0
    starts = 0
    with_add = []
    for i, day in enumerate(days):
        rest = [pl for pl in day if pl.player_id != drop]
        after = best_lineup([*rest, add] if i in add_days else rest, slots)
        gain += after.value - best_lineup(day, slots).value
        if add.player_id in after.assignment:
            starts += 1
            with_add.append((after.value, after.starts))
    return gain, starts, with_add


def check_against_oracle(
    days: Sequence[Sequence[LineupPlayer]],
    slots: Mapping[str, int],
    add: LineupPlayer,
    add_days: frozenset[int],
    drop: str | None,
) -> SwapGain:
    result = week_gain(days, slots, add, add_days, drop)
    gain, starts, with_add = oracle(days, slots, add, add_days, drop)
    assert result.gain == gain
    assert result.starts == starts
    assert len(result.filled) == starts
    assert [d for d, _ in result.filled] == sorted(d for d, _ in result.filled)
    for (day, slot), best in zip(result.filled, with_add, strict=True):
        # The add can take ``slot`` in a best lineup of that day.
        assert slot in add.positions
        assert day in add_days
        rest = [pl for pl in days[day] if pl.player_id != drop]
        reduced = {**slots, slot: slots[slot] - 1}
        fewer = best_lineup(rest, reduced)
        assert (fewer.value + units(add.value), fewer.starts + 1) == best
    return result


def test_the_owners_example_fills_empty_slots_over_benching_a_starter() -> None:
    """A slightly lower TTLTST who fills the empty D slot beats a higher C who
    would push a starter to the bench."""
    slots = {"C": 1, "D": 1}
    day = [p("c1", "C", 0.6), p("c2", "C", 0.5)]
    days = [day, day, day]
    every = frozenset({0, 1, 2})
    filler = check_against_oracle(days, slots, p("d", "D", 0.45), every, None)
    center = check_against_oracle(days, slots, p("c", "C", 0.5), every, None)
    assert filler.gain == 3 * units(0.45)
    assert filler.starts == 3
    assert filler.filled == ((0, "D"), (1, "D"), (2, "D"))
    assert center.gain == 0  # benches c2 (0.5) for 0.5: nothing gained
    assert filler.gain > center.gain


def test_a_drop_who_loses_starts_costs_them_on_every_day() -> None:
    slots = {"C": 1, "D": 1}
    day = [p("c1", "C", 0.6), p("d1", "D", 0.3)]
    days = [day, day, [p("c1", "C", 0.6)]]  # d1 has no game on day 2
    result = check_against_oracle(days, slots, p("d2", "D", 0.4), frozenset({0}), "d1")
    assert result.gain == units(0.4) - units(0.3) - units(0.3)  # +0.1 on day 0, -0.3 on day 1
    assert result.starts == 1


def test_dropping_nobody_counts_only_the_add() -> None:
    slots = {"C": 2}
    days = [[p("c1", "C", 0.6)], []]
    result = check_against_oracle(days, slots, p("c2", "C", 0.2), frozenset({0, 1}), None)
    assert result.gain == 2 * units(0.2)
    assert result.filled == ((0, "C"), (1, "C"))


def test_an_add_with_no_games_gains_nothing_without_a_drop() -> None:
    slots = {"C": 1}
    days = [[p("c1", "C", 0.6)], []]
    result = week_gain(days, slots, p("c2", "C", 0.9), frozenset(), None)
    assert result == SwapGain(0, 0, ())


def test_a_zero_value_add_into_an_empty_slot_starts() -> None:
    """Ties go to more starts: an unrated add who fills an empty slot starts and gains 0."""
    slots = {"C": 1, "D": 1}
    days = [[p("c1", "C", 0.6)]]
    result = check_against_oracle(days, slots, p("d", "D", 0.0), frozenset({0}), None)
    assert (result.gain, result.starts, result.filled) == (0, 1, ((0, "D"),))


@pytest.mark.parametrize(
    ("add_id", "starts"), [("a", 1), ("z", 0)], ids=["add-sorts-first", "add-sorts-last"]
)
def test_an_equal_valued_add_starts_only_if_his_id_sorts_first(add_id: str, starts: int) -> None:
    slots = {"C": 1}
    days = [[p("m", "C", 0.5)]]
    result = check_against_oracle(days, slots, p(add_id, "C", 0.5), frozenset({0}), None)
    assert (result.gain, result.starts) == (0, starts)


def test_the_slot_tie_order_follows_the_mapping() -> None:
    days: list[list[LineupPlayer]] = [[]]
    add = p("x", "C/LW", 0.5)
    assert week_gain(days, {"LW": 1, "C": 1}, add, frozenset({0}), None).filled == ((0, "LW"),)
    assert week_gain(days, {"C": 1, "LW": 1}, add, frozenset({0}), None).filled == ((0, "C"),)


def test_a_drop_with_no_game_that_day_changes_nothing_that_day() -> None:
    slots = {"C": 1}
    days = [[p("c1", "C", 0.6)]]
    result = check_against_oracle(days, slots, p("c2", "C", 0.1), frozenset({0}), "absent")
    assert result.gain == 0


def test_an_add_already_on_a_day_is_refused() -> None:
    days = [[p("c1", "C", 0.6)]]
    with pytest.raises(ValueError, match="already"):
        week_gain(days, {"C": 1}, p("c1", "C", 0.6), frozenset({0}), None)


def test_add_days_outside_the_horizon_are_refused() -> None:
    with pytest.raises(ValueError, match="day"):
        week_gain([[]], {"C": 1}, p("c", "C", 0.6), frozenset({1}), None)
    with pytest.raises(ValueError, match="day"):
        week_gain([[]], {"C": 1}, p("c", "C", 0.6), frozenset({-1}), None)


def test_one_drop_week_prices_many_adds() -> None:
    slots = {"C": 1, "D": 1}
    days = [[p("c1", "C", 0.6), p("d1", "D", 0.2)], [p("c1", "C", 0.6)]]
    week = drop_week(days, slots, "d1")
    for add, add_days in [
        (p("d2", "D", 0.5), frozenset({0, 1})),
        (p("c2", "C", 0.9), frozenset({1})),
        (p("w", "LW", 0.9), frozenset({0})),
    ]:
        assert week.gain(add, add_days) == week_gain(days, slots, add, add_days, "d1")


@st.composite
def weeks(
    draw: st.DrawFn,
) -> tuple[list[list[LineupPlayer]], dict[str, int], LineupPlayer, frozenset[int], str | None]:
    roster, slots = draw(instances(max_players=5))
    n_days = draw(st.integers(1, 3))
    days = [
        [pl for pl in roster if draw(st.booleans())] for _ in range(n_days)
    ]  # who has a game each day
    add = LineupPlayer(
        draw(st.sampled_from(["a", "p02x", "zz"])),  # sorts before, among, after the roster
        frozenset(draw(st.lists(st.sampled_from(SLOT_TYPES), min_size=1, max_size=3))),
        draw(VALUES),
    )
    add_days = frozenset(draw(st.lists(st.integers(0, n_days - 1), max_size=n_days)))
    ids = [pl.player_id for pl in roster]
    drop = draw(st.sampled_from([None, "absent", *ids]))
    return days, slots, add, add_days, drop


@DETERMINISTIC
@given(weeks())
def test_week_gain_matches_the_direct_re_solve(
    week: tuple[list[list[LineupPlayer]], dict[str, int], LineupPlayer, frozenset[int], str | None],
) -> None:
    check_against_oracle(*week)
