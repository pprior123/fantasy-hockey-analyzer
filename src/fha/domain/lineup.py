"""The best daily lineup, and what an add/drop swap gains over a week of days.

Pure (plans/weekly-suggester.md §0, §2). A lineup puts skaters into starting slot types
(``slots``: type -> count, from the league's settings). Each player may fill one slot of
a type in his ``positions``; the value of a lineup is the sum of its starters' values.
The best lineup has the highest value, then the most starts, then prefers lower player
ids. Values arrive as floats and are held as integer ``units`` (millionths), so sums,
differences and ties are exact.

The solver: a player's value doesn't depend on the slot he fills, so the sets of players
who can all start together form a matroid (a transversal matroid with slot capacities).
Taking players best first (value descending, then id) and keeping each one for whom an
augmenting path frees a slot is therefore exact. It is not the greedy slot-filling of
plans §1 C: seating a player may move earlier starters to other slot types along the
path. With values >= 0 the result is a largest independent set too (most starts).

The week gain of a swap (``week_gain``) uses per-slot thresholds: with the add in slot
type s, the day's best is ``best(rest, slots - one s) + add``, so one solve per slot type
prices every add (``drop_week``). An exact tie in (value, starts) between the add
starting and not starting is settled by solving that day directly, so the result always
equals the direct re-solve.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

SCALE = 1_000_000  # units per 1.0 of value


def units(value: float) -> int:
    """A value in integer millionths, rounded."""
    return round(value * SCALE)


@dataclass(frozen=True)
class LineupPlayer:
    player_id: str
    positions: frozenset[str]  # the slot types he may fill
    value: float  # >= 0 and finite (his TTLTST per start; 0 when unrated)


@dataclass(frozen=True)
class Lineup:
    value: int  # units
    starts: int
    assignment: Mapping[str, str]  # player id -> slot type, starters only


@dataclass(frozen=True)
class Thresholds:
    """A day's best (value, starts), and per slot type the best with one fewer slot of it
    (None when the type has no slot to give up), in the ``slots`` mapping's order."""

    base: tuple[int, int]
    without: Mapping[str, tuple[int, int] | None]


@dataclass(frozen=True)
class SwapGain:
    gain: int  # units, summed over the days
    starts: int  # the add's starts
    filled: tuple[tuple[int, str], ...]  # (day index, slot type) for each of his starts


def _checked(slots: Mapping[str, int]) -> dict[str, int]:
    for slot, count in slots.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"slot count for {slot!r} must be a whole number >= 0, got {count!r}")
    return dict(slots)


def _value(player: LineupPlayer) -> int:
    if not math.isfinite(player.value) or player.value < 0:
        raise ValueError(f"{player.player_id}: value must be finite and >= 0, got {player.value}")
    return units(player.value)


def best_lineup(players: Iterable[LineupPlayer], slots: Mapping[str, int]) -> Lineup:
    """The best lineup: highest value, then most starts, then lower player ids."""
    capacity = _checked(slots)
    valued = [(_value(pl), pl) for pl in players]
    if len({pl.player_id for _, pl in valued}) < len(valued):
        raise ValueError("a player is listed twice")
    valued.sort(key=lambda item: (-item[0], item[1].player_id))
    seated: dict[str, list[LineupPlayer]] = {slot: [] for slot in capacity}
    assignment: dict[str, str] = {}
    total = 0
    for value, player in valued:
        if _seat(player, capacity, seated, assignment):
            total += value
    return Lineup(total, len(assignment), assignment)


def _seat(
    player: LineupPlayer,
    capacity: Mapping[str, int],
    seated: dict[str, list[LineupPlayer]],
    assignment: dict[str, str],
) -> bool:
    """Seat ``player`` along the shortest augmenting path, moving starters as it says."""
    # came_from[t] = (slot type s, the player moved from s to t), or None for a start slot.
    came_from: dict[str, tuple[str, LineupPlayer] | None] = {}
    queue: deque[str] = deque()
    for slot in capacity:
        if slot in player.positions:
            came_from[slot] = None
            queue.append(slot)
    while queue:
        slot = queue.popleft()
        if len(seated[slot]) < capacity[slot]:
            start = _shift(slot, came_from, seated, assignment)
            seated[start].append(player)
            assignment[player.player_id] = start
            return True
        for mover in seated[slot]:
            for other in capacity:
                if other in mover.positions and other not in came_from:
                    came_from[other] = (slot, mover)
                    queue.append(other)
    return False


