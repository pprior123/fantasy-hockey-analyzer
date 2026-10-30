"""Update my tab (SPEC §4a "Writing", §7.5): the edits that bring the owner's tab
in line with their Yahoo roster, before → after, with the cap check.

The preview reads the league sheet afresh (and stores it, as Admin's Read
sheet does), so what it shows is the sheet as it is now. Rows awaiting match
review get a "drop this row" tick; ticking re-renders the preview (a GET, so it
changes nothing). While writing is off (no ``FHA_SHEET_WRITE=1``), the page says
to apply the edits by hand, and the confirm answers 404.

The confirm (POST) carries the preview's digest, signed and short-lived; the
service re-reads the sheet and writes only if the plan is still that one. The
outcome comes back to the page as a signed flash, as on Admin.

Hygiene (SPEC §4a): only parsed fields reach the page.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from fha.services.league_sheet import load_league_sheet, read_league_sheet
from fha.services.settings import SettingsError
from fha.services.sheet_update import prepare_update, write_update
from fha.sources.league_sheet.models import LeagueSheetError, ParsedSheet
from fha.storage.repository import RepositoryError
from fha.web.app import render, services
from fha.web.data import NO_DATA, page_data
from fha.web.format import ago
from fha.web.routes.admin import MAX_FLASH, _flash, _short, _signer

router = APIRouter()
log = logging.getLogger(__name__)

MAX_TICKS = 40  # rows a preview can tick; a tab has ~30
CONFIRM_SALT = "fha-sheet-update-v1"
CONFIRM_MAX_AGE = 600  # seconds: a preview older than this must be looked at again


def ticks(raw_ticks: list[str]) -> tuple[int, ...]:
    """The "drop this row" ticks; anything not a row number is ignored (the service
    keeps only rows awaiting review)."""
    rows = []
    for raw in raw_ticks[:MAX_TICKS]:
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
    update = await prepare_update(
        svc.repo, data.view.snapshot, sheet, ticks(request.query_params.getlist("tick"))
    )
    writing = svc.sheet_writer is not None
    ready = writing and update.plan is not None and update.plan.ok and update.plan.edits
    return _page(
        request,
        update=update,
        writing=writing,
        token=_confirmer(request).dumps(update.digest) if ready else None,
        note=note,
        read_ago=ago(svc.clock.now() - read_at) if read_at is not None else None,
        yahoo_ago=data.refreshed_label,
    )


@router.post("/admin/sheet/update")
async def sheet_update_confirm(request: Request, token: str = Form()) -> RedirectResponse:
    svc = services(request)
    if svc.sheet_writer is None or svc.sheet is None:
        raise HTTPException(404)
    form = await request.form()
    ticked = ticks([t for t in form.getlist("tick") if isinstance(t, str)])
    try:
        expected = _confirmer(request).loads(token, max_age=CONFIRM_MAX_AGE)
    except BadSignature:  # tampered, or expired
        return _back(request, "error", "That preview is too old: check it again.", ticked)
    if not isinstance(expected, str):
        return _back(request, "error", "That preview is too old: check it again.", ticked)
    try:
        data = await page_data(request)
    except NO_DATA as e:
        return _back(
            request, "error", f"Nothing was written: no Yahoo data ({type(e).__name__}).", ticked
        )
    result = await write_update(
        svc.repo,
        svc.clock,
        svc.sheet,
        svc.sheet_writer,
        data.view.snapshot,
        expected,
        ticked,
    )
    return _back(
        request, "ok" if result.ok else "error", result.message, () if result.ok else ticked
    )


def _confirmer(request: Request) -> URLSafeTimedSerializer:
    secret = request.app.state.context.settings.session_secret
    return URLSafeTimedSerializer(secret, salt=CONFIRM_SALT)


def _back(request: Request, kind: str, text: str, ticked: tuple[int, ...]) -> RedirectResponse:
    flash = _signer(request).dumps({"kind": kind, "text": _short(text, MAX_FLASH)})
    params = [("flash", flash), *(("tick", str(t)) for t in ticked)]
    return RedirectResponse(f"/admin/sheet/update?{urlencode(params)}", status_code=303)


def _page(request: Request, **values: Any) -> HTMLResponse:
    flash = _flash(request, request.query_params.get("flash"))
    return render(
        request,
        "sheet_update.html",
        active="admin",
        title="Update my tab",
        flash=flash,
        **{"writing": False, "token": None, **values},
    )
