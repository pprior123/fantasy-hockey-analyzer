"""Salary-cap predicates (SPEC §5): None means "unavailable", never False."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fha.domain.cap import CapHit, cap_room, counts, fits, room_after, swap_ok

CAP = 119_600_000

# ---------------------------------------------------------------- cap_room


def test_cap_room_is_cap_minus_payroll_and_can_be_negative() -> None:
    assert cap_room(CAP, 115_000_000) == 4_600_000
    assert cap_room(CAP, 123_940_833) == -4_340_833  # over the cap


@pytest.mark.parametrize(("cap", "payroll"), [(None, 1), (CAP, None), (None, None)])
def test_cap_room_is_unavailable_without_cap_or_payroll(
    cap: int | None, payroll: int | None
) -> None:
    assert cap_room(cap, payroll) is None


# ---------------------------------------------------------------- counts


def test_counts_only_a_player_in_the_payroll_range() -> None:
    assert counts(CapHit(1_000_000, counted=True)) is True
    assert counts(CapHit(1_000_000, counted=False)) is False  # an IR row
    assert counts(None) is False  # missing from the tab


# ---------------------------------------------------------------- fits


def test_fits_is_aav_within_room() -> None:
    assert fits(1_000_000, 1_000_000) is True
    assert fits(1_000_001, 1_000_000) is False
    assert fits(0, 0) is True
    assert fits(0, -1) is False  # over the cap: not even a $0 pickup


@pytest.mark.parametrize(("aav", "room"), [(None, 5), (5, None), (None, None)])
def test_fits_is_undefined_without_an_aav_or_room(aav: int | None, room: int | None) -> None:
    assert fits(aav, room) is None


# ---------------------------------------------------------------- room_after / swap_ok


def test_a_counted_drop_frees_its_cap_hit() -> None:
    drop = CapHit(3_000_000, counted=True)
    assert room_after(500_000, drop, 3_400_000) == 100_000
    assert swap_ok(500_000, drop, 3_400_000) is True
    assert room_after(500_000, drop, 3_600_000) == -100_000
    assert swap_ok(500_000, drop, 3_600_000) is False


def test_an_exact_fit_is_ok() -> None:
    assert room_after(0, CapHit(2, counted=True), 2) == 0
    assert swap_ok(0, CapHit(2, counted=True), 2) is True


@pytest.mark.parametrize("drop", [CapHit(3_000_000, counted=False), None])
def test_an_ir_or_missing_drop_frees_nothing(drop: CapHit | None) -> None:
    assert room_after(500_000, drop, 400_000) == 100_000
    assert room_after(500_000, drop, 600_000) == -100_000
    assert swap_ok(500_000, drop, 600_000) is False


def test_an_ir_drop_with_an_unknown_salary_still_frees_nothing() -> None:
    assert room_after(500_000, CapHit(None, counted=False), 400_000) == 100_000


@pytest.mark.parametrize(
    ("room", "drop", "add"),
    [
        (None, CapHit(1, counted=True), 1),  # the team's cap room is unavailable
        (5, CapHit(1, counted=True), None),  # the incoming player has no AAV
        (5, CapHit(None, counted=True), 1),  # a counted drop whose salary is unknown ("???")
    ],
)
def test_swap_is_undefined_when_a_needed_amount_is_unknown(
    room: int | None, drop: CapHit | None, add: int | None
) -> None:
    assert room_after(room, drop, add) is None
    assert swap_ok(room, drop, add) is None


@pytest.mark.parametrize("value", [-1, True])
def test_amounts_must_be_whole_non_negative_dollars(value: int) -> None:
    with pytest.raises(ValueError, match="aav"):
        fits(value, 5)
    with pytest.raises(ValueError, match="aav"):
        room_after(5, None, value)
    with pytest.raises(ValueError, match="aav"):
        CapHit(value, counted=True)
    with pytest.raises(ValueError, match="cap"):
        cap_room(value, 5)
    with pytest.raises(ValueError, match="payroll"):
        cap_room(5, value)


def test_room_rejects_a_bool() -> None:
    with pytest.raises(ValueError, match="room must be whole dollars"):
        fits(1, True)
    with pytest.raises(ValueError, match="room must be whole dollars"):
        room_after(True, None, 1)


# ---------------------------------------------------------------- properties

dollars = st.integers(min_value=0, max_value=200_000_000)
rooms = st.integers(min_value=-50_000_000, max_value=200_000_000)
drops = st.one_of(st.none(), st.builds(CapHit, st.one_of(st.none(), dollars), st.booleans()))


@given(rooms, drops, dollars)
def test_swap_ok_is_room_after_non_negative(room: int, drop: CapHit | None, add: int) -> None:
    after = room_after(room, drop, add)
    assert swap_ok(room, drop, add) == (None if after is None else after >= 0)


@given(rooms, dollars)
def test_a_standalone_add_is_a_swap_with_no_drop(room: int, add: int) -> None:
    assert fits(add, room) == swap_ok(room, None, add)


@given(st.integers(min_value=0, max_value=200_000_000))
def test_a_zero_cap_hit_always_fits_with_room(room: int) -> None:
    assert fits(0, room) is True


@given(rooms, dollars, dollars)
def test_dropping_a_counted_player_never_hurts(room: int, drop_aav: int, add: int) -> None:
    with_drop = room_after(room, CapHit(drop_aav, counted=True), add)
    without = room_after(room, None, add)
    assert with_drop is not None
    assert without is not None
    assert with_drop >= without
