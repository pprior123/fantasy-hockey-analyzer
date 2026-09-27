"""The rated league the screens show (SPEC §5, §7): one season's pool, rated.

``build_view`` is pure: a snapshot, the rating settings, a season choice and
the salaries in, rows out. Ratings are computed on every view from the raw
snapshot (the cache never holds them), so a settings change re-rates
everyone without a refresh. ``load_view`` gathers the inputs from the
Repository.

Salaries (SPEC §4): a rostered player's cap hit comes from his league-sheet
row (the sheet wins), otherwise the PuckPedia CSV (with any AAV override);
a free agent's from the CSV. A player with no known cap hit has ``aav``
None, shown "—", never 0.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from fha.domain.engine import (
    EngineConfig,
    Rating,
    RatingResult,
    display_order,
    prefers_baseline,
    rate,
)
from fha.domain.models import Category, PlayerSeason
from fha.services.aliases import load_aliases
from fha.services.free_agents import load_free_agent_salaries
from fha.services.league_sheet import (
    RosteredSalary,
    TabReport,
    load_league_sheet,
    payrolls,
    rostered_salaries,
    tab_reports,
)
from fha.sources.yahoo.models import IR_SLOTS, LeagueSnapshot, Player, StatLine, Team
from fha.sources.yahoo.stat_map import StatMap, build_stat_map, goalie_stats, to_player_season
from fha.storage.repository import Repository

DEFAULT_BASELINE_MIN_GP = 10  # SPEC §5 "Baseline season"


class ViewError(ValueError):
    """The snapshot can't be turned into a view (e.g. a stat the league lacks)."""


class Season(StrEnum):
    CURRENT = "current"
    LAST = "last"


class AavSource(StrEnum):
    SHEET = "sheet"  # the league sheet row (rostered players; wins, SPEC §4)
    CSV = "csv"  # the PuckPedia import, or the owner's single-player edit


@dataclass(frozen=True)
class Salaries:
    """Everything the view knows about money. Built by ``load_view``; tests build it."""

    rostered: Mapping[str, RosteredSalary] = field(default_factory=dict)  # by player_id
    free_agents: Mapping[str, int] = field(default_factory=dict)  # by player_id
    payrolls: Mapping[str, int | None] = field(default_factory=dict)  # by team_key, bound tabs
    cap: int | None = None
    # Teams whose bound tab has counted rows matched to no roster player (in review):
    # one of them may be a rostered player who shows no sheet row.
    unmatched_counted: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PlayerRow:
    player_id: str
    player_key: str
    name: str
    position: str  # Yahoo's display position, e.g. "C,LW"
    eligible_positions: tuple[str, ...]
    nhl_team: str
    status: str | None  # injury designation
    is_goalie: bool
    owner_key: str | None  # None: available (free agent or waivers)
    slot: str | None  # the roster slot today (IR / IR+ ...), rostered players only
    gp: int
    rating: Rating | None  # skaters only; unrated skaters have a Rating with Nones
    aav: int | None
    aav_source: AavSource | None
    counts: bool | None  # in the tab's PAYROLL range; None unless on a sheet row
    goalie: Mapping[str, float | None] = field(default_factory=dict)  # W / GAA / SV%

    @property
    def ttltst(self) -> float | None:
        return self.rating.ttltst if self.rating else None

    @property
    def percentile(self) -> float | None:
        return self.rating.percentile if self.rating else None

    @property
    def value(self) -> float | None:
        return self.rating.value if self.rating else None

    @property
    def norms(self) -> Mapping[Category, float]:
        return self.rating.norms if self.rating else {}

    @property
    def in_ir_slot(self) -> bool:
        return self.slot in IR_SLOTS


@dataclass(frozen=True)
class TeamInfo:
    team_key: str
    name: str
    is_mine: bool
    payroll: int | None  # the tab's PAYROLL; None if unbound or unrecognized (SPEC §5)
    player_ids: tuple[str, ...]
    unmatched_counted: bool = False  # see Salaries.unmatched_counted


@dataclass(frozen=True)
class LeagueView:
    season: Season  # the season shown
    default_season: Season  # what the view shows unless the owner toggles
    season_year: int  # e.g. 2026 for the 2026-27 season
    last_season_available: bool
    config: EngineConfig
    result: RatingResult
    rows: tuple[PlayerRow, ...]  # display order: rated skaters by rank, unrated, goalies
    teams: tuple[TeamInfo, ...]
    cap: int | None
    snapshot: LeagueSnapshot

    @property
    def by_id(self) -> dict[str, PlayerRow]:
        return {r.player_id: r for r in self.rows}

    @property
    def my_team(self) -> TeamInfo | None:
        return next((t for t in self.teams if t.is_mine), None)

    def team(self, team_key: str) -> TeamInfo | None:
        return next((t for t in self.teams if t.team_key == team_key), None)

    def roster(self, team_key: str) -> list[PlayerRow]:
        team = self.team(team_key)
        if team is None:
            return []
        by_id = self.by_id
        return [by_id[pid] for pid in team.player_ids if pid in by_id]

    def free_agents(self) -> list[PlayerRow]:
        return [r for r in self.rows if r.owner_key is None]


def default_season(
    snapshot: LeagueSnapshot, stat_map: StatMap, baseline_min_gp: int = DEFAULT_BASELINE_MIN_GP
) -> Season:
    """Last season while this season's games are too few (SPEC §5), if it was fetched."""
    if snapshot.last_season_stats is None:
        return Season.CURRENT
    current = _seasons(snapshot.pool, snapshot.stats, stat_map, {})
    return Season.LAST if prefers_baseline(current, baseline_min_gp) else Season.CURRENT


