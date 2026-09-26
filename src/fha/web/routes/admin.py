"""The Admin screen (SPEC §7.5, §4a, §6): the league sheet, bindings, salary
imports, match review, the AAV edit, refresh, and the rating settings.

Every change is a POST that redirects back to ``/admin`` (post/redirect/get);
the outcome travels in a signed, short-lived ``flash`` token, so a message
can't be forged in a link and a reload repeats nothing. GET never changes
state (apart from the league sheet's automatic row matches, which every
rated screen persists: bind once, SPEC §6).

Hygiene (SPEC §4a): only ``ParsedSheet`` data is shown. The grid, with the
GMs' contact details, never leaves the parser.

The forms' values aren't trusted: a tab must be one the stored sheet has, a
team or player one the Yahoo data has, and an alias is named from the stored
row and the pool, never from the form. A value the store refuses (e.g. a tab
named ``__x__``) is a message, not a 500.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from fha.domain.engine import DEFAULT_TOP_N, DivisorMethod
from fha.domain.names import normalize_name
from fha.services.aliases import add_alias, load_aliases
from fha.services.free_agents import (
    FreeAgentError,
    confirm,
    import_free_agent_salaries,
    load_bindings,
    load_overrides,
    pending_reviews,
    set_aav_override,
    unbind,
)
from fha.services.league_sheet import (
    LeagueSheetServiceError,
    bind_tab,
    confirm_row,
    load_league_sheet,
    load_tab_bindings,
    read_league_sheet,
    suggest_bindings,
)
from fha.services.settings import SettingsError, load_rating_settings, save_rating_settings
from fha.sources.league_sheet.models import LeagueSheetError
from fha.sources.league_sheet.source import XlsxLeagueSheet
from fha.sources.puckpedia import SalaryCsvError, parse_salary_csv
from fha.storage.repository import Repository, RepositoryError
from fha.web.app import render, services
from fha.web.data import NO_DATA, PageData, page_data
from fha.web.format import ago

router = APIRouter()
log = logging.getLogger(__name__)

MAX_SHEET_BYTES = 4 * 1024 * 1024  # a league sheet download is ~100 KB; under Vercel's 4.5 MB
MAX_CSV_BYTES = 2 * 1024 * 1024  # every PuckPedia page pasted is ~200 KB
REVIEW_LIMIT = 30  # review rows shown at once; the rest wait for the next visit
SEARCH_LIMIT = 20
FLASH_SALT = "fha-admin-flash-v1"
FLASH_MAX_AGE = 600  # seconds: a message belongs to the redirect that carried it
MAX_CAP_HIT = 1_000_000_000  # dollars: far above any real cap hit (the cap is ~$120M)

DOLLARS = re.compile(r"\$?([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)")
SCALED = re.compile(r"\$?([0-9]+(?:\.[0-9]+)?)([MmKk])")
PERCENT = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%?")
METHOD_LABELS = {
    DivisorMethod.WORKBOOK: "Workbook (top-N totals over top-N GP)",
    DivisorMethod.TOP_PER82: "Top-N per-82 rates",
}


class InputError(ValueError):
    """A form value the Admin screen can't use; the message says what it wants."""


def parse_dollars(text: str) -> int:
    """A cap hit typed by the owner: "$7,250,000", "7250000" or "7.25M" / "725K".

    Commas must group thousands; a scaled amount must come to whole dollars;
    anything else ("7.25", "1,5", "7M5") is refused rather than guessed, and
    so is an amount over ``MAX_CAP_HIT`` (or a huge string: nothing is parsed
    past 20 characters).
    """
    raw = text.strip().replace(" ", "")
    amount: int | Decimal | None = None
    if len(raw) > 20:
        pass
    elif m := DOLLARS.fullmatch(raw):
        amount = int(m[1].replace(",", ""))
    elif m := SCALED.fullmatch(raw):
        scaled = Decimal(m[1]) * (1_000_000 if m[2] in "Mm" else 1_000)
        if scaled == scaled.to_integral_value():
            amount = scaled
    if amount is None:
        raise InputError(f"enter a cap hit like $7,250,000, 7250000 or 7.25M (got {text!r})")
    if amount > MAX_CAP_HIT:
        raise InputError(f"a cap hit can't be over ${MAX_CAP_HIT:,} (got {text!r})")
    return int(amount)


