"""The Rosters screen and its Replace view (SPEC §7.2)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from fha.services.schedule import tonight
from fha.services.teams import discrepancy_flags, replace_view, summarize
from fha.web.app import render, services
from fha.web.data import page_data, season_param, view_param
from fha.web.format import game_label
from fha.web.routes.players import bad_request

router = APIRouter()


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
    slate = await tonight(svc.repo, svc.schedule_source, svc.clock)
    game_labels = {r.player_id: game_label(slate.matchup(r.nhl_team)) for r in roster}
    outside_ir = [r for r in roster if not r.in_ir_slot]
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
        slate=slate,
        game_labels=game_labels,
        active_count=len(outside_ir),
        tonight_count=sum(game_labels[r.player_id] is not None for r in outside_ir),
    )


@router.get("/rosters/replace", response_class=HTMLResponse)
async def replace(request: Request) -> HTMLResponse:
    params = dict(request.query_params)
    swap_only = params.get("swap_ok") or ""
    if swap_only not in ("", "1"):
        return bad_request(request, "swap_ok must be 1 or left out.", "/rosters", "rosters")
    mode = view_param(params.get("view"))
    data = await page_data(request, season=season_param(params.get("season")))
    swap = replace_view(data.view, params.get("drop") or "", swap_ok_only=swap_only == "1")
    if swap is None:
        return bad_request(
            request, "Replace works for a player on your own roster.", "/rosters", "rosters"
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
        swap_only=swap_only == "1",
        categories=data.view.config.categories,
        mode=mode,
        here=_here(request),
    )
