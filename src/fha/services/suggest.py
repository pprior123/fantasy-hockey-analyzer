"""The weekly add suggester (plans/weekly-suggester.md): which free agent to add, for whom.

Three parts:

- ``horizon``: the days to plan for (the rest of this matchup week from tomorrow, or next
  week) and the adds left, from the snapshot and today's Eastern date. Pure.
- ``load_week``: those days' NHL slates, through the schedule cache. The only I/O.
- ``rank`` / ``replace_gains``: the gains, through the lineup optimizer
  (``fha.domain.lineup``). Pure.

The value of a swap is ``Σ over the days [best lineup with the swap - best lineup now]``:
TTLTST x the starts the add would get, minus whom he benches and what the drop loses.
No projections: a player plays when his NHL team has a game and he has no status (or
``DTD``); every other status means he doesn't (SPEC §1, "Added after Phase 1").
Display text is the screens' job; this returns dates, slot types and integer units.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from fha.domain.cap import cap_room, fits, swap_ok
from fha.domain.lineup import (
    LineupPlayer,
    SwapGain,
    best_lineup,
    drop_week,
    units,
)
from fha.services.clock import Clock
from fha.services.league_view import LeagueView, PlayerRow
from fha.services.schedule import Tonight, eastern_date, slates, team_code
from fha.services.teams import drop_cap_hit
from fha.sources.nhl.schedule import ScheduleSource
from fha.sources.yahoo.models import IR_SLOTS, LeagueSnapshot
from fha.storage.repository import Repository

SKATER_SLOTS = ("C", "LW", "RW", "D")  # starting slot types a skater can fill
PLAYS = frozenset({None, "", "DTD"})  # statuses that still play; any other means he sits
DROP_SHARE = 0.5  # drops come from the rated skaters at or below this share (the median)
MIN_GAIN = units(0.005)  # what rounds to 0.00 isn't shown as a gain
TOP = 10


class Week(StrEnum):
    THIS = "this"
    NEXT = "next"


@dataclass(frozen=True)
class Horizon:
    week: Week
    days: tuple[date, ...]  # empty: no days left in that week
    known: bool  # False: Yahoo gave no start or end date for the week
    has_next: bool  # a next week exists (the toggle)
    max_adds: int | None  # the league's weekly add limit; None: unknown
    adds_left: int | None  # None: unknown


@dataclass(frozen=True)
class Suggestion:
    add: PlayerRow
    drop: PlayerRow | None  # None: an open spot, nobody dropped
    gain: int  # units (TTLTST-starts x SCALE)
    starts: int  # the add's starts
    games: int  # the add's games in the horizon
    filled: tuple[tuple[date, str], ...]  # the days and slot types he starts in
    cap_ok: bool | None  # None: the cap fit is unknown


@dataclass(frozen=True)
class Gap:
    day: date
    empty: tuple[tuple[str, int], ...]  # (slot type, empty slots) in the best lineup now


@dataclass(frozen=True)
class Ranking:
    gaps: tuple[Gap, ...]
    rows: tuple[Suggestion, ...]
    open_spot: bool


def horizon(snapshot: LeagueSnapshot, today: date, week: Week) -> Horizon:
    """The days to plan for. Today is left out: its lineup is set, and whether an add made
    today can play today is a Yahoo setting the app doesn't read."""
    tomorrow = today + timedelta(days=1)
    settings = snapshot.settings
    limit = settings.max_weekly_adds
    has_next = snapshot.next_scoreboard is not None
    board = snapshot.scoreboard if week is Week.THIS else snapshot.next_scoreboard
    first = _iso(board.week_start) if board is not None else None
    end = _iso(board.week_end) if board is not None else None
    if first is None or end is None:
        return Horizon(week, (), False, has_next, limit, None)
    start = max(first, tomorrow)
    days = tuple(start + timedelta(days=i) for i in range((end - start).days + 1))
    if week is Week.NEXT:
        # The limit resets, unless Yahoo still reports the week that has ended.
        stale = first <= today
        return Horizon(week, days, True, has_next, limit, None if stale else limit)
    mine = snapshot.my_team
    used = mine.roster_adds if mine is not None else None
    if limit is None or used is None or used[0] != settings.current_week:
        return Horizon(week, days, True, has_next, limit, None)
    return Horizon(week, days, True, has_next, limit, max(limit - used[1], 0))