def parse_percent(text: str) -> str:
    """The GP floor as a percent ("2" or "2%") -> the fraction as text ("0.02")."""
    m = PERCENT.fullmatch(text.strip())
    if m is None:
        raise InputError(f"enter the GP floor as a percent, e.g. 2 (got {text!r})")
    return str(Decimal(m[1]) / 100)


def percent_text(fraction: float) -> str:
    """0.02 -> "2", 0.025 -> "2.5": the stored fraction as the form's percent."""
    return format((Decimal(str(fraction)) * 100).normalize(), "f")


# ---------------------------------------------------------------- flash

STORE_REFUSED = "the app's storage refused the change (try again in a minute)"


def _why(error: Exception) -> str:
    """An error for the flash, which travels in the redirect URL (so browser history
    and request logs). A store error's own text can name the Firestore project, so
    it is replaced, and only its type is logged."""
    if isinstance(error, RepositoryError):
        log.warning("storage refused an Admin change (%s)", type(error).__name__)
        return STORE_REFUSED
    return str(error)


def _signer(request: Request) -> URLSafeTimedSerializer:
    secret = request.app.state.context.settings.session_secret
    return URLSafeTimedSerializer(secret, salt=FLASH_SALT)


def _back(request: Request, kind: str, text: str, *, anchor: str, q: str = "") -> RedirectResponse:
    token = _signer(request).dumps({"kind": kind, "text": text})
    params = {"flash": token, **({"q": q} if q else {})}
    return RedirectResponse(f"/admin?{urlencode(params)}#{anchor}", status_code=303)


def _flash(request: Request, token: str | None) -> dict[str, str] | None:
    if not token:
        return None
    try:
        value = _signer(request).loads(token, max_age=FLASH_MAX_AGE)
    except BadSignature:  # tampered, or expired (SignatureExpired is a BadSignature)
        return None
    if not isinstance(value, dict) or value.get("kind") not in ("ok", "error"):
        return None
    return {"kind": value["kind"], "text": str(value.get("text", ""))}


async def _read(upload: UploadFile, limit: int) -> bytes:
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise InputError(f"that file is over {limit // (1024 * 1024)} MB")
    if not data:
        raise InputError("choose a file first")
    return data


# ---------------------------------------------------------------- the page


@dataclass(frozen=True)
class Found:
    """A player found by the AAV search, with where his cap hit comes from."""

    player_id: str
    name: str
    team: str
    owner: str | None
    aav: int | None
    source: str | None  # "sheet", "override", "csv" or None
    bound_key: str | None  # his bound CSV row, if any (for unbind)


