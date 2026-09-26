"""Salary-cap predicates (SPEC §5). Pure; amounts are whole dollars.

::

    counts(p)          = p is in the tab's PAYROLL range (not an IR row);
                         False for a player missing from the tab
    cap_room(team)     = cap - payroll(team)
    fits(p, team)      = aav(p) <= cap_room(team)              # a pickup, no drop
    room_after(team, drop, add) = cap_room(team) + aav(drop)·counts(drop) - aav(add)
    swap_ok(team, drop, add)    = room_after(team, drop, add) >= 0   # a Yahoo add/drop

None means "unavailable": an unreadable or unbound tab has no payroll, a free
agent may have no AAV. Then the predicates are None too, never False and
never a guess, so the cap filters can exclude such rows and count them. The
incoming player's IR status never matters: his full cap hit must fit when he
is acquired.
"""

from __future__ import annotations

from dataclasses import dataclass


def _dollars(value: int | None, what: str, *, negative_ok: bool = False) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be whole dollars, got {value!r}")
    if value < 0 and not negative_ok:
        raise ValueError(f"{what} must be >= 0, got {value}")
    return value


@dataclass(frozen=True)
class CapHit:
    """A rostered player's salary as the league sheet has it."""

    aav: int | None  # None: the sheet's cell had no number
    counted: bool  # in the tab's PAYROLL range (False: an IR row, not counted)

    def __post_init__(self) -> None:
        _dollars(self.aav, "aav")


def counts(hit: CapHit | None) -> bool:
    """Whether dropping the player frees his cap hit. ``None``: missing from the tab."""
    return hit is not None and hit.counted


def cap_room(cap: int | None, payroll: int | None) -> int | None:
    """``cap - payroll`` (negative when over the cap), or None if either is unknown."""
    cap, payroll = _dollars(cap, "cap"), _dollars(payroll, "payroll")
    if cap is None or payroll is None:
        return None
    return cap - payroll


def fits(aav: int | None, room: int | None) -> bool | None:
    """A straight pickup (no drop): the cap hit fits in the room. None if unknown."""
    aav, room = _dollars(aav, "aav"), _dollars(room, "room", negative_ok=True)
    if aav is None or room is None:
        return None
    return aav <= room


def room_after(room: int | None, drop: CapHit | None, add: int | None) -> int | None:
    """Cap room after dropping ``drop`` and adding a player with cap hit ``add``.

    A counted drop frees its cap hit; an IR row or a player missing from the tab
    frees nothing. None when the room, the added cap hit, or a counted drop's cap
    hit is unknown.
    """
    room, add = _dollars(room, "room", negative_ok=True), _dollars(add, "aav")
    if room is None or add is None:
        return None
    if drop is None or not counts(drop):
        return room - add
    if drop.aav is None:
        return None
    return room + drop.aav - add


def swap_ok(room: int | None, drop: CapHit | None, add: int | None) -> bool | None:
    """A Yahoo add/drop (one transaction) is cap-legal. None if unknown."""
    after = room_after(room, drop, add)
    return None if after is None else after >= 0
