"""The league sheet from the Google Sheets API → ``Grid`` (production; SPEC §4a).

One ``spreadsheets.get`` request with ``includeGridData``, masked to tab titles
and each cell's entered value (which holds the formula) and computed value.
So formulas and values come together in a single request. The service
account's token comes from an injected callable; if Google refuses it (a
revoked key, ``invalid_grant``) that is a ``LeagueSheetError`` like any other
failed read. The spreadsheet ID is private (SPEC §8), so it never appears in
an error, and an httpx error, whose message could carry the URL, is replaced
rather than chained.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from fha.sources.google_auth import GoogleAuthError
from fha.sources.league_sheet.grid import Cell, Grid, Tab, Value, a1
from fha.sources.league_sheet.models import LeagueSheetError

API = "https://sheets.googleapis.com/v4/spreadsheets/"
FIELDS = (
    "sheets(properties(title),"
    "data(startRow,startColumn,rowData(values(userEnteredValue,effectiveValue))))"
)
SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"

TokenProvider = Callable[[], Awaitable[str]]


async def read_sheets_api(http: httpx.AsyncClient, sheet_id: str, token: TokenProvider) -> Grid:
    try:
        bearer = await token()
    except GoogleAuthError as e:  # its message never carries the key or a token
        raise LeagueSheetError(f"service-account auth failed: {e}") from None
    headers = {"Authorization": f"Bearer {bearer}"}
    params = {"includeGridData": "true", "fields": FIELDS}
    try:
        response = await http.get(API + sheet_id, params=params, headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL) as e:  # InvalidURL: e.g. a newline in the ID
        raise LeagueSheetError(f"Sheets API request failed ({type(e).__name__})") from None
    if response.status_code != httpx.codes.OK:
        raise LeagueSheetError(f"Sheets API answered HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise LeagueSheetError("Sheets API answered with something that isn't JSON") from None
    return grid_from_response(body)


def grid_from_response(body: Any) -> Grid:
    """The grid in a ``spreadsheets.get`` response (``includeGridData``)."""
    sheets = body.get("sheets") if isinstance(body, dict) else None
    if not isinstance(sheets, list):
        raise LeagueSheetError("Sheets API response has no sheets")
    tabs = []
    for i, sheet in enumerate(sheets):
        title = _get(_get(sheet, "properties", f"sheet {i}"), "title", f"sheet {i}")
        if not isinstance(title, str):
            raise LeagueSheetError(f"sheet {i}: title is not text")
        cells: dict[tuple[int, int], Cell] = {}
        for block in _list(sheet.get("data"), f"{title}: data"):
            if not isinstance(block, dict):
                raise LeagueSheetError(f"{title}: a data block is not an object")
            row0 = _int(block.get("startRow", 0), f"{title}: startRow")
            col0 = _int(block.get("startColumn", 0), f"{title}: startColumn")
            for r, row in enumerate(
                _list(block.get("rowData"), f"{title}: rowData"), start=row0 + 1
            ):
                if row is not None and not isinstance(row, dict):
                    raise LeagueSheetError(f"{title}: row {r} is not an object")
                values = _list((row or {}).get("values"), f"{title}: row {r} values")
                for c, raw in enumerate(values, start=col0 + 1):
                    cell = _cell(raw, f"{title}!{a1(r, c)}")
                    if cell.value is not None or cell.formula:
                        cells[(r, c)] = cell
        tabs.append(Tab(title, cells))
    return Grid(tuple(tabs))


def _list(value: Any, what: str) -> list[Any]:
    """A JSON list, or [] when absent (Google omits empty ones)."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise LeagueSheetError(f"{what} is not a list")
    return value


def _cell(raw: Any, where: str) -> Cell:
    if not isinstance(raw, dict):
        return Cell()
    entered = raw.get("userEnteredValue") or {}
    formula = entered.get("formulaValue") if isinstance(entered, dict) else None
    return Cell(
        _value(raw.get("effectiveValue"), where), formula if isinstance(formula, str) else None
    )


def _value(ev: Any, where: str) -> Value:
    """An ExtendedValue: number, string or bool; an error value reads as no value.
    A value of the wrong JSON type is an error (``where`` is a cell address)."""
    if not isinstance(ev, dict):
        return None
    for key, kinds in (
        ("numberValue", (int, float)),
        ("stringValue", (str,)),
        ("boolValue", (bool,)),
    ):
        if key in ev:
            v = ev[key]
            if not isinstance(v, kinds) or (key == "numberValue" and isinstance(v, bool)):
                raise LeagueSheetError(f"{where}: {key} of the wrong type")
            return v
    return None


def _get(obj: Any, key: str, what: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise LeagueSheetError(f"{what}: missing {key!r}")
    return obj[key]


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LeagueSheetError(f"{what}: expected a non-negative integer")
    return value