def build_view(
    snapshot: LeagueSnapshot,
    config: EngineConfig,
    salaries: Salaries,
    *,
    season: Season | None = None,
    baseline_min_gp: int = DEFAULT_BASELINE_MIN_GP,
) -> LeagueView:
    """Rate ``season`` (default: ``default_season``) over the snapshot's whole pool."""
    try:
        stat_map = build_stat_map(snapshot.settings.stat_categories, snapshot.game_stat_categories)
    except ValueError as e:
        raise ViewError(str(e)) from None
    fallback = default_season(snapshot, stat_map, baseline_min_gp)
    shown = season or fallback
    if shown is Season.LAST and snapshot.last_season_stats is None:
        raise ViewError("last season's stats weren't fetched")
    lines = snapshot.stats if shown is Season.CURRENT else snapshot.last_season_stats or {}
    aavs = _aavs(snapshot, salaries)
    pool = snapshot.pool
    result = rate(_seasons(pool, lines, stat_map, {pid: a for pid, (a, _) in aavs.items()}), config)
    owners = _owners(snapshot.teams)
    rows_by_id = {
        p.player_id: _row(p, lines, stat_map, result, owners, aavs, salaries) for p in pool
    }
    ordered = [rows_by_id[r.player_id] for r in display_order(result)]
    goalies = sorted((r for r in rows_by_id.values() if r.is_goalie), key=lambda r: r.name)
    teams = tuple(
        TeamInfo(
            t.team_key,
            t.name,
            t.is_mine,
            salaries.payrolls.get(t.team_key),
            tuple(e.player.player_id for e in t.roster),
            t.team_key in salaries.unmatched_counted,
        )
        for t in snapshot.teams
    )
    return LeagueView(
        season=shown,
        default_season=fallback,
        season_year=snapshot.game.season if shown is Season.CURRENT else snapshot.game.season - 1,
        last_season_available=snapshot.last_season_stats is not None,
        config=config,
        result=result,
        rows=(*ordered, *goalies),
        teams=teams,
        cap=salaries.cap,
        snapshot=snapshot,
    )


def _seasons(
    pool: Sequence[Player],
    lines: Mapping[str, StatLine],
    stat_map: StatMap,
    aav: Mapping[str, int | None],
) -> list[PlayerSeason]:
    return [
        to_player_season(p, lines.get(p.player_key), stat_map, aav.get(p.player_id)) for p in pool
    ]


def _owners(teams: Sequence[Team]) -> dict[str, tuple[str, str]]:
    """player_id -> (team_key, today's slot)."""
    return {e.player.player_id: (t.team_key, e.selected_position) for t in teams for e in t.roster}


def _aavs(
    snapshot: LeagueSnapshot, salaries: Salaries
) -> dict[str, tuple[int | None, AavSource | None]]:
    out: dict[str, tuple[int | None, AavSource | None]] = {}
    for p in snapshot.pool:
        sheet = salaries.rostered.get(p.player_id)
        if sheet is not None:
            out[p.player_id] = (sheet.aav, AavSource.SHEET)
        elif p.player_id in salaries.free_agents:
            out[p.player_id] = (salaries.free_agents[p.player_id], AavSource.CSV)
        else:
            out[p.player_id] = (None, None)
    return out


def _row(
    p: Player,
    lines: Mapping[str, StatLine],
    stat_map: StatMap,
    result: RatingResult,
    owners: Mapping[str, tuple[str, str]],
    aavs: Mapping[str, tuple[int | None, AavSource | None]],
    salaries: Salaries,
) -> PlayerRow:
    line = lines.get(p.player_key)
    season = to_player_season(p, line, stat_map)
    owner = owners.get(p.player_id)
    aav, source = aavs[p.player_id]
    sheet = salaries.rostered.get(p.player_id)
    return PlayerRow(
        player_id=p.player_id,
        player_key=p.player_key,
        name=p.name,
        position=p.display_position,
        eligible_positions=p.eligible_positions,
        nhl_team=p.nhl_team,
        status=p.status,
        is_goalie=p.is_goalie,
        owner_key=owner[0] if owner else None,
        slot=owner[1] if owner else None,
        gp=season.gp,
        rating=None if p.is_goalie else result.ratings.get(p.player_id),
        aav=aav,
        aav_source=source,
        counts=sheet.counted if sheet is not None else None,
        goalie=goalie_stats(line, stat_map) if p.is_goalie else {},
    )


async def load_salaries(
    repo: Repository, snapshot: LeagueSnapshot, *, cap_override: int | None = None
) -> tuple[Salaries, list[TabReport]]:
    """The salaries from the stored league sheet and the free-agent import, and the
    sheet's tab reports (new automatic row matches are persisted as they're made).

    ``cap_override`` (the ``SALARY_CAP`` config, SPEC §5) applies only when the
    sheet gives no cap.
    """
    stored = await load_league_sheet(repo)
    reports: list[TabReport] = []
    cap = None
    if stored is not None:
        sheet, _ = stored
        reports = await tab_reports(repo, sheet, snapshot.teams, await load_aliases(repo))
        cap = sheet.cap
    free_agents = await load_free_agent_salaries(repo)
    salaries = Salaries(
        rostered=rostered_salaries(reports),
        free_agents=free_agents.aav,
        payrolls=payrolls(reports),
        cap=cap if cap is not None else cap_override,
        unmatched_counted=frozenset(
            r.team_key for r in reports if r.team_key and any(x.counted for x in r.not_on_roster)
        ),
    )
    return salaries, reports