def _iso(text: str | None) -> date | None:
    """A Yahoo week date, or None when missing or not an ISO date (the week is then unknown)."""
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


async def load_week(
    repo: Repository, source: ScheduleSource | None, clock: Clock, days: Sequence[date]
) -> tuple[Tonight, ...]:
    """The horizon's slates, concurrently, through the per-date schedule cache."""
    return await slates(repo, source, clock, days)


def starting_slots(snapshot: LeagueSnapshot) -> dict[str, int]:
    """The league's starting skater slots, in the settings' order."""
    slots: dict[str, int] = {}
    for slot in snapshot.settings.roster_slots:
        if slot.is_starting and slot.position in SKATER_SLOTS:
            slots[slot.position] = slots.get(slot.position, 0) + slot.count
    return slots


def has_open_spot(view: LeagueView) -> bool:
    """Fewer players outside IR slots than the non-IR slots (BN included): an add needs no drop."""
    mine = view.my_team
    if mine is None:
        return False
    active = sum(1 for r in view.roster(mine.team_key) if not r.in_ir_slot)
    room = sum(s.count for s in view.snapshot.settings.roster_slots if s.position not in IR_SLOTS)
    return active < room


def _value(row: PlayerRow) -> float:
    """His TTLTST per start; 0 when unrated (or, defensively, not a finite value >= 0)."""
    ttltst = row.ttltst if row.rating is not None and row.rating.rated else None
    return ttltst if ttltst is not None and math.isfinite(ttltst) and ttltst >= 0 else 0.0


def _lineup_player(row: PlayerRow, slots: Collection[str]) -> LineupPlayer | None:
    """The skater as the optimizer sees him, or None if he can't start (status, position)."""
    if row.is_goalie or row.status not in PLAYS:
        return None
    positions = frozenset(row.eligible_positions) & frozenset(slots)
    return LineupPlayer(row.player_id, positions, _value(row)) if positions else None


def _game_days(row: PlayerRow, playing: Sequence[Collection[str]]) -> frozenset[int]:
    code = team_code(row.nhl_team)
    return frozenset(i for i, teams in enumerate(playing) if code in teams)


def _days(
    roster: Sequence[PlayerRow], slots: Collection[str], playing: Sequence[Collection[str]]
) -> list[list[LineupPlayer]]:
    """Each day's players who can start: outside IR slots, playing, with a game."""
    starters = [
        (row, player)
        for row in roster
        if not row.in_ir_slot and (player := _lineup_player(row, slots)) is not None
    ]
    days: list[list[LineupPlayer]] = [[] for _ in playing]
    for row, player in starters:
        for i in _game_days(row, playing):
            days[i].append(player)
    return days


def _share_value(values: Sequence[float], share: float) -> float:
    """The value at ``share`` of the sorted values (interpolated: the median at 0.5)."""
    ordered = sorted(values)
    at = (len(ordered) - 1) * share
    low = math.floor(at)
    high = math.ceil(at)
    return ordered[low] + (ordered[high] - ordered[low]) * (at - low)


def drop_options(roster: Sequence[PlayerRow]) -> list[PlayerRow]:
    """Skaters outside IR slots, rated, at or below the median TTLTST of those rated skaters.
    Unrated skaters are never automatic drops (unrated usually means "hasn't played yet")."""
    rated = [
        r
        for r in roster
        if not r.is_goalie and not r.in_ir_slot and r.rating is not None and r.rating.rated
    ]
    if not rated:
        return []
    line = _share_value([_value(r) for r in rated], DROP_SHARE)
    return [r for r in rated if _value(r) <= line]


