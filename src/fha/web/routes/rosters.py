"""The Rosters screen and its Replace view (SPEC §7.2)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from fha.services.teams import discrepancy_flags, replace_view, summarize
from fha.web import format as _format  # noqa: F401 - installs the template filters
from fha.web.app import render
from fha.web.data import page_data, season_param
from fha.web.routes.players import bad_request

router = APIRouter()


def _here(request: Request) -> str:
    return str(request.url.path) + (f"?{request.url.query}" if request.url.query else "")


@router.get("/rosters", response_class=HTMLResponse)
async def rosters(request: Request) -> HTMLResponse:
    params = dict(request.query_params)
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
        here=_here(request),
    )


@router.get("/rosters/replace", response_class=HTMLResponse)
async def replace(request: Request) -> HTMLResponse:
    params = dict(request.query_params)
    swap_only = params.get("swap_ok") or ""
    if swap_only not in ("", "1"):
        return bad_request(request, "swap_ok must be 1 or left out.", "/rosters", "rosters")
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
        here=_here(request),
    )
