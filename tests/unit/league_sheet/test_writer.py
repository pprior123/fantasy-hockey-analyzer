"""The league-sheet writer (SPEC §4a "Writing"): the exact batchUpdate body, and
errors that never carry the spreadsheet ID. Google is mocked; no network."""

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from fha.domain.sheet_update import CellEdit
from fha.sources.google_auth import GoogleAuthError
from fha.sources.league_sheet.models import LeagueSheetError, ParsedTab
from fha.sources.league_sheet.writer import (
    WRITE_SCOPE,
    FakeLeagueSheetWriter,
    SheetsApiLeagueSheetWriter,
    update_request,
)

SHEET_ID = "sheet-id-that-must-stay-private"
TAB = ParsedTab("Mine", "ok", None, 1, 1, "F", sheet_id=42)
EDITS = (
    CellEdit(9, "C", "Zed Nobody", "Oettinger, Jake"),
    CellEdit(9, "F", 900_000, 8_000_000),
    CellEdit(10, "AB", "x", None),
)


def test_the_body_addresses_each_cell_from_zero_end_exclusive() -> None:
    body = update_request(42, EDITS)
    assert body == {
        "requests": [
            {
                "updateCells": {
                    "range": {
                        "sheetId": 42,
                        "startRowIndex": 8,
                        "endRowIndex": 9,
                        "startColumnIndex": 2,
                        "endColumnIndex": 3,
                    },
                    "rows": [
                        {"values": [{"userEnteredValue": {"stringValue": "Oettinger, Jake"}}]}
                    ],
                    "fields": "userEnteredValue",
                }
            },
            {
                "updateCells": {
                    "range": {
                        "sheetId": 42,
                        "startRowIndex": 8,
                        "endRowIndex": 9,
                        "startColumnIndex": 5,
                        "endColumnIndex": 6,
                    },
                    "rows": [{"values": [{"userEnteredValue": {"numberValue": 8_000_000}}]}],
                    "fields": "userEnteredValue",
                }
            },
            {
                "updateCells": {
                    "range": {
                        "sheetId": 42,
                        "startRowIndex": 9,
                        "endRowIndex": 10,
                        "startColumnIndex": 27,
                        "endColumnIndex": 28,
                    },
                    "rows": [{"values": [{}]}],  # cleared by the field mask
                    "fields": "userEnteredValue",
                }
            },
        ]
    }


def test_a_name_that_looks_like_a_formula_is_still_text() -> None:
    [request] = update_request(1, [CellEdit(7, "C", None, "=IMPORTXML(1)")])["requests"]
    value = request["updateCells"]["rows"][0]["values"][0]
    assert value == {"userEnteredValue": {"stringValue": "=IMPORTXML(1)"}}


@pytest.mark.parametrize(
    "edit",
    [
        CellEdit(7, "A", None, "IR"),
        CellEdit(0, "C", None, "x"),
        CellEdit(7, "C1", None, "x"),
        CellEdit(7, "Ä", None, "x"),  # a letter, but not a column
        CellEdit(7, "c", None, "x"),
        CellEdit(7, "ABCD", None, "x"),
        CellEdit(7, "C", None, True),
        CellEdit(7, "C", None, 1.5),  # type: ignore[arg-type]
    ],
)
def test_cells_it_cannot_address_or_values_it_cannot_write_are_refused(edit: CellEdit) -> None:
    with pytest.raises(LeagueSheetError, match="refusing to write"):
        update_request(1, [edit])


class Google:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={"replies": [], "secret": SHEET_ID})


async def token() -> str:
    return "write-token"


def writer(handler: Any, tok: Any = token) -> SheetsApiLeagueSheetWriter:
    return SheetsApiLeagueSheetWriter(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), SHEET_ID, tok
    )


async def test_one_batch_update_with_the_write_token() -> None:
    google = Google()
    await writer(google).write(TAB, EDITS)
    [request] = google.requests
    assert request.method == "POST"
    assert (
        str(request.url) == f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate"
    )
    assert request.headers["Authorization"] == "Bearer write-token"
    assert json.loads(request.content) == update_request(42, EDITS)
    assert WRITE_SCOPE == "https://www.googleapis.com/auth/spreadsheets"


async def test_nothing_to_write_sends_nothing() -> None:
    google = Google()
    await writer(google).write(TAB, ())
    assert google.requests == []


async def test_a_tab_without_a_sheet_id_is_refused_before_any_request() -> None:
    google = Google()
    with pytest.raises(LeagueSheetError, match="no sheet ID"):
        await writer(google).write(replace(TAB, sheet_id=None), EDITS)
    assert google.requests == []


async def test_a_refused_write_names_the_status_only() -> None:
    with pytest.raises(LeagueSheetError) as info:
        await writer(Google(403)).write(TAB, EDITS)
    assert str(info.value) == "Sheets API refused the write: HTTP 403"


async def test_transport_and_auth_failures_never_carry_the_sheet_id() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with pytest.raises(LeagueSheetError) as info:
        await writer(down).write(TAB, EDITS)
    assert str(info.value) == "Sheets API write failed (ConnectError)"
    assert info.value.__cause__ is None

    async def refused() -> str:
        raise GoogleAuthError("Google refused the token request: HTTP 400 (invalid_grant)")

    with pytest.raises(LeagueSheetError, match=r"service-account auth failed: .*invalid_grant"):
        await writer(Google(), refused).write(TAB, EDITS)
    assert SHEET_ID not in repr(writer(Google()))


async def test_the_fake_records_writes_or_fails_as_told() -> None:
    fake = FakeLeagueSheetWriter()
    await fake.write(TAB, EDITS)
    assert fake.writes == [("Mine", EDITS)]
    failing = FakeLeagueSheetWriter(LeagueSheetError("boom"))
    with pytest.raises(LeagueSheetError, match="boom"):
        await failing.write(TAB, EDITS)
