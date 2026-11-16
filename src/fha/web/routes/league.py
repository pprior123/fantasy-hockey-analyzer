"""The League screen (SPEC §7.3): one row per team, with its standing."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from fha.services.players_table import QueryError
from fha.services.teams import LeagueQuery, discrepancy_flags, league_table
from fha.web import format as _format  # noqa: F401 - installs the template filters
from fha.web.app import render
from fha.web.data import page_data, season_param, view_param
from fha.web.routes.players import bad_request

router = APIRouter()


@router.get("/league", response_class=HTMLResponse)
async def league(request: Request) -> HTMLResponse:
    params = dict(request.query_params)
    try:
        query = LeagueQuery.from_params(params)
    except QueryError as e:
        return bad_request(request, f"That sort isn't one League knows: {e}.", "/league", "league")
    mode = view_param(params.get("view"))
    data = await page_data(request, season=season_param(params.get("season")))
    view = data.view
    # A category outside the config is refused by league_table, but the app's categories
    # are always the engine's full set, so every category parsed here is one of them.
    table = league_table(view, query, discrepancies=discrepancy_flags(data.reports))
    return render(
        request,
        "league.html",
        active="league",
        title="League",
        season_label=data.season_label,
        refreshed_at=data.refreshed_label,
        data=data,
        params=params,
        table=table,
        query=query,
        sort=query.sort.value,
        categories=table.categories,
        cap=view.cap,
        mode=mode,
        here=str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
    )
