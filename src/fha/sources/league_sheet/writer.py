"""``LeagueSheetWriter`` (SPEC §3, §4a "Writing"): cell values into the owner's tab.

- ``SheetsApiLeagueSheetWriter``: one ``spreadsheets.batchUpdate``, which Google
  applies all or nothing. Each cell is its own ``updateCells`` request with
  ``fields: userEnteredValue``, so a cell given no value is cleared and its
  formatting stays. Text is always a ``stringValue``, so a name can never become
  a formula. The token has the ``spreadsheets`` scope and is used for nothing
  else; reads keep ``spreadsheets.readonly``.
- ``FakeLeagueSheetWriter``: records what it was asked to write, for tests.

The caller plans and guards the edits (``domain.sheet_update.guard``); the
writer checks only what it needs to address a cell. As on the read path, an
error never carries the spreadsheet ID or Google's response body.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from fha.domain.sheet_update import CellEdit
from fha.sources.google_auth import GoogleAuthError, TokenProvider
from fha.sources.league_sheet.grid import column_number
from fha.sources.league_sheet.models import LeagueSheetError, ParsedTab
from fha.sources.league_sheet.sheets_api import API

WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


class LeagueSheetWriter(Protocol):
    async def write(self, tab: ParsedTab, edits: Sequence[CellEdit]) -> None:
        """Write ``edits`` to ``tab`` all at once (raises ``LeagueSheetError``)."""
        ...


def update_request(sheet_id: int, edits: Sequence[CellEdit]) -> dict[str, Any]:
    """The ``batchUpdate`` body: one ``updateCells`` per edit (GridRange indexes
    start at 0, and the end is exclusive)."""
    requests = []
    for e in edits:
        if e.column == "A" or not e.column.isalpha() or e.row < 1:
            raise LeagueSheetError(f"refusing to write {e.column}{e.row}")
        col = column_number(e.column)
        requests.append(
            {
                "updateCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": e.row - 1,
                        "endRowIndex": e.row,
                        "startColumnIndex": col - 1,
                        "endColumnIndex": col,
                    },
                    "rows": [{"values": [_cell(e)]}],
                    "fields": "userEnteredValue",
                }
            }
        )
    return {"requests": requests}


def _cell(e: CellEdit) -> dict[str, Any]:
    if e.new is None:
        return {}  # with the userEnteredValue mask: a clear
    if isinstance(e.new, bool) or not isinstance(e.new, str | int):
        raise LeagueSheetError(f"refusing to write a {type(e.new).__name__} to {e.column}{e.row}")
    kind = "stringValue" if isinstance(e.new, str) else "numberValue"
    return {"userEnteredValue": {kind: e.new}}


class SheetsApiLeagueSheetWriter:
    def __init__(self, http: httpx.AsyncClient, sheet_id: str, token: TokenProvider) -> None:
        self._http = http
        self._sheet_id = sheet_id
        self._token = token

    async def write(self, tab: ParsedTab, edits: Sequence[CellEdit]) -> None:
        if tab.sheet_id is None:
            raise LeagueSheetError("the tab has no sheet ID (was it read from an .xlsx?)")
        body = update_request(tab.sheet_id, edits)
        if not body["requests"]:
            return
        try:
            bearer = await self._token()
        except GoogleAuthError as e:
            raise LeagueSheetError(f"service-account auth failed: {e}") from None
        url = f"{API}{self._sheet_id}:batchUpdate"
        try:
            response = await self._http.post(
                url, json=body, headers={"Authorization": f"Bearer {bearer}"}
            )
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise LeagueSheetError(f"Sheets API write failed ({type(e).__name__})") from None
        if response.status_code != httpx.codes.OK:
            raise LeagueSheetError(f"Sheets API refused the write: HTTP {response.status_code}")

    def __repr__(self) -> str:
        return "SheetsApiLeagueSheetWriter(<sheet id hidden>)"


class FakeLeagueSheetWriter:
    def __init__(self, error: LeagueSheetError | None = None) -> None:
        self.writes: list[tuple[str, tuple[CellEdit, ...]]] = []
        self.error = error

    async def write(self, tab: ParsedTab, edits: Sequence[CellEdit]) -> None:
        if self.error is not None:
            raise self.error
        self.writes.append((tab.name, tuple(edits)))
