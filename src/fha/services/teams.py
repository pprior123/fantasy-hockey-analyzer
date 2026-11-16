"""Team-level views (SPEC §7.2-7.4): rosters, the league table, the matchup,
and the Replace view, over a ``LeagueView``. Pure.

Profiles compare rates, not projected weekly totals (SPEC §5); the screens
say so. Cap predicates are None ("unavailable") whenever an amount they need
is unknown, and the filters that use them exclude those rows and count them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from fha.domain.cap import CapHit, cap_room, fits, room_after, swap_ok
from fha.domain.models import Category
from fha.domain.profiles import (
    DEFAULT_PROFILE_CONFIG,
    Matchup,
    Member,
    ProfileConfig,
    TeamProfile,
    compare,
    need_score,
    team_profile,
)
from fha.domain.standings import Cell, TeamStanding, standings
from fha.services.league_sheet import TabReport
from fha.services.league_view import LeagueView, PlayerRow, TeamInfo
from fha.services.players_table import QueryError, _comparable

POSITIONS = frozenset({"C", "LW", "RW", "D", "G"})  # real positions, not BN / Util / IR


@dataclass(frozen=True)
class TeamSummary:
    team: TeamInfo
    profile: TeamProfile
    payroll: int | None
    cap_room: int | None  # None: payroll or cap unavailable
    has_discrepancies: bool = False

    @property
    def over_cap(self) -> bool:
        return self.cap_room is not None and self.cap_room < 0


def members(view: LeagueView, team_key: str) -> list[Member]:
    return [
        Member(r.player_id, r.in_ir_slot, r.rating if not r.is_goalie else None)
        for r in view.roster(team_key)
    ]


def summarize(
    view: LeagueView,
    team: TeamInfo,
    *,
    config: ProfileConfig = DEFAULT_PROFILE_CONFIG,
    discrepancies: Mapping[str, bool] | None = None,
) -> TeamSummary:
    categories = view.config.categories  # the engine's, so every member has these norms
    profile = team_profile(members(view, team.team_key), replace(config, categories=categories))
    return TeamSummary(
        team=team,
        profile=profile,
        payroll=team.payroll,
        cap_room=cap_room(view.cap, team.payroll),
        has_discrepancies=bool((discrepancies or {}).get(team.team_key)),
    )


class LeagueSort(StrEnum):
    TEAM = "team"
    ROTO = "roto"
    TTLTST = "ttltst"
    PCTL = "pctl"  # the Pctl of the team's average TTLTST
    PAYROLL = "payroll"
    ROOM = "room"


@dataclass(frozen=True)
class LeagueQuery:
    """The League table's sort (SPEC §7.3): a column, or a category; best first by
    default (the team name A to Z)."""

    sort: LeagueSort | Category = LeagueSort.PCTL
    descending: bool = True

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> LeagueQuery:
        """From query-string values; blank means the default. Unknown values are refused."""
        sort_text = (params.get("sort") or "").strip() or LeagueSort.PCTL.value
        direction = (params.get("dir") or "").strip() or None
        for name, value in (("sort", sort_text), ("dir", direction or "")):
            if len(value) > 12:  # the 400 page quotes ≤ 12 chars
                raise QueryError(f"unknown {name} {value[:12] + '…'!r}")
        sort: LeagueSort | Category
        if sort_text in {c.value for c in Category}:
            sort = Category(sort_text)
        elif sort_text in {k.value for k in LeagueSort}:
            sort = LeagueSort(sort_text)
        else:
            raise QueryError(f"unknown sort {sort_text!r}")
        if direction not in (None, "asc", "desc"):
            raise QueryError(f"dir must be asc or desc, got {direction!r}")
        descending = direction == "desc" if direction else sort is not LeagueSort.TEAM
        return cls(sort, descending)


@dataclass(frozen=True)
class LeagueRow:
    summary: TeamSummary
    standing: TeamStanding

    @property
    def team(self) -> TeamInfo:
        return self.summary.team


@dataclass(frozen=True)
class LeagueTable:
    rows: tuple[LeagueRow, ...]
    categories: tuple[Category, ...]
    names: Mapping[str, str]  # team_key -> name

    @property
    def mine(self) -> LeagueRow | None:
        return next((r for r in self.rows if r.team.is_mine), None)

    def above(self, cell: Cell) -> str:
        """The teams a gap is to, by name."""
        return ", ".join(sorted(self.names[k] for k in cell.above))


def league_table(
    view: LeagueView,
    query: LeagueQuery | None = None,
    *,
    config: ProfileConfig = DEFAULT_PROFILE_CONFIG,
    discrepancies: Mapping[str, bool] | None = None,
) -> LeagueTable:
    """One row per team with its standing (SPEC §7.3), sorted by ``query`` (default:
    Pctl, best first); teams with no value in the sort column last, ties by name.
    Pctl is a step function of TTLTST (``1 + #players above``), so equal Pctls are
    ordered by TTLTST first: the Pctl order is exactly TTLTST's. The owner's team is
    not pinned: the order reads as standings."""
    query = query or LeagueQuery()
    categories = view.config.categories
    if isinstance(query.sort, Category) and query.sort not in categories:
        raise QueryError(f"{query.sort.value} isn't one of this league's categories")
    summaries = [summarize(view, t, config=config, discrepancies=discrepancies) for t in view.teams]
    table = standings(
        {s.team.team_key: s.profile for s in summaries},
        list(view.result.ratings.values()),
        view.result.pool_size,
        view.result.divisors,
    )
    rows = [LeagueRow(s, table[s.team.team_key]) for s in summaries]
    key = _league_sort_value(query.sort)
    present = [r for r in rows if key(r) is not None]
    missing = sorted((r for r in rows if key(r) is None), key=lambda r: r.team.name)
    present.sort(key=lambda r: r.team.name)
    if query.sort is LeagueSort.PCTL:  # then TTLTST; a Pctl implies a TTLTST
        present.sort(key=lambda r: r.standing.ttltst.value or 0.0, reverse=query.descending)
    present.sort(key=lambda r: _comparable(key(r)), reverse=query.descending)
    return LeagueTable(
        tuple([*present, *missing]), categories, {t.team_key: t.name for t in view.teams}
    )


def _league_sort_value(sort: LeagueSort | Category) -> Callable[[LeagueRow], object | None]:
    if isinstance(sort, Category):
        return lambda r: r.standing.categories[sort].value
    by: dict[LeagueSort, Callable[[LeagueRow], object | None]] = {
        LeagueSort.TEAM: lambda r: r.team.name,
        LeagueSort.ROTO: lambda r: r.standing.roto,
        LeagueSort.TTLTST: lambda r: r.standing.ttltst.value,
        LeagueSort.PCTL: lambda r: r.standing.ttltst.pctl,
        LeagueSort.PAYROLL: lambda r: r.summary.payroll,
        LeagueSort.ROOM: lambda r: r.summary.cap_room,
    }
    return by[sort]


# ---------------------------------------------------------------- matchup


@dataclass(frozen=True)
class MatchupView:
    week: int
    week_start: str | None
    week_end: str | None
    me: TeamSummary
    opponent: TeamSummary | None  # None: a bye, or no scoreboard for the week
    comparison: Matchup | None


def matchup_view(
    view: LeagueView,
    *,
    next_week: bool = False,
    config: ProfileConfig = DEFAULT_PROFILE_CONFIG,
) -> MatchupView | None:
    """The owner's matchup this week or next (SPEC §7.4); None without the owner's team
    or without a next week (after the last week)."""
    mine = view.my_team
    board = view.snapshot.next_scoreboard if next_week else view.snapshot.scoreboard
    if mine is None or board is None:
        return None
    me = summarize(view, mine, config=config)
    opp_key = board.opponent(mine.team_key)
    opp_team = view.team(opp_key) if opp_key else None
    opponent = summarize(view, opp_team, config=config) if opp_team else None
    comparison = compare(me.profile, opponent.profile, config) if opponent else None
    return MatchupView(board.week, board.week_start, board.week_end, me, opponent, comparison)


@dataclass(frozen=True)
class NeedRow:
    player: PlayerRow
    need: float
    fits: bool | None  # a straight pickup within my cap room; None: unknown


@dataclass(frozen=True)
class NeedList:
    rows: tuple[NeedRow, ...]
    excluded_unknown: int = 0  # rows left out by "fits my cap" because it's unknown


def free_agents_by_need(
    view: LeagueView, matchup: MatchupView, *, fits_my_cap: bool = False
) -> NeedList:
    """Free agents sorted by need_score over the trailing categories (SPEC §7.4)."""
    trailing = matchup.comparison.trailing if matchup.comparison else frozenset()
    room = matchup.me.cap_room
    rows = []
    unknown = 0
    for r in view.free_agents():
        if r.rating is None or not trailing:
            continue
        need = need_score(r.rating, trailing)
        if need is None:
            continue
        fit = fits(r.aav, room)
        if fits_my_cap and fit is not True:
            if fit is None:
                unknown += 1
            continue
        rows.append(NeedRow(r, need, fit))
    rows.sort(key=lambda n: (-n.need, n.player.name, n.player.player_id))
    return NeedList(tuple(rows), unknown)


# ---------------------------------------------------------------- replace


@dataclass(frozen=True)
class ReplaceRow:
    player: PlayerRow  # the free agent
    delta_ttltst: float | None  # his TTLTST minus the dropped player's
    room_after: int | None
    swap_ok: bool | None
    delta_norms: Mapping[Category, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplaceView:
    drop: PlayerRow
    rows: tuple[ReplaceRow, ...]
    excluded_unknown: int = 0  # left out by the swap_ok toggle because it's unknown


def positions(row: PlayerRow) -> frozenset[str]:
    return frozenset(row.eligible_positions) & POSITIONS


def replace_view(
    view: LeagueView, drop_id: str, *, swap_ok_only: bool = False
) -> ReplaceView | None:
    """Free agents eligible at any of the dropped player's positions, by TTLTST (SPEC §7.2).

    None if ``drop_id`` isn't on the owner's roster.
    """
    mine = view.my_team
    drop = view.by_id.get(drop_id)
    if mine is None or drop is None or drop.owner_key != mine.team_key:
        return None
    room = cap_room(view.cap, mine.payroll)
    if drop.counts is not None:
        hit: CapHit | None = CapHit(drop.aav, bool(drop.counts))
    elif mine.unmatched_counted:  # his counted row may be one no player matched (M4R9B-1)
        hit = CapHit(None, True)  # so what the drop frees is unknown
    else:
        hit = None  # missing from the tab: frees nothing (SPEC §5)
    wanted = positions(drop)
    rows: list[ReplaceRow] = []
    unknown = 0
    for fa in view.free_agents():
        if not (positions(fa) & wanted):
            continue
        ok = swap_ok(room, hit, fa.aav)
        if swap_ok_only and ok is not True:
            if ok is None:
                unknown += 1
            continue
        rows.append(
            ReplaceRow(
                player=fa,
                delta_ttltst=_delta(fa.ttltst, drop.ttltst),
                room_after=room_after(room, hit, fa.aav),
                swap_ok=ok,
                delta_norms=_delta_norms(fa, drop),
            )
        )
    rows.sort(
        key=lambda r: (
            r.player.ttltst is None,
            -(r.player.ttltst or 0.0),
            r.player.name,
            r.player.player_id,
        )
    )
    return ReplaceView(drop, tuple(rows), unknown)


def _delta(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


def _delta_norms(fa: PlayerRow, drop: PlayerRow) -> dict[Category, float]:
    if not (fa.rating and fa.rating.rated and drop.rating and drop.rating.rated):
        return {}
    return {c: fa.norms[c] - drop.norms[c] for c in fa.norms if c in drop.norms}


def discrepancy_flags(reports: Iterable[TabReport]) -> dict[str, bool]:
    """team_key -> the Rosters badge (SPEC §7.2), from the league-sheet tab reports."""
    return {r.team_key: r.has_discrepancies for r in reports if r.team_key is not None}
