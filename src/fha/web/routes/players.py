"""The Players screen (SPEC §7.1), its "Show more" rows, and the Refresh button's POST."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from fha.domain.schedule import mark
from fha.services.players_table import (
    MAX_Q_CHARS,
    PAGE_SIZE,
    POSITIONS,
    Owner,
    PlayersQuery,
    QueryError,
    SortKey,
    select,
    zero_gp_hidden,
)
from fha.services.schedule import eastern_date, team_code, tonight
from fha.web.app import render, services
from fha.web.auth import safe_next
from fha.web.data import page_data, season_param, view_param
from fha.web.format import game_label, url

router = APIRouter()

COLUMNS = (
    ("name", "Name"),
    ("pos", "Pos"),
    ("team", "Team"),
    ("owner", "Owner"),
    ("gp", "GP"),
    ("ttltst", "TTLTST"),
    ("pctl", "Pctl"),
)
MONEY_COLUMNS = (("aav", "AAV"), ("value", "$/TTLTST"))
OWNER_CHIPS = (
    (Owner.ALL, "All"),
    (Owner.MINE, "My Team"),
    (Owner.FREE, "Free Agents"),
    (Owner.TAKEN, "Taken"),
)
# What a name search keeps (plans/players-name-search.md §1): how the list is shown,
# not which players are in it; the owner chip, team, position and min GP start over.
DISPLAY_STATE = ("view", "season", "sort", "dir")


def bad_request(request: Request, message: str, back: str, active: str) -> HTMLResponse:
    return render(
        request,
        "bad_request.html",
        status_code=400,
        active=active,
        title="Not understood",
        message=message,
        back=back,
    )


def _reload_list(
    request: Request, params: dict[str, str], colspan: int, *, new_night: bool
) -> HTMLResponse:
    return render(
        request,
        "_players_reload.html",
        colspan=colspan,
        reload_url=url("/players", params),
        new_night=new_night,
    )


@router.get("/players", response_class=HTMLResponse)
async def players(request: Request) -> HTMLResponse:
    """The page: rows up to ``start`` + one page (``start`` is the no-JS "Show more")."""
    return await _players(request, partial=False)


@router.get("/players/rows", response_class=HTMLResponse)
async def more_players(request: Request) -> HTMLResponse:
    """htmx's "Show more": the one page of rows from ``start``, and the next button."""
    return await _players(request, partial=True)


async def _players(request: Request, *, partial: bool) -> HTMLResponse:
    params = dict(request.query_params)
    try:
        query = PlayersQuery.from_params(params)
    except QueryError as e:
        return bad_request(
            request, f"That filter isn't one the Players screen knows: {e}.", "/players", "players"
        )
    mode = view_param(params.get("view"), totals=True)
    if partial:
        request.state.suppress_sheet_auto = True
    data = await page_data(request, season=season_param(params.get("season")))
    view = data.view
    if query.team_key is not None and view.team(query.team_key) is None:
        return bad_request(request, "That team isn't in the league.", "/players", "players")
    categories = view.config.categories
    colspan = len(COLUMNS) + (len(MONEY_COLUMNS) if mode == "money" else len(categories))
    svc = services(request)
    page_night = params.pop("night", None)
    page_slate = params.pop("slate", None)
    day = eastern_date(svc.clock)
    if partial and page_night is not None and page_night != day.isoformat():
        return _reload_list(request, params, colspan, new_night=True)
    slate = await tonight(svc.repo, svc.schedule_source, svc.clock, night=day)
    playing = None if slate.state == "unavailable" else slate.games
    team_labels = {code: game_label(mark(code, game)) for code, game in slate.games.items()}
    fingerprint = (
        "off"
        if playing is None
        else sha256(json.dumps(team_labels, sort_keys=True).encode()).hexdigest()[:12]
    )
    if partial and page_slate is not None and page_slate != fingerprint:
        return _reload_list(request, params, colspan, new_night=False)
    rows = select(view, query, totals=mode == "totals", playing=playing)
    end = query.start + PAGE_SIZE
    shown = rows[query.start if partial else 0 : end]
    params.pop("start", None)  # every other link starts from the top again
    if not query.searching:
        params.pop("q", None)  # "" or "!!": no search, so no link carries it
    display_state = {k: params[k] for k in DISPLAY_STATE if params.get(k)}
    context: dict[str, Any] = {
        "rows": shown,
        "next_start": end if end < len(rows) else None,
        "left": max(len(rows) - end, 0),
        "page_size": PAGE_SIZE,
        "params": params,
        "mode": mode,
        "categories": categories,
        "colspan": colspan,
        "team_names": {t.team_key: t.name for t in view.teams},
        "game_labels": {r.player_id: team_labels.get(team_code(r.nhl_team)) for r in shown},
        "paging_night": slate.date.isoformat() if svc.schedule_source is not None else None,
        "paging_slate": fingerprint if svc.schedule_source is not None else None,
    }
    if partial:
        return render(request, "_player_rows.html", **context)
    return render(
        request,
        "players.html",
        active="players",
        title="Players",
        season_label=data.season_label,
        refreshed_at=data.refreshed_label,
        data=data,
        total=len(rows),
        zero_gp_hidden=zero_gp_hidden(view, query, playing=playing),
        slate=slate,
        tonight_active=query.tonight and playing is not None,
        query=query,
        sort=query.sort.value,
        columns=COLUMNS,
        money_columns=MONEY_COLUMNS,
        owner_chips=OWNER_CHIPS,
        display_state=display_state,
        max_q_chars=MAX_Q_CHARS,
        positions=POSITIONS,
        sort_keys={k.value for k in SortKey},
        here=str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
        **context,
    )


@router.post("/refresh")
async def refresh(request: Request, next: str = Form("")) -> Response:
    """The Refresh button: read Yahoo again now, then go back where the owner was."""
    await services(request).refresh.current(force=True)
    return RedirectResponse(safe_next(next), status_code=303)
