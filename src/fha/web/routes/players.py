"""The Players screen (SPEC §7.1), and the Refresh button's POST."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from fha.services.players_table import POSITIONS, Owner, PlayersQuery, QueryError, SortKey, select
from fha.web import format as _format  # noqa: F401 - installs the template filters
from fha.web.app import render, services
from fha.web.auth import safe_next
from fha.web.data import page_data, season_param

router = APIRouter()

VIEWS = ("money", "cats")  # the Categories toggle: salary columns, or the 7 norms
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
    params = dict(request.query_params)
    try:
        query = PlayersQuery.from_params(params)
    except QueryError as e:
        return bad_request(
            request, f"That filter isn't one the Players screen knows: {e}.", "/players", "players"
        )
    mode = params.get("view") or "money"
    if mode not in VIEWS:
        return bad_request(request, f"Unknown view {mode!r}.", "/players", "players")
    data = await page_data(request, season=season_param(params.get("season")))
    view = data.view
    if query.team_key is not None and view.team(query.team_key) is None:
        return bad_request(request, "That team isn't in the league.", "/players", "players")
    sort = query.sort.value
    return render(
        request,
        "players.html",
        active="players",
        title="Players",
        season_label=data.season_label,
        refreshed_at=data.refreshed_label,
        data=data,
        rows=select(view, query),
        query=query,
        params=params,
        mode=mode,
        sort=sort,
        columns=COLUMNS,
        money_columns=MONEY_COLUMNS,
        owner_chips=OWNER_CHIPS,
        positions=POSITIONS,
        categories=view.config.categories,
        team_names={t.team_key: t.name for t in view.teams},
        sort_keys={k.value for k in SortKey},
        here=str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
    )


@router.post("/refresh")
async def refresh(request: Request, next: str = Form("")) -> Response:
    """The Refresh button: read Yahoo again now, then go back where the owner was."""
    await services(request).refresh.current(force=True)
    return RedirectResponse(safe_next(next), status_code=303)
