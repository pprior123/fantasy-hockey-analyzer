"""The Players screen (SPEC §7.1), its "Show more" rows, and the Refresh button's POST."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from fha.services.players_table import (
    PAGE_SIZE,
    POSITIONS,
    Owner,
    PlayersQuery,
    QueryError,
    SortKey,
    select,
    zero_gp_hidden,
)
from fha.web import format as _format  # noqa: F401 - installs the template filters
from fha.web.app import render, services
from fha.web.auth import safe_next
from fha.web.data import page_data, season_param, view_param

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
    data = await page_data(request, season=season_param(params.get("season")))
    view = data.view
    if query.team_key is not None and view.team(query.team_key) is None:
        return bad_request(request, "That team isn't in the league.", "/players", "players")
    rows = select(view, query, totals=mode == "totals")
    end = query.start + PAGE_SIZE
    params.pop("start", None)  # every other link starts from the top again
    categories = view.config.categories
    context: dict[str, Any] = {
        "rows": rows[query.start if partial else 0 : end],
        "next_start": end if end < len(rows) else None,
        "left": max(len(rows) - end, 0),
        "page_size": PAGE_SIZE,
        "params": params,
        "mode": mode,
        "categories": categories,
        "colspan": len(COLUMNS) + (len(MONEY_COLUMNS) if mode == "money" else len(categories)),
        "team_names": {t.team_key: t.name for t in view.teams},
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
        zero_gp_hidden=zero_gp_hidden(view, query),
        query=query,
        sort=query.sort.value,
        columns=COLUMNS,
        money_columns=MONEY_COLUMNS,
        owner_chips=OWNER_CHIPS,
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
