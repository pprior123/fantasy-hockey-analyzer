"""The weekly add suggester's service (plans/weekly-suggester.md §0, §3 phase 3)."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from fha.domain.engine import DEFAULT_CONFIG, Rating
from fha.domain.lineup import LineupPlayer, best_lineup, units
from fha.domain.schedule import Game
from fha.services.league_view import LeagueView, PlayerRow, Salaries, Season, TeamInfo, build_view
from fha.services.schedule import COLLECTION, team_code
from fha.services.suggest import (
    MIN_GAIN,
    Gap,
    Ranking,
    Week,
    drop_options,
    games,
    has_open_spot,
    horizon,
    plan_week,
    rank,
    replace_gains,
    starting_slots,
)
from fha.services.teams import replace_view
from fha.sources.yahoo.demo import demo_snapshot
from fha.sources.yahoo.models import RosterSlot, Scoreboard, Team
from fha.storage.memory import InMemoryRepository
from tests.unit.services.test_schedule import Source
from tests.unit.web.helpers import FakeClock

DEMO = demo_snapshot()
TEMPLATE = build_view(DEMO, DEFAULT_CONFIG, Salaries(), season=Season.LAST)
MY = "465.l.8076.t.1"
TODAY = date(2026, 11, 27)  # a Friday
NOW = datetime.fromisoformat("2026-11-27T15:00:00Z").timestamp()  # 10:00 ET
SAT, SUN = date(2026, 11, 28), date(2026, 11, 29)
WEEK = 9
SLOTS = (
    RosterSlot("C", 1, True),
    RosterSlot("LW", 1, True),
    RosterSlot("D", 2, True),
    RosterSlot("G", 1, True),
    RosterSlot("BN", 1, False),
    RosterSlot("IR", 1, False),
)


def row(
    pid: str,
    pos: str = "C",
    team: str = "BOS",
    ttltst: float | None = 0.5,
    *,
    status: str | None = None,
    owner: str | None = MY,
    slot: str | None = "BN",
    aav: int | None = None,
    counts: bool | None = None,
    goalie: bool = False,
) -> PlayerRow:
    rating = (
        None
        if goalie
        else Rating(pid, f"N{pid}", ttltst, 1 if ttltst is not None else None, None, None)
    )
    return PlayerRow(
        player_id=pid,
        player_key=f"465.p.{pid}",
        name=f"N{pid}",
        position=pos.replace("/", ","),
        eligible_positions=tuple(pos.split("/")),
        nhl_team=team,
        status=status,
        is_goalie=goalie,
        owner_key=owner,
        slot=slot if owner else None,
        gp=10,
        rating=rating,
        aav=aav,
        aav_source=None,
        counts=counts,
    )


def fa(
    pid: str, pos: str = "C", team: str = "BOS", ttltst: float | None = 0.5, **kw: object
) -> PlayerRow:
    return row(pid, pos, team, ttltst, owner=None, **kw)  # type: ignore[arg-type]


def league(
    mine: Sequence[PlayerRow],
    fas: Sequence[PlayerRow] = (),
    *,
    slots: tuple[RosterSlot, ...] = SLOTS,
    week: tuple[str | None, str | None] = ("2026-11-23", "2026-11-29"),
    next_week: tuple[str | None, str | None] | None = ("2026-11-30", "2026-12-06"),
    max_adds: int | None = 5,
    roster_adds: tuple[int, int] | None = (WEEK, 2),
    cap: int | None = None,
    payroll: int | None = None,
    unmatched_counted: bool = False,
) -> LeagueView:
    snapshot = replace(
        DEMO,
        settings=replace(
            DEMO.settings, roster_slots=slots, max_weekly_adds=max_adds, current_week=WEEK
        ),
        teams=(Team(MY, "Mine", True, (), roster_adds=roster_adds),),
        scoreboard=Scoreboard(WEEK, week[0], week[1], ()),
        next_scoreboard=None if next_week is None else Scoreboard(WEEK + 1, *next_week, ()),
    )
    teams = (
        TeamInfo(MY, "Mine", True, payroll, tuple(r.player_id for r in mine), unmatched_counted),
    )
    return replace(TEMPLATE, rows=(*mine, *fas), teams=teams, cap=cap, snapshot=snapshot)


# The horizon


def test_this_week_runs_from_tomorrow_to_the_week_end() -> None:
    h = horizon(league([]).snapshot, TODAY, Week.THIS)
    assert (h.days, h.known, h.has_next) == ((SAT, SUN), True, True)
    assert (h.max_adds, h.adds_left) == (5, 3)


def test_the_last_day_of_the_week_leaves_no_days() -> None:
    assert horizon(league([]).snapshot, SUN, Week.THIS).days == ()
    assert horizon(league([]).snapshot, date(2026, 11, 30), Week.THIS).days == ()


def test_next_week_is_its_whole_week_with_the_limit_reset() -> None:
    h = horizon(league([]).snapshot, TODAY, Week.NEXT)
    assert h.days == tuple(date(2026, 11, 30) + timedelta(days=i) for i in range(7))
    assert h.adds_left == 5


def test_next_week_starts_tomorrow_when_yahoo_still_reports_the_old_week() -> None:
    view = league([], week=("2026-11-16", "2026-11-22"), next_week=("2026-11-23", "2026-11-29"))
    h = horizon(view.snapshot, TODAY, Week.NEXT)
    assert h.days == (SAT, SUN)
    assert h.adds_left is None  # the limit may not have reset as Yahoo counts it


@pytest.mark.parametrize(
    "week",
    [(None, "2026-11-29"), ("2026-11-23", None), ("Nov 23", "2026-11-29"), ("2026-11-23", "")],
    ids=["no-start", "no-end", "not-iso", "blank-end"],
)
def test_a_week_without_dates_is_unknown(week: tuple[str | None, str | None]) -> None:
    h = horizon(league([], week=week).snapshot, TODAY, Week.THIS)
    assert (h.known, h.days) == (False, ())


def test_no_next_week_hides_the_toggle() -> None:
    h = horizon(league([], next_week=None).snapshot, TODAY, Week.NEXT)
    assert (h.has_next, h.known, h.days) == (False, False, ())


@pytest.mark.parametrize(
    ("max_adds", "roster_adds", "left"),
    [
        (5, (WEEK, 5), 0),
        (5, (WEEK, 7), 0),
        (5, (WEEK - 1, 2), None),
        (5, None, None),
        (None, (WEEK, 2), None),
    ],
    ids=["none-left", "over", "other-week", "no-team-adds", "no-limit"],
)
def test_adds_left_this_week(
    max_adds: int | None, roster_adds: tuple[int, int] | None, left: int | None
) -> None:
    view = league([], max_adds=max_adds, roster_adds=roster_adds)
    assert horizon(view.snapshot, TODAY, Week.THIS).adds_left == left
    if max_adds is not None:
        assert horizon(view.snapshot, TODAY, Week.NEXT).adds_left == max_adds


# Who starts, who can be dropped


def test_starting_slots_follow_the_settings_and_leave_goalies_and_bench_out() -> None:
    assert starting_slots(league([]).snapshot) == {"C": 1, "LW": 1, "D": 2}
    assert list(starting_slots(DEMO)) == ["C", "LW", "RW", "D"]


def test_an_open_spot_counts_players_outside_ir_against_non_ir_slots() -> None:
    six = [row(str(i)) for i in range(6)]  # C, LW, D x2, G, BN: 6 slots outside IR
    assert not has_open_spot(league(six))
    assert has_open_spot(league(six[:5]))
    assert has_open_spot(league([*six[:5], row("ir", slot="IR")]))


@pytest.mark.parametrize(
    ("values", "dropped"),
    [
        ([0.1, 0.2, 0.3, 0.4], {"0", "1"}),  # even count: the median is 0.25
        ([0.1, 0.2, 0.3], {"0", "1"}),
        ([0.4], {"0"}),
        ([0.2, 0.2, 0.2], {"0", "1", "2"}),
    ],
    ids=["even", "odd", "single", "equal"],
)
def test_drops_are_rated_skaters_at_or_below_the_median(
    values: list[float], dropped: set[str]
) -> None:
    roster = [row(str(i), ttltst=v) for i, v in enumerate(values)]
    roster += [row("u", ttltst=None), row("ir", ttltst=0.0, slot="IR"), row("g", goalie=True)]
    assert {r.player_id for r in drop_options(roster)} == dropped


def test_all_unrated_means_no_automatic_drops() -> None:
    assert drop_options([row("a", ttltst=None), row("b", ttltst=None)]) == []


# The ranking


def playing(*teams: Collection[str]) -> list[frozenset[str]]:
    return [frozenset(t) for t in teams]


def test_the_owners_example_ranks_the_filler_above_the_bench_maker() -> None:
    mine = [
        row("c1", "C", ttltst=0.6, slot="C"),
        row("c2", "C", ttltst=0.5),
        row("d1", "D", ttltst=0.1, slot="D"),
    ]
    fas = [fa("fd", "D", ttltst=0.45), fa("fc", "C", ttltst=0.7)]
    result = rank(league(mine, fas), (SAT, SUN), playing({"BOS"}, {"BOS"}))
    assert [s.add.player_id for s in result.rows] == ["fd", "fc"]
    filler = result.rows[0]
    assert filler.drop is None  # 4 players, 5 non-IR slots: an open spot
    assert filler.gain == 2 * units(0.45)
    assert (filler.starts, filler.games) == (2, 2)
    assert filler.filled == ((SAT, "D"), (SUN, "D"))
    # fc beats c1 (0.7 over 0.6) but benches him: worth less than filling the empty D.
    assert result.rows[1].gain == 2 * (units(0.7) - units(0.6))


def test_gaps_list_each_days_empty_starting_slots() -> None:
    mine = [row("c1", "C", slot="C"), row("w", "LW", team="NYR")]
    result = rank(league(mine), (SAT, SUN), playing({"BOS", "NYR"}, {"BOS"}))
    assert result.gaps == (Gap(SAT, (("D", 2),)), Gap(SUN, (("LW", 1), ("D", 2))))


def test_status_ir_slot_goalie_and_unknown_status_rules() -> None:
    mine = [row("c1", "C", ttltst=0.3, slot="C"), row("x", "C", ttltst=0.9, slot="IR")]
    fas = [
        fa("dtd", "C", ttltst=0.6, status="DTD"),
        fa("out", "C", ttltst=0.9, status="O"),
        fa("odd", "C", ttltst=0.9, status="XYZ"),
        fa("goal", "G", ttltst=None, goalie=True),
        fa("idle", "C", team="ANA", ttltst=0.9),
    ]
    result = rank(league(mine, fas), (SAT,), playing({"BOS"}))
    assert [s.add.player_id for s in result.rows] == ["dtd"]  # the IR-slot x doesn't start either


def test_an_out_drop_loses_nothing_and_shows_up_as_the_drop() -> None:
    full = [
        row("c1", "C", ttltst=0.6, slot="C"),
        row("w1", "LW", ttltst=0.6, slot="LW"),
        row("d1", "D", ttltst=0.6, slot="D"),
        row("d2", "D", ttltst=0.1, slot="D", status="O"),
        row("g", "G", goalie=True, slot="G"),
        row("b", "C", ttltst=0.2, slot="BN"),
    ]
    result = rank(league(full, [fa("fd", "D", ttltst=0.3)]), (SAT,), playing({"BOS"}))
    (best,) = result.rows
    assert not result.open_spot
    assert best.drop is not None
    assert best.drop.player_id == "d2"  # the same gain as dropping b (benched): the weaker one
    assert best.gain == units(0.3)


def test_a_drop_tie_proposes_the_open_spot_then_the_weakest_then_the_id() -> None:
    mine = [
        row("c1", "C", ttltst=0.6, slot="C"),
        row("a", "LW", team="ANA", ttltst=0.2),
        row("b", "LW", team="ANA", ttltst=0.1),
    ]
    fas = [fa("fd", "D", ttltst=0.4)]
    with_spot = rank(league(mine, fas), (SAT,), playing({"BOS"}))
    assert with_spot.rows[0].drop is None
    full = [
        *mine,
        row("z1", "LW", team="ANA", ttltst=0.1),
        row("y", "LW", team="ANA", ttltst=0.15),
        row("g", "G", goalie=True),
    ]
    no_spot = rank(league(full, fas), (SAT,), playing({"BOS"}))
    assert no_spot.rows[0].drop is not None
    assert no_spot.rows[0].drop.player_id == "b"  # b and z1 both 0.1 and idle: lower id


def test_gains_under_half_a_hundredth_are_not_listed() -> None:
    mine = [row("c1", "C", ttltst=0.5, slot="C")]
    below = fa("small", "D", ttltst=0.0049)
    at = fa("edge", "D", ttltst=0.005)
    result = rank(league(mine, [below, at]), (SAT,), playing({"BOS"}))
    assert [s.add.player_id for s in result.rows] == ["edge"]
    assert result.rows[0].gain == MIN_GAIN


def test_the_cap_rule_drops_misfits_and_marks_unknowns() -> None:
    full = [
        row("c1", "C", ttltst=0.6, slot="C"),
        row("w1", "LW", ttltst=0.6, slot="LW"),
        row("d1", "D", ttltst=0.1, slot="D", aav=1_000_000, counts=True),
        row("d2", "D", ttltst=0.6, slot="D", aav=1_000_000, counts=True),
        row("g", "G", goalie=True, slot="G"),
        row("b", "C", team="ANA", ttltst=0.6),
    ]
    fas = [
        fa("fits", "D", ttltst=0.5, aav=1_500_000),
        fa("big", "D", ttltst=0.9, aav=9_000_000),
        fa("unknown", "D", ttltst=0.4),
    ]
    view = league(full, fas, cap=10_000_000, payroll=9_000_000)
    result = rank(view, (SAT,), playing({"BOS"}))
    by_add = {s.add.player_id: s for s in result.rows}
    assert set(by_add) == {"fits", "unknown"}
    assert by_add["fits"].drop is not None
    assert by_add["fits"].drop.player_id == "d1"  # the pair Replace is checked for below
    assert by_add["unknown"].drop is not None
    assert by_add["unknown"].drop.player_id == "d1"
    assert by_add["fits"].cap_ok is True
    assert by_add["unknown"].cap_ok is None
    # Replace computes the same mark for the same pair (one drop cap-hit rule).
    swap = replace_view(view, "d1")
    assert swap is not None
    marks = {r.player.player_id: r.swap_ok for r in swap.rows}
    assert marks["fits"] is True
    assert marks["unknown"] is None
    assert marks["big"] is False


def test_unmatched_counted_rows_make_the_drop_cap_hit_unknown_in_both() -> None:
    full = [
        row("c1", "C", ttltst=0.6, slot="C"),
        row("w1", "LW", ttltst=0.6, slot="LW"),
        row("d1", "D", ttltst=0.1, slot="D"),
        row("d2", "D", ttltst=0.6, slot="D"),
        row("g", "G", goalie=True, slot="G"),
        row("b", "C", team="ANA", ttltst=0.6),
    ]
    view = league(
        full, [fa("f", "D", ttltst=0.5, aav=1)], cap=10, payroll=10, unmatched_counted=True
    )
    (s,) = rank(view, (SAT,), playing({"BOS"})).rows
    swap = replace_view(view, "d1")
    assert swap is not None
    assert s.cap_ok is None
    assert swap.rows[0].swap_ok is None


def test_replace_gains_price_every_listed_free_agent_for_the_drop() -> None:
    mine = [row("c1", "C", ttltst=0.6, slot="C"), row("d1", "D", ttltst=0.2, slot="D")]
    fas = [
        fa("fd", "D", ttltst=0.5),
        fa("out", "D", ttltst=0.9, status="O"),
        fa("idle", "D", team="ANA"),
    ]
    view = league(mine, fas)
    drop = view.by_id["d1"]
    gains = replace_gains(view, drop, fas, playing({"BOS"}, {"BOS"}))
    assert gains["fd"].gain == 2 * (units(0.5) - units(0.2))
    assert gains["out"].gain == gains["idle"].gain == -2 * units(0.2)
    assert gains["out"].starts == 0


def oracle_rank(
    view: LeagueView, playing_by_day: Sequence[frozenset[str]]
) -> list[tuple[str, str | None, int, int]]:
    """Every (add, drop) re-solved directly, with the same rules, for the top 10."""
    mine = view.my_team
    assert mine is not None
    slots = starting_slots(view.snapshot)
    roster = view.roster(mine.team_key)

    def lp(r: PlayerRow) -> LineupPlayer | None:
        if r.is_goalie or r.status not in (None, "", "DTD"):
            return None
        positions = frozenset(r.eligible_positions) & set(slots)
        value = (
            r.ttltst if r.rating is not None and r.rating.rated and r.ttltst is not None else 0.0
        )
        return LineupPlayer(r.player_id, positions, value) if positions else None

    def day(i: int, without: str | None) -> list[LineupPlayer]:
        out = []
        for r in roster:
            p = lp(r)
            if (
                p
                and not r.in_ir_slot
                and r.player_id != without
                and team_code(r.nhl_team) in playing_by_day[i]
            ):
                out.append(p)
        return out

    base = sum(best_lineup(day(i, None), slots).value for i in range(len(playing_by_day)))
    drops: list[PlayerRow | None] = [
        *([None] if has_open_spot(view) else []),
        *drop_options(roster),
    ]
    rows = []
    for f in view.free_agents():
        p = lp(f)
        plays = [
            i for i in range(len(playing_by_day)) if team_code(f.nhl_team) in playing_by_day[i]
        ]
        if p is None or not plays:
            continue
        options = []
        for d in drops:
            total, starts = 0, 0
            for i in range(len(playing_by_day)):
                players = day(i, d.player_id if d else None) + ([p] if i in plays else [])
                lineup = best_lineup(players, slots)
                total += lineup.value
                starts += f.player_id in lineup.assignment
            dv = (d.ttltst or 0.0) if d else 0.0
            options.append(
                (
                    (-(total - base), d is not None, dv, d.player_id if d else ""),
                    d,
                    total - base,
                    starts,
                )
            )
        _, d, gain, starts = min(options, key=lambda o: o[0])
        if gain >= MIN_GAIN:
            rows.append(
                (
                    -gain,
                    -((f.ttltst or 0.0) if f.rating and f.rating.rated else 0.0),
                    f.name,
                    f.player_id,
                    d,
                    gain,
                    starts,
                )
            )
    rows.sort(key=lambda r: r[:4])
    return [(r[3], r[4].player_id if r[4] else None, r[5], r[6]) for r in rows[:10]]


def test_the_top_ten_equals_an_exhaustive_direct_re_solve_on_the_demo_league() -> None:
    teams = sorted({team_code(r.nhl_team) for r in TEMPLATE.rows})
    days = playing(teams[::2], teams[1::3], teams[::5])
    result = rank(TEMPLATE, (SAT, SUN, date(2026, 11, 30)), days)
    assert len(result.rows) == 10
    got = [
        (s.add.player_id, s.drop.player_id if s.drop else None, s.gain, s.starts)
        for s in result.rows
    ]
    assert got == oracle_rank(TEMPLATE, days)


# The week plan (slates through the cache)


async def test_any_unavailable_day_blocks_the_ranking_and_names_the_days() -> None:
    view = league([row("c1", "C", slot="C")], [fa("f", "D")])
    repo = InMemoryRepository()
    clock = FakeClock(NOW)

    class Partial(Source):
        async def games_on(self, day: date) -> tuple[Game, ...]:
            self.calls.append(day)
            if day == SUN:
                from fha.sources.nhl.schedule import ScheduleError

                raise ScheduleError("down")
            return self.games

    plan = await plan_week(view, repo, Partial(), clock, Week.THIS)
    assert plan.missing == (SUN,)
    assert plan.ranking is None
    assert [s.date for s in plan.slates] == [SAT, SUN]


async def test_stale_days_are_used_and_reported() -> None:
    view = league([row("c1", "C", slot="C")], [fa("f", "D", team="TBL")])
    repo = InMemoryRepository()
    clock = FakeClock(NOW)
    source = Source()
    first = await plan_week(view, repo, source, clock, Week.THIS)
    assert first.ranking is not None
    assert [s.add.player_id for s in first.ranking.rows] == ["f"]
    clock.t += 7 * 3600
    source.fail = True
    again = await plan_week(view, repo, source, clock, Week.THIS)
    assert [s.date for s in again.stale] == [SAT, SUN]
    assert again.ranking == first.ranking
    assert await repo.get(COLLECTION, SAT.isoformat()) is not None


async def test_no_adds_left_keeps_the_gaps_but_lists_no_suggestions() -> None:
    view = league([row("c1", "C", slot="C")], [fa("f", "D", team="TBL")], roster_adds=(WEEK, 5))
    plan = await plan_week(view, InMemoryRepository(), Source(), FakeClock(NOW), Week.THIS)
    assert plan.horizon.adds_left == 0
    assert plan.ranking is not None
    assert plan.ranking.rows == ()
    assert plan.ranking.gaps
    later = await plan_week(view, InMemoryRepository(), Source(), FakeClock(NOW), Week.NEXT)
    assert later.ranking is not None
    assert later.ranking.rows


async def test_no_days_left_or_an_unknown_week_reads_no_schedule() -> None:
    source = Source()
    sunday = FakeClock(datetime.fromisoformat("2026-11-29T15:00:00Z").timestamp())
    plan = await plan_week(league([]), InMemoryRepository(), source, sunday, Week.THIS)
    assert (plan.horizon.days, plan.ranking) == ((), None)
    unknown = await plan_week(
        league([], week=(None, None)), InMemoryRepository(), source, FakeClock(NOW), Week.THIS
    )
    assert (unknown.horizon.known, unknown.ranking) == (False, None)
    assert source.calls == []


async def test_the_horizon_follows_the_eastern_date_across_midnight() -> None:
    late = FakeClock(datetime.fromisoformat("2026-11-28T04:59:00Z").timestamp())  # Fri 23:59 ET
    source = Source()
    plan = await plan_week(league([]), InMemoryRepository(), source, late, Week.THIS)
    assert plan.horizon.days == (SAT, SUN)
    late.t += 60  # Saturday in the East
    plan = await plan_week(league([]), InMemoryRepository(), source, late, Week.THIS)
    assert plan.horizon.days == (SUN,)


def test_without_my_team_there_is_nothing_to_suggest() -> None:
    view = league([row("c1")], [fa("f", "D")])
    orphan = replace(view, teams=tuple(replace(t, is_mine=False) for t in view.teams))
    assert not has_open_spot(orphan)
    assert rank(orphan, (SAT,), playing({"BOS"})) == Ranking((), (), False)
    assert replace_gains(orphan, view.by_id["c1"], [view.by_id["f"]], playing({"BOS"})) == {}


def test_dates_and_playing_teams_must_line_up() -> None:
    with pytest.raises(ValueError, match="one set of playing teams per date"):
        rank(league([]), (SAT, SUN), playing({"BOS"}))


def test_games_counts_the_teams_games_over_the_horizon() -> None:
    assert games(fa("f", team="Bos"), playing({"BOS"}, {"NYR"}, {"BOS"})) == 2
    assert games(fa("f", team=""), playing({"BOS"})) == 0
