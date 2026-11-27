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

``NotSentError`` means no batch left the process. ``RejectedWriteError``
means Google answered with a 4xx. Other failures after the POST began are
``UncertainWriteError`` and require a read of the tab to check what landed.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

import httpx

from fha.domain.sheet_update import CellEdit
from fha.sources.google_auth import GoogleAuthError, TokenProvider
from fha.sources.league_sheet.grid import column_number
from fha.sources.league_sheet.models import LeagueSheetError, ParsedTab
from fha.sources.league_sheet.sheets_api import API

WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
COLUMN = re.compile(r"[B-Z]|[A-Z]{2,3}")  # never column A (the IR labels)


# The request never reached Google: nothing can have been written.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)
BeforePost = Callable[[], Awaitable[str | None]]


class UncertainWriteError(LeagueSheetError):
    """The request was sent but its answer was lost or a server error: the batch
    may or may not have been applied."""


class NotSentError(LeagueSheetError):
    """The batch could not be sent."""

    def __init__(self, message: str, *, code: str = "not_sent") -> None:
        super().__init__(message)
        self.code = code


class RejectedWriteError(LeagueSheetError):
    """Google answered with a client error and did not apply the batch."""


class LeagueSheetWriter(Protocol):
    async def write(
        self,
        tab: ParsedTab,
        edits: Sequence[CellEdit],
        *,
        deadline: float | None = None,
        before_post: BeforePost | None = None,
        on_send: Callable[[], None] | None = None,
    ) -> None:
        """Write the batch before the monotonic deadline and signal when posting begins."""
        ...


async def _check_before_post(
    before_post: BeforePost | None, deadline: float | None, monotonic: Callable[[], float]
) -> None:
    if before_post is not None:
        code = await before_post()
        if code is not None:
            raise NotSentError("the final automatic check refused", code=code)
    if deadline is not None and monotonic() >= deadline:
        raise NotSentError("the write deadline expired", code="out_of_time")


def update_request(sheet_id: int, edits: Sequence[CellEdit]) -> dict[str, Any]:
    """The ``batchUpdate`` body: one ``updateCells`` per edit (GridRange indexes
    start at 0, and the end is exclusive)."""
    requests = []
    for e in edits:
        if not COLUMN.fullmatch(e.column) or e.row < 1:
            raise NotSentError(f"refusing to write {e.column}{e.row}")
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
        raise NotSentError(f"refusing to write a {type(e.new).__name__} to {e.column}{e.row}")
    kind = "stringValue" if isinstance(e.new, str) else "numberValue"
    return {"userEnteredValue": {kind: e.new}}


def _protected(response: httpx.Response) -> bool:
    """Google's 400 for a protected cell. Only this test reads the body; it is never
    shown, so nothing in it (ranges, the spreadsheet ID) can reach a message."""
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return False
    return isinstance(message, str) and "protected cell" in message


class SheetsApiLeagueSheetWriter:
    def __init__(
        self,
        http: httpx.AsyncClient,
        sheet_id: str,
        token: TokenProvider,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._sheet_id = sheet_id
        self._token = token
        self._monotonic = monotonic

    async def write(
        self,
        tab: ParsedTab,
        edits: Sequence[CellEdit],
        *,
        deadline: float | None = None,
        before_post: BeforePost | None = None,
        on_send: Callable[[], None] | None = None,
    ) -> None:
        if tab.sheet_id is None:
            raise NotSentError("the tab has no sheet ID (was it read from an .xlsx?)")
        body = update_request(tab.sheet_id, edits)
        if not body["requests"]:
            return
        try:
            bearer = await self._token()
        except GoogleAuthError as e:
            raise NotSentError(f"service-account auth failed: {e}") from None
        url = f"{API}{self._sheet_id}:batchUpdate"
        await _check_before_post(before_post, deadline, self._monotonic)
        if on_send is not None:
            on_send()
        try:
            response = await self._http.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {bearer}"},
                follow_redirects=False,
            )
        except (*NOT_SENT, httpx.InvalidURL) as e:
            raise NotSentError(f"Sheets API write failed ({type(e).__name__})") from None
        except httpx.HTTPError as e:
            raise UncertainWriteError(f"Sheets API write: no answer ({type(e).__name__})") from None
        status = response.status_code
        if response.is_client_error:
            why = " (a cell to change is protected against the app)" if _protected(response) else ""
            raise RejectedWriteError(f"Sheets API refused the write: HTTP {status}{why}")
        if status != httpx.codes.OK:
            raise UncertainWriteError(f"Sheets API write: HTTP {status}, outcome unknown")

    def __repr__(self) -> str:
        return "SheetsApiLeagueSheetWriter(<sheet id hidden>)"


class FakeLeagueSheetWriter:
    def __init__(self, error: LeagueSheetError | None = None) -> None:
        self.writes: list[tuple[str, tuple[CellEdit, ...]]] = []
        self.error = error

    async def write(
        self,
        tab: ParsedTab,
        edits: Sequence[CellEdit],
        *,
        deadline: float | None = None,
        before_post: BeforePost | None = None,
        on_send: Callable[[], None] | None = None,
    ) -> None:
        await _check_before_post(before_post, deadline, time.monotonic)
        if on_send is not None:
            on_send()
        if self.error is not None:
            raise self.error
        self.writes.append((tab.name, tuple(edits)))
