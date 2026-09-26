"""The League screen (SPEC §7.3): one row per team."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from fha.services.teams import discrepancy_flags, league_table
from fha.web import format as _format  # noqa: F401 - installs the template filters
from fha.web.app import render
from fha.web.data import page_data, season_param, view_param

router = APIRouter()


@router.get("/league", response_class=HTMLResponse)
async def league(request: Request) -> HTMLResponse:
    params = dict(request.query_params)
    mode = view_param(params.get("view"))
    data = await page_data(request, season=season_param(params.get("season")))
    view = data.view
    return render(
        request,
        "league.html",
        active="league",
        title="League",
        season_label=data.season_label,
        refreshed_at=data.refreshed_label,
        data=data,
        params=params,
        table=league_table(view, discrepancies=discrepancy_flags(data.reports)),
        categories=view.config.categories,
        cap=view.cap,
        mode=mode,
        here=str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
    )