def _shift(
    free: str,
    came_from: Mapping[str, tuple[str, LineupPlayer] | None],
    seated: dict[str, list[LineupPlayer]],
    assignment: dict[str, str],
) -> str:
    """Move each player on the path one step toward the free slot; return the path's start."""
    slot = free
    step = came_from[slot]
    while step is not None:
        source, mover = step
        seated[source].remove(mover)
        seated[slot].append(mover)
        assignment[mover.player_id] = slot
        slot = source
        step = came_from[slot]
    return slot


def day_thresholds(players: Iterable[LineupPlayer], slots: Mapping[str, int]) -> Thresholds:
    """The day's best, and the best with one fewer slot of each type (one solve per type)."""
    capacity = _checked(slots)
    roster = list(players)
    base = best_lineup(roster, capacity)
    without: dict[str, tuple[int, int] | None] = {}
    for slot, count in capacity.items():
        if count == 0:
            without[slot] = None
        else:
            fewer = best_lineup(roster, {**capacity, slot: count - 1})
            without[slot] = (fewer.value, fewer.starts)
    return Thresholds((base.value, base.starts), without)


@dataclass(frozen=True)
class _Day:
    full: int  # the day's best value with the whole roster
    rest: tuple[LineupPlayer, ...]  # the roster less the drop
    thresholds: Thresholds  # of ``rest``


class DropWeek:
    """One drop option over the days, priced once; each add is then arithmetic."""

    def __init__(
        self,
        days: Sequence[Iterable[LineupPlayer]],
        slots: Mapping[str, int],
        drop: str | None,
    ) -> None:
        self._slots = _checked(slots)
        self._days = []
        for day in days:
            roster = list(day)
            rest = tuple(pl for pl in roster if pl.player_id != drop)
            full = best_lineup(roster, self._slots).value
            self._days.append(_Day(full, rest, day_thresholds(rest, self._slots)))

    def gain(self, add: LineupPlayer, add_days: frozenset[int]) -> SwapGain:
        """The swap's gain over the days, the add's starts and the slots he fills."""
        if any(not 0 <= i < len(self._days) for i in add_days):
            raise ValueError(f"add days must be day indexes below {len(self._days)}")
        value = _value(add)
        gain = 0
        filled: list[tuple[int, str]] = []
        for i, day in enumerate(self._days):
            after = day.thresholds.base[0]
            if i in add_days:
                if any(pl.player_id == add.player_id for pl in day.rest):
                    raise ValueError(f"{add.player_id} is already on day {i}")
                slot, after = self._with_add(day, add, value)
                if slot is not None:
                    filled.append((i, slot))
            gain += after - day.full
        return SwapGain(gain, len(filled), tuple(filled))

    def _with_add(self, day: _Day, add: LineupPlayer, value: int) -> tuple[str | None, int]:
        """The slot the add starts in (None: benched) and the day's best value with him."""
        base = day.thresholds.base
        options = [
            ((fewer[0] + value, fewer[1] + 1), slot)
            for slot, fewer in day.thresholds.without.items()
            if fewer is not None and slot in add.positions
        ]
        if not options:
            return None, base[0]
        best, best_slot = max(options, key=lambda option: option[0])  # the first in slot order
        if best < base:
            return None, base[0]
        if best > base:
            return best_slot, best[0]
        # A tie in value and starts: the id order decides, so solve the day directly.
        lineup = best_lineup([*day.rest, add], self._slots)
        return lineup.assignment.get(add.player_id), lineup.value


def drop_week(
    days: Sequence[Iterable[LineupPlayer]], slots: Mapping[str, int], drop: str | None
) -> DropWeek:
    """Price one drop option (None: an open spot, nobody dropped) over the days."""
    return DropWeek(days, slots, drop)


def week_gain(
    days: Sequence[Iterable[LineupPlayer]],
    slots: Mapping[str, int],
    add: LineupPlayer,
    add_days: frozenset[int],
    drop: str | None,
) -> SwapGain:
    """``Σ over days [best(day - drop + add) - best(day)]``, the add only on ``add_days``.
    ``days`` lists each day's eligible players (games and status already applied), without
    the add."""
    return drop_week(days, slots, drop).gain(add, add_days)
