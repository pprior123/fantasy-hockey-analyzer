"""The Matchup screen and its free-agent shortcut (SPEC §7.4)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from fha.services.teams import free_agents_by_need, matchup_view
from fha.web import format as _format  # noqa: F401 - installs the template filters
from fha.web.app import render
from fha.web.data import page_data, season_param, view_param
from fha.web.routes.players import bad_request

router = APIRouter()
WEEKS = ("current", "next")


def _here(request: Request) -> str:
    return str(request.url.path) + (f"?{request.url.query}" if request.url.query else "")


def _week(params: dict[str, str]) -> str | None:
    week = params.get("week") or "current"
    return week if week in WEEKS else None


@router.get("/matchup", response_class=HTMLResponse)
async def matchup(request: Request) -> HTMLResponse:
    params = dict(request.query_params)
    week = _week(params)
    if week is None:
        return bad_request(request, "week must be current or next.", "/matchup", "matchup")
    view_param(params.get("view"))  # no salary columns here, but the same values hold
    data = await page_data(request, season=season_param(params.get("season")))
    view = data.view
    game = matchup_view(view, next_week=week == "next")
    opp_goalies = []
    if game is not None and game.opponent is not None:
        opp_goalies = [r for r in view.roster(game.opponent.team.team_key) if r.is_goalie]
    my_goalies = [r for r in view.roster(game.me.team.team_key) if r.is_goalie] if game else []
    return render(
        request,
        "matchup.html",
        active="matchup",
        title="Matchup",
        season_label=data.season_label,
        refreshed_at=data.refreshed_label,
        data=data,
        params=params,
        week=week,
        game=game,
        categories=view.config.categories,
        my_goalies=my_goalies,
        opp_goalies=opp_goalies,
        here=_here(request),
    )


@router.get("/matchup/free-agents", response_class=HTMLResponse)
async def free_agents(request: Request) -> HTMLResponse:
    params = dict(request.query_params)
    week = _week(params)
    fits = params.get("fits") or ""
    if week is None or fits not in ("", "1"):
        return bad_request(
            request, "week must be current or next, and fits 1 or left out.", "/matchup", "matchup"
        )
    mode = view_param(params.get("view"))
    data = await page_data(request, season=season_param(params.get("season")))
    game = matchup_view(data.view, next_week=week == "next")
    need = free_agents_by_need(data.view, game, fits_my_cap=fits == "1") if game else None
    return render(
        request,
        "matchup_free_agents.html",
        active="matchup",
        title="Free agents who help",
        season_label=data.season_label,
        refreshed_at=data.refreshed_label,
        data=data,
        params=params,
        week=week,
        game=game,
        need=need,
        fits=fits == "1",
        mode=mode,
        categories=data.view.config.categories,
        here=_here(request),
    )