@router.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, flash: str | None = None, q: str | None = None) -> HTMLResponse:
    svc = services(request)
    settings = request.app.state.context.settings
    data: PageData | None = None
    data_error = None
    try:
        data = await page_data(request)
    except NO_DATA as e:
        data_error = f"{type(e).__name__}: {e}"
    except SettingsError:  # the settings form below is where the owner fixes them
        data_error = "the stored rating settings are invalid"
    stored = await load_league_sheet(svc.repo)
    sheet, read_at = stored if stored else (None, None)
    tab_bindings = await load_tab_bindings(svc.repo)
    aliases = await load_aliases(svc.repo)
    teams = data.view.snapshot.teams if data else ()
    suggestions = suggest_bindings(sheet, teams, aliases) if sheet and data else {}
    reviews = await pending_reviews(svc.repo, data.view.snapshot.pool, aliases) if data else []
    settings_error = None
    try:
        config = await load_rating_settings(svc.repo)
    except SettingsError as e:
        settings_error = str(e)
        config = None
    found = await _search(svc.repo, data, q) if q else []
    team_names = {t.team_key: t.name for t in teams}
    return render(
        request,
        "admin.html",
        active="admin",
        title="Admin",
        season_label=data.season_label if data else None,
        refreshed_at=data.refreshed_label if data else None,
        flash=_flash(request, flash),
        data=data,
        data_error=data_error,
        sheet=sheet,
        read_at=read_at,
        read_ago=ago(svc.clock.now() - read_at) if read_at is not None else None,
        sheet_source=_sheet_source(svc.sheet),
        sheet_configured=svc.sheet is not None,
        tab_bindings=tab_bindings,
        suggestions=suggestions,
        teams=sorted(teams, key=lambda t: t.name),
        team_names=team_names,
        reports=[r for r in (data.reports if data else []) if r.team_key is not None],
        reviews=reviews[:REVIEW_LIMIT],
        reviews_total=len(reviews),
        config=config,
        settings_error=settings_error,
        methods=METHOD_LABELS,
        floor_percent=percent_text(config.gp_floor_fraction) if config else "",
        default_top_n=DEFAULT_TOP_N,
        q=q or "",
        found=found,
        settings=settings,
        repo_kind=type(svc.repo).__name__,
    )


def _sheet_source(sheet: object | None) -> str:
    kind = type(sheet).__name__ if sheet is not None else None
    return {
        "SheetsApiLeagueSheet": "the live Google Sheet (LEAGUE_SHEET_ID)",
        "XlsxLeagueSheet": "a downloaded .xlsx (LEAGUE_SHEET_XLSX)",
        None: "none configured: set LEAGUE_SHEET_ID (live) or LEAGUE_SHEET_XLSX (a download)",
    }.get(kind, str(kind))


async def _search(repo: Repository, data: PageData | None, q: str) -> list[Found]:
    if data is None:
        return []
    wanted = normalize_name(q)
    if not wanted:
        return []
    overrides = await load_overrides(repo)
    bound = {b["player_id"]: key for key, b in (await load_bindings(repo)).items()}
    names = {t.team_key: t.name for t in data.view.teams}
    out = []
    for row in data.view.rows:
        if wanted not in normalize_name(row.name):
            continue
        source = (
            "override"
            if row.player_id in overrides and row.aav_source != "sheet"
            else (str(row.aav_source) if row.aav_source else None)
        )
        out.append(
            Found(
                row.player_id,
                row.name,
                row.nhl_team,
                names.get(row.owner_key) if row.owner_key else None,
                row.aav,
                source,
                bound.get(row.player_id),
            )
        )
        if len(out) >= SEARCH_LIMIT:
            break
    return out


# ---------------------------------------------------------------- league sheet


@router.post("/admin/sheet/read")
async def sheet_read(request: Request) -> RedirectResponse:
    svc = services(request)
    if svc.sheet is None:
        return _back(request, "error", "No league sheet is configured.", anchor="sheet")
    try:
        sheet = await read_league_sheet(svc.repo, svc.sheet, svc.clock)
    except LeagueSheetError as e:
        return _back(request, "error", f"The sheet couldn't be read: {e}", anchor="sheet")
    except RepositoryError as e:
        return _back(request, "error", f"The sheet couldn't be saved: {_why(e)}.", anchor="sheet")
    return _back(request, "ok", _sheet_summary(sheet), anchor="sheet")


