"""The Rosters screen and its Replace view (SPEC §7.2)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from fha.domain.lineup import SwapGain
from fha.services.league_view import LeagueView
from fha.services.schedule import eastern_date, nights
from fha.services.suggest import (
    Week,
    WeekPlan,
    add_candidates,
    games,
    horizon,
    load_week,
    plan_week,
    replace_gains,
)
from fha.services.teams import discrepancy_flags, replace_view, summarize
from fha.web.app import render, services
from fha.web.data import page_data, season_param, view_param
from fha.web.format import day_labels, game_count, games_line, schedule_notes
from fha.web.routes.players import bad_request

router = APIRouter()


def _week(value: str | None) -> Week | None:
    """Replace's ``week``: absent means no horizon; anything but this or next is refused."""
    return Week(value) if value else None  # ValueError for anything else


def _here(request: Request) -> str:
    return str(request.url.path) + (f"?{request.url.query}" if request.url.query else "")


@router.get("/rosters", response_class=HTMLResponse)
async def rosters(request: Request) -> HTMLResponse:
    params = dict(request.query_params)
    mode = view_param(params.get("view"), totals=True)
    data = await page_data(request, season=season_param(params.get("season")))
    view = data.view
    wanted = params.get("team") or None
    team = (
        view.team(wanted) if wanted else (view.my_team or (view.teams[0] if view.teams else None))
    )
    if team is None:
        return bad_request(request, "That team isn't in the league.", "/rosters", "rosters")
    summary = summarize(view, team, discrepancies=discrepancy_flags(data.reports))
    roster = view.roster(team.team_key)
    svc = services(request)
    # Lenient like Rosters' other parameters: anything but 2 means Tonight.
    days = 2 if params.get("tonight") == "2" else 1
    slates = await nights(
        svc.repo, svc.schedule_source, svc.clock, count=days, night=eastern_date(svc.clock)
    )
    game_labels = {r.player_id: day_labels(slates, r.nhl_team) for r in roster}
    game_counts = {r.player_id: game_count(slates, r.nhl_team) for r in roster}
    counts = [game_counts[r.player_id] for r in roster if not r.in_ir_slot]
    return render(
        request,
        "rosters.html",
        active="rosters",
        title="Rosters",
        season_label=data.season_label,
        refreshed_at=data.refreshed_label,
        data=data,
        params=params,
        team=team,
        summary=summary,
        skaters=[r for r in roster if not r.is_goalie],
        goalies=[r for r in roster if r.is_goalie],
        categories=view.config.categories,
        cap=view.cap,
        mode=mode,
        here=_here(request),
        days=days,
        schedule_on=all(s.state != "unavailable" for s in slates),
        schedule_notes=schedule_notes(slates),
        games_line=games_line(slates),
        game_labels=game_labels,
        game_counts=game_counts,
        active_count=len(counts),
        tonight_count=sum(1 for n in counts if n),
        games_count=sum(n or 0 for n in counts),
    )


@router.get("/rosters/suggest", response_class=HTMLResponse)
async def suggest(request: Request) -> HTMLResponse:
    """Suggest adds (plans/weekly-suggester.md): the best free agent adds for my roster."""
    params = dict(request.query_params)
    try:
        week = Week(params.get("week") or Week.THIS)
    except ValueError:
        return bad_request(request, "week must be this or next.", "/rosters", "rosters")
    data = await page_data(request, season=season_param(params.get("season")))
    view = data.view
    mine = view.my_team
    wanted = params.get("team") or None
    if mine is None or (wanted is not None and wanted != mine.team_key):
        return bad_request(request, "Suggest works for your own roster.", "/rosters", "rosters")
    svc = services(request)
    plan = await plan_week(view, svc.repo, svc.schedule_source, svc.clock, week)
    return render(
        request,
        "suggest.html",
        active="rosters",
        title="Suggest adds",
        season_label=data.season_label,
        refreshed_at=data.refreshed_label,
        data=data,
        params=params,
        plan=plan,
        week=week.value,
        here=_here(request),
    )


@router.get("/rosters/replace", response_class=HTMLResponse)
async def replace(request: Request) -> HTMLResponse:
    params = dict(request.query_params)
    swap_only = params.get("swap_ok") or ""
    if swap_only not in ("", "1"):
        return bad_request(request, "swap_ok must be 1 or left out.", "/rosters", "rosters")
    try:
        week = _week(params.get("week"))
    except ValueError:
        return bad_request(
            request, "week must be this or next, or left out.", "/rosters", "rosters"
        )
    mode = view_param(params.get("view"))
    data = await page_data(request, season=season_param(params.get("season")))
    view = data.view
    drop = view.by_id.get(params.get("drop") or "")
    # Goalies aren't in the lineup model, and an IR-slot drop frees no active spot.
    can_plan = drop is not None and not drop.is_goalie and not drop.in_ir_slot
    priced = await _replace_week(request, view, week) if drop and can_plan and week else None
    # A week priced whole lists every add candidate (Week + is position-free);
    # otherwise the free agents at the drop's positions, as without a week.
    only = set(priced[1]) if priced is not None and priced[1] is not None else None
    swap = replace_view(view, params.get("drop") or "", swap_ok_only=swap_only == "1", only=only)
    if swap is None:
        return bad_request(
            request, "Replace works for a player on your own roster.", "/rosters", "rosters"
        )
    rows = swap.rows
    gains: dict[str, SwapGain] = {}
    game_totals: dict[str, int] = {}
    if priced is not None and priced[1] is not None:
        fas = [r.player for r in rows]
        gains = replace_gains(view, swap.drop, fas, priced[2])
        game_totals = {fa.player_id: games(fa, priced[2]) for fa in fas}
        rows = tuple(
            sorted(
                rows,
                key=lambda r: (
                    -gains[r.player.player_id].gain,
                    r.player.ttltst is None,
                    -(r.player.ttltst or 0.0),
                    r.player.name,
                    r.player.player_id,
                ),
            )
        )
    return render(
        request,
        "replace.html",
        active="rosters",
        title=f"Replace {swap.drop.name}",
        season_label=data.season_label,
        refreshed_at=data.refreshed_label,
        data=data,
        params=params,
        swap=swap,
        rows=rows,
        swap_only=swap_only == "1",
        can_plan=can_plan,
        week=week.value if week and can_plan else None,
        week_plan=priced[0] if priced else None,
        gains=gains,
        game_totals=game_totals,
        priced=only is not None,
        categories=data.view.config.categories,
        mode=mode,
        here=_here(request),
    )


async def _replace_week(
    request: Request, view: LeagueView, week: Week
) -> tuple[WeekPlan, list[str] | None, list[frozenset[str]]]:
    """The week's plan, the ids of its add candidates and each day's playing teams; the ids
    are None when the week can't be priced whole (unknown, no days left, a missing day)."""
    svc = services(request)
    plan = horizon(view.snapshot, eastern_date(svc.clock), week)
    if not plan.known or not plan.days:
        return WeekPlan(plan, (), (), None), None, []
    loaded = await load_week(svc.repo, svc.schedule_source, svc.clock, plan.days)
    missing = tuple(s.date for s in loaded if s.state == "unavailable")
    if missing:
        return WeekPlan(plan, loaded, missing, None), None, []
    playing = [frozenset(s.games) for s in loaded]
    ids = [fa.player_id for fa in add_candidates(view, playing)]
    return WeekPlan(plan, loaded, (), None), ids, playing