def rank(
    view: LeagueView,
    dates: Sequence[date],
    playing: Sequence[Collection[str]],
    *,
    top: int = TOP,
) -> Ranking:
    """The best add for each free agent (with his best drop), the top ``top`` by gain, and
    the gaps. ``playing``: for each of ``dates``, the NHL team codes with a game."""
    if len(dates) != len(playing):
        raise ValueError("one set of playing teams per date")
    mine = view.my_team
    if mine is None:
        return Ranking((), (), False)
    slots = starting_slots(view.snapshot)
    roster = view.roster(mine.team_key)
    days = _days(roster, slots, playing)
    open_spot = has_open_spot(view)
    room = cap_room(view.cap, mine.payroll)
    drops: list[PlayerRow | None] = [*([None] if open_spot else []), *drop_options(roster)]
    weeks = [drop_week(days, slots, d.player_id if d else None) for d in drops]
    hits = [drop_cap_hit(mine, d) if d else None for d in drops]
    rows: list[Suggestion] = []
    for fa in view.free_agents():
        player = _lineup_player(fa, slots)
        add_days = _game_days(fa, playing) if player is not None else frozenset()
        if player is None or not add_days:
            continue
        best: tuple[tuple[int, bool, float, str], PlayerRow | None, SwapGain, bool | None] | None
        best = None
        for drop, week, hit in zip(drops, weeks, hits, strict=True):
            ok = fits(fa.aav, room) if drop is None else swap_ok(room, hit, fa.aav)
            if ok is False:
                continue
            result = week.gain(player, add_days)
            # The highest gain; on a tie the open spot, then the weakest drop, then the id.
            key = (
                -result.gain,
                drop is not None,
                _value(drop) if drop else 0.0,
                drop.player_id if drop else "",
            )
            if best is None or key < best[0]:
                best = (key, drop, result, ok)
        if best is None or best[2].gain < MIN_GAIN:
            continue
        _, drop, result, ok = best
        filled = tuple((dates[i], slot) for i, slot in result.filled)
        rows.append(Suggestion(fa, drop, result.gain, result.starts, len(add_days), filled, ok))
    rows.sort(key=lambda s: (-s.gain, -_value(s.add), s.add.name, s.add.player_id))
    return Ranking(_gaps(dates, days, slots), tuple(rows[:top]), open_spot)


def _gaps(
    dates: Sequence[date], days: Sequence[Sequence[LineupPlayer]], slots: Mapping[str, int]
) -> tuple[Gap, ...]:
    gaps = []
    for day, players in zip(dates, days, strict=True):
        taken = list(best_lineup(players, slots).assignment.values())
        empty = tuple(
            (slot, count - taken.count(slot))
            for slot, count in slots.items()
            if count > taken.count(slot)
        )
        gaps.append(Gap(day, empty))
    return tuple(gaps)


def replace_gains(
    view: LeagueView, drop: PlayerRow, rows: Sequence[PlayerRow], playing: Sequence[Collection[str]]
) -> dict[str, SwapGain]:
    """Replace's Week + and Starts: each free agent in ``rows`` swapped for ``drop``."""
    mine = view.my_team
    if mine is None:
        return {}
    slots = starting_slots(view.snapshot)
    week = drop_week(_days(view.roster(mine.team_key), slots, playing), slots, drop.player_id)
    gains = {}
    for fa in rows:
        player = _lineup_player(fa, slots)
        if player is None:  # he can't start: the swap only loses what the drop would have
            player = LineupPlayer(fa.player_id, frozenset(), 0.0)
            add_days: frozenset[int] = frozenset()
        else:
            add_days = _game_days(fa, playing)
        gains[fa.player_id] = week.gain(player, add_days)
    return gains


def games(row: PlayerRow, playing: Sequence[Collection[str]]) -> int:
    """His NHL team's games over the horizon."""
    return len(_game_days(row, playing))


@dataclass(frozen=True)
class WeekPlan:
    horizon: Horizon
    slates: tuple[Tonight, ...]  # one per horizon day
    missing: tuple[date, ...]  # days whose schedule is unavailable
    ranking: Ranking | None  # None: nothing to rank (no days, an unknown week, a missing day)

    @property
    def stale(self) -> tuple[Tonight, ...]:
        return tuple(s for s in self.slates if s.state == "stale")


async def plan_week(
    view: LeagueView,
    repo: Repository,
    source: ScheduleSource | None,
    clock: Clock,
    week: Week,
) -> WeekPlan:
    """The horizon, its slates and the ranking. Any unavailable day blocks the ranking:
    never a value from part of the week. With no adds left only the gaps are shown."""
    plan = horizon(view.snapshot, eastern_date(clock), week)
    if not plan.known or not plan.days:
        return WeekPlan(plan, (), (), None)
    loaded = await load_week(repo, source, clock, plan.days)
    missing = tuple(s.date for s in loaded if s.state == "unavailable")
    if missing:
        return WeekPlan(plan, loaded, missing, None)
    top = 0 if plan.adds_left == 0 else TOP
    ranking = rank(view, plan.days, [frozenset(s.games) for s in loaded], top=top)
    return WeekPlan(plan, loaded, (), ranking)