@router.post("/admin/sheet/upload")
async def sheet_upload(request: Request, file: UploadFile) -> RedirectResponse:
    svc = services(request)
    try:
        data = await _read(file, MAX_SHEET_BYTES)
        sheet = await read_league_sheet(svc.repo, XlsxLeagueSheet(data), svc.clock)
    except InputError as e:
        return _back(request, "error", f"Upload: {e}.", anchor="sheet")
    except LeagueSheetError as e:
        return _back(
            request, "error", f"That file isn't a league sheet we can read: {e}", anchor="sheet"
        )
    except RepositoryError as e:
        return _back(request, "error", f"The sheet couldn't be saved: {_why(e)}.", anchor="sheet")
    return _back(request, "ok", _sheet_summary(sheet), anchor="sheet")


def _sheet_summary(sheet: Any) -> str:
    ok = sum(t.status == "ok" for t in sheet.tabs)
    bad = len(sheet.tabs) - ok
    text = f"Read {len(sheet.tabs)} team tabs: {ok} ok"
    return text + (f", {bad} unrecognized." if bad else ".")


async def _data_for(request: Request, what: str) -> PageData:
    """The rated view a form needs to check its values against; InputError without it."""
    try:
        return await page_data(request)
    except NO_DATA as e:
        raise InputError(f"{what} needs Yahoo data first ({type(e).__name__})") from None
    except SettingsError:
        raise InputError(f"{what} needs valid rating settings first (below)") from None


@router.post("/admin/bind")
async def bind(request: Request, tab: str = Form(), team_key: str = Form("")) -> RedirectResponse:
    svc = services(request)
    try:
        stored = await load_league_sheet(svc.repo)
        tabs = {t.name for t in stored[0].tabs} if stored else set()
        if tab not in tabs and tab not in await load_tab_bindings(svc.repo):
            raise InputError("that tab isn't in the stored sheet")
        if team_key:
            data = await _data_for(request, "Binding")
            if data.view.team(team_key) is None:
                raise InputError("that team isn't in the league")
        await bind_tab(svc.repo, tab, team_key or None)
    except (InputError, LeagueSheetServiceError, RepositoryError) as e:
        return _back(request, "error", f"{tab}: {_why(e)}.", anchor="bindings")
    text = f"{tab} is now bound." if team_key else f"{tab} is unbound."
    return _back(request, "ok", text, anchor="bindings")


@router.post("/admin/sheet/confirm")
async def sheet_confirm(
    request: Request, tab: str = Form(), key: str = Form(), player_id: str = Form()
) -> RedirectResponse:
    svc = services(request)
    try:
        data = await _data_for(request, "Matching a row")
        report = next((r for r in data.reports if r.tab.name == tab), None)
        if report is None or report.team_key is None:
            raise InputError(f"tab {tab!r} isn't bound to a team yet")
        if key not in {m.key for m in report.rows}:
            raise InputError("that row isn't on the tab")
        team = data.view.team(report.team_key)
        if team is None or player_id not in team.player_ids:
            raise InputError("that player isn't on the tab's Yahoo roster")
        await confirm_row(svc.repo, tab, key, player_id)
    except (InputError, LeagueSheetServiceError, RepositoryError) as e:
        return _back(request, "error", f"{tab}: {_why(e)}.", anchor="discrepancies")
    return _back(request, "ok", f"{tab}: row matched.", anchor="discrepancies")


# ---------------------------------------------------------------- free agents


@router.post("/admin/csv")
async def csv_import(request: Request, file: UploadFile) -> RedirectResponse:
    svc = services(request)
    try:
        raw = await _read(file, MAX_CSV_BYTES)
        rows = parse_salary_csv(raw)
        data = await page_data(request)
    except InputError as e:
        return _back(request, "error", f"Upload: {e}.", anchor="csv")
    except SalaryCsvError as e:
        return _back(request, "error", f"The CSV wasn't imported: {e}.", anchor="csv")
    except NO_DATA as e:
        return _back(
            request, "error", f"Import needs Yahoo data first ({type(e).__name__}).", anchor="csv"
        )
    except SettingsError:
        return _back(request, "error", "Import needs valid rating settings first.", anchor="csv")
    try:
        report = await import_free_agent_salaries(
            svc.repo, rows, data.view.snapshot.pool, await load_aliases(svc.repo)
        )
    except RepositoryError as e:
        return _back(request, "error", f"The CSV couldn't be saved: {_why(e)}.", anchor="csv")
    if not report.changed:
        text = f"No changes: the same {report.rows} rows as last time."
    else:
        text = (
            f"Imported {report.rows} rows: {report.bound} bound now, "
            f"{report.already_bound} already bound, {len(report.reviews)} to review."
        )
    if report.conflicts:
        text += f" Not imported (two cap hits for one player): {', '.join(report.conflicts)}."
    if report.unknown_teams:
        text += f" Unknown teams: {', '.join(report.unknown_teams)}."
    return _back(request, "ok", text, anchor="csv")


