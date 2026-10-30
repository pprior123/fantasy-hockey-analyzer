"""Update my tab (SPEC §4a "Writing", §7.5): the edits that bring the owner's tab
in line with their Yahoo roster, before → after, with the cap check.

The preview reads the league sheet afresh (and stores it, as Admin's Read
sheet does), so what it shows is the sheet as it is now. Rows awaiting match
review get a "drop this row" tick; ticking re-renders the preview (a GET, so it
changes nothing). While writing is off, the page says to apply the edits by
hand.

Hygiene (SPEC §4a): only parsed fields reach the page.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from fha.services.league_sheet import load_league_sheet, read_league_sheet
from fha.services.settings import SettingsError
from fha.services.sheet_update import prepare_update
from fha.sources.league_sheet.models import LeagueSheetError, ParsedSheet
from fha.storage.repository import RepositoryError
from fha.web.app import render, services
from fha.web.data import NO_DATA, page_data
from fha.web.format import ago

router = APIRouter()
log = logging.getLogger(__name__)

MAX_TICKS = 40  # rows a preview can tick; a tab has ~30


def ticks(request: Request) -> tuple[int, ...]:
    """The "drop this row" ticks from the query string; anything not a row number
    is ignored (the service keeps only rows awaiting review)."""
    rows = []
    for raw in request.query_params.getlist("tick")[:MAX_TICKS]:
        if raw.isascii() and raw.isdigit() and len(raw) <= 6:
            rows.append(int(raw))
    return tuple(rows)


async def fresh_sheet(request: Request) -> tuple[ParsedSheet | None, float | None, str | None]:
    """The sheet read now (stored, as Read sheet does), or the stored copy with a note
    saying why it couldn't be read."""
    svc = services(request)
    if svc.sheet is not None:
        try:
            sheet = await read_league_sheet(svc.repo, svc.sheet, svc.clock)
            return sheet, svc.clock.now(), None
        except LeagueSheetError as e:
            note = f"The sheet couldn't be read just now ({e}), so this is the stored copy."
        except RepositoryError as e:
            log.warning("the fresh sheet wasn't stored (%s)", e.summary)
            note = "The sheet was read but couldn't be stored, so this is the stored copy."
    else:
        note = None
    stored = await load_league_sheet(svc.repo)
    if stored is None:
        return None, None, note
    return stored[0], stored[1], note


@router.get("/admin/sheet/update", response_class=HTMLResponse)
async def sheet_update_page(request: Request) -> HTMLResponse:
    svc = services(request)
    try:
        data = await page_data(request)
    except NO_DATA as e:
        return _page(request, error=f"No Yahoo data yet ({type(e).__name__}).")
    except SettingsError:
        return _page(request, error="The stored rating settings are invalid (Admin).")
    sheet, read_at, note = await fresh_sheet(request)
    if sheet is None:
        return _page(request, error="No league sheet has been read yet (Admin, League sheet).")
    update = await prepare_update(svc.repo, data.view.snapshot, sheet, ticks(request))
    return _page(
        request,
        update=update,
        note=note,
        read_ago=ago(svc.clock.now() - read_at) if read_at is not None else None,
        yahoo_ago=data.refreshed_label,
    )


def _page(request: Request, **values: Any) -> HTMLResponse:
    return render(
        request,
        "sheet_update.html",
        active="admin",
        title="Update my tab",
        writing=False,
        **values,
    )