@router.post("/admin/fa/confirm")
async def fa_confirm(
    request: Request, key: str = Form(), player_id: str = Form(), alias: str = Form("")
) -> RedirectResponse:
    """Bind a CSV row to a pool player; with ``alias``, also save that the row's name
    means the player's (both names from the store and the pool, not the form)."""
    svc = services(request)
    try:
        player = (await _data_for(request, "Matching")).view.by_id.get(player_id)
        if player is None:
            raise InputError("that player isn't in the pool")
        row = await confirm(svc.repo, key, player_id)
        text = "Matched."
        if alias and normalize_name(row.name) != normalize_name(player.name):
            await add_alias(svc.repo, player.name, row.name)
            text += f" Alias saved: {row.name} = {player.name}."
    except (InputError, FreeAgentError, RepositoryError) as e:
        return _back(request, "error", f"{_why(e)}.", anchor="review")
    return _back(request, "ok", text, anchor="review")


@router.post("/admin/fa/unbind")
async def fa_unbind(request: Request, key: str = Form(), q: str = Form("")) -> RedirectResponse:
    try:
        await unbind(services(request).repo, key)
    except RepositoryError as e:
        return _back(request, "error", f"{_why(e)}.", anchor="aav", q=q)
    return _back(request, "ok", "Unbound: that row waits for review again.", anchor="aav", q=q)


@router.post("/admin/aav")
async def aav(
    request: Request,
    player_id: str = Form(),
    aav: str = Form(""),
    clear: str = Form(""),
    q: str = Form(""),
) -> RedirectResponse:
    svc = services(request)
    try:
        value = None if clear else parse_dollars(aav)
        if value is not None:
            pool = (await _data_for(request, "A cap hit")).view.by_id
            if player_id not in pool:
                raise InputError("that player isn't in the pool")
        await set_aav_override(svc.repo, player_id, value)
    except (InputError, FreeAgentError, RepositoryError) as e:
        return _back(request, "error", f"{_why(e)}.", anchor="aav", q=q)
    text = "Override cleared." if value is None else f"Cap hit set to ${value:,}."
    return _back(request, "ok", text, anchor="aav", q=q)


# ---------------------------------------------------------------- rating settings


@router.post("/admin/settings")
async def rating_settings(
    request: Request,
    divisor_method: str = Form(),
    divisor_top_n: str = Form(""),
    gp_floor_percent: str = Form(""),
) -> RedirectResponse:
    svc = services(request)
    try:
        fraction = parse_percent(gp_floor_percent)
        config = await save_rating_settings(
            svc.repo,
            {
                "divisor_method": divisor_method,
                "divisor_top_n": divisor_top_n,
                "gp_floor_fraction": fraction,
            },
        )
    except (InputError, SettingsError, RepositoryError) as e:
        return _back(request, "error", f"Not saved: {_why(e)}.", anchor="settings")
    text = (
        f"Saved: {METHOD_LABELS[config.divisor_method]}, top {config.top_n}, "
        f"GP floor {percent_text(config.gp_floor_fraction)}%. Every player is re-rated."
    )
    return _back(request, "ok", text, anchor="settings")
