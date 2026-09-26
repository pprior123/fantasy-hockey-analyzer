"""The Google Sheets API grid reader, against mocked HTTP (no network)."""

from typing import Any

import httpx
import pytest

from fha.sources.league_sheet.grid import Cell, Grid, Tab
from fha.sources.league_sheet.models import LeagueSheetError
from fha.sources.league_sheet.parse import parse_sheet
from fha.sources.league_sheet.sheets_api import FIELDS, grid_from_response, read_sheets_api
from fha.sources.league_sheet.source import SheetsApiLeagueSheet
from tests.unit.league_sheet.sheets import IRRow, Player, TeamTab, grid, notes_tab, roster

SHEET_ID = "1SecretSheetIdDoNotLeak"


def extended(value: object) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int | float):
        return {"numberValue": value}
    return {"stringValue": value}


def api_response(g: Grid) -> dict[str, Any]:
    """A ``spreadsheets.get`` (includeGridData) body for a grid, as Google sends it:
    rows as lists of cells from column A, empty cells as {}, trailing ones left out."""
    sheets = []
    for tab in g:
        row_data = []
        for r in range(1, tab.max_row + 1):
            width = max((c for (row, c) in tab.cells if row == r), default=0)
            values: list[dict[str, Any]] = []
            for c in range(1, width + 1):
                cell = tab.cell(r, c)
                raw: dict[str, Any] = {}
                if cell.value is not None:
                    raw["effectiveValue"] = extended(cell.value)
                    raw["userEnteredValue"] = extended(cell.value)
                if cell.formula:
                    raw["userEnteredValue"] = {"formulaValue": cell.formula}
                values.append(raw)
            row_data.append({"values": values} if values else {})
        sheets.append({"properties": {"title": tab.title}, "data": [{"rowData": row_data}]})
    return {"sheets": sheets}


class Google:
    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status, self.body = status, body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.body, bytes):
            return httpx.Response(self.status, content=self.body)
        return httpx.Response(self.status, json=self.body)


async def token() -> str:
    return "service-account-token"


def http(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_one_request_with_grid_data_values_and_formulas() -> None:
    google = Google(body=api_response(grid(TeamTab("Aces", roster(2)))))
    await read_sheets_api(http(google), SHEET_ID, token)
    [request] = google.requests
    assert request.method == "GET"
    assert request.url.host == "sheets.googleapis.com"
    assert request.url.path == f"/v4/spreadsheets/{SHEET_ID}"
    assert request.url.params["includeGridData"] == "true"
    assert request.url.params["fields"] == FIELDS
    assert "userEnteredValue" in FIELDS
    assert "effectiveValue" in FIELDS
    assert request.headers["Authorization"] == "Bearer service-account-token"


async def test_every_variant_parses_the_same_from_the_api_as_from_the_grid() -> None:
    source = grid(
        TeamTab("Aces", roster(3), below=[IRRow("IR", Player("Ike Injured")), IRRow("IR+")]),
        TeamTab("Bees", roster(4), start=6, header="in_range", payroll="sum_ref"),
        TeamTab("Cats", [*roster(2), None, Player("Ben", salary="???")], payroll="cell_ref"),
        notes_tab(),
    )
    sheet = SheetsApiLeagueSheet(http(Google(body=api_response(source))), SHEET_ID, token)
    parsed = await sheet.fetch()
    assert parsed == parse_sheet(source)
    assert [t.status for t in parsed.tabs] == ["ok"] * 3


def test_cells_offsets_and_value_kinds() -> None:
    body = {
        "sheets": [
            {
                "properties": {"title": "T"},
                "data": [
                    {
                        "startRow": 2,
                        "startColumn": 1,
                        "rowData": [
                            {
                                "values": [
                                    {
                                        "userEnteredValue": {"formulaValue": "=SUM(B3:B4)"},
                                        "effectiveValue": {"numberValue": 7},
                                    },
                                    {"effectiveValue": {"stringValue": "x"}},
                                    {"effectiveValue": {"boolValue": False}},
                                    {"effectiveValue": {"errorValue": {"type": "REF"}}},
                                    {},
                                    "junk",
                                ]
                            },
                            {},
                            None,
                            {"values": None},
                        ],
                    },
                    {"rowData": None},
                ],
            },
            {"properties": {"title": "Empty"}},
        ]
    }
    g = grid_from_response(body)
    assert g.tabs == (
        Tab("T", {(3, 2): Cell(7, "=SUM(B3:B4)"), (3, 3): Cell("x"), (3, 4): Cell(False)}),
        Tab("Empty", {}),
    )
    assert g.tabs[0].cells == {
        (3, 2): Cell(7, "=SUM(B3:B4)"),
        (3, 3): Cell("x"),
        (3, 4): Cell(False),
    }


def test_a_formula_with_an_error_value_keeps_its_formula() -> None:
    body = {
        "sheets": [
            {
                "properties": {"title": "T"},
                "data": [
                    {
                        "rowData": [
                            {
                                "values": [
                                    {
                                        "userEnteredValue": {"formulaValue": "=1/0"},
                                        "effectiveValue": {
                                            "errorValue": {"type": "DIVIDE_BY_ZERO"}
                                        },
                                    },
                                ]
                            }
                        ]
                    }
                ],
            }
        ]
    }
    assert grid_from_response(body).tabs[0].cells == {(1, 1): Cell(None, "=1/0")}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({}, "no sheets"),
        ([], "no sheets"),
        ({"sheets": {}}, "no sheets"),
        ({"sheets": [{}]}, "sheet 0: missing 'properties'"),
        ({"sheets": [{"properties": {}}]}, "sheet 0: missing 'title'"),
        ({"sheets": [{"properties": {"title": 5}}]}, "sheet 0: title is not text"),
        (
            {"sheets": [{"properties": {"title": "T"}, "data": [{"startRow": -1}]}]},
            "T: startRow: expected a non-negative integer",
        ),
        (
            {"sheets": [{"properties": {"title": "T"}, "data": [{"startColumn": True}]}]},
            "T: startColumn: expected a non-negative integer",
        ),
    ],
)
def test_malformed_responses_are_errors(body: Any, message: str) -> None:
    with pytest.raises(LeagueSheetError, match=message):
        grid_from_response(body)


@pytest.mark.parametrize(
    "value",
    [{"numberValue": "7"}, {"numberValue": True}, {"stringValue": 7}, {"boolValue": "yes"}],
)
def test_a_value_of_the_wrong_type_names_its_cell(value: dict[str, Any]) -> None:
    row = {"values": [{}, {"effectiveValue": value}]}
    body = {"sheets": [{"properties": {"title": "T"}, "data": [{"rowData": [{}, row]}]}]}
    key = next(iter(value))
    with pytest.raises(LeagueSheetError, match=f"^T!B2: {key} of the wrong type$"):
        grid_from_response(body)


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_an_http_error_names_the_status_but_never_the_sheet_id(status: int) -> None:
    google = Google(status, {"error": {"message": f"Requested entity {SHEET_ID} not found"}})
    with pytest.raises(LeagueSheetError) as caught:
        await read_sheets_api(http(google), SHEET_ID, token)
    assert str(caught.value) == f"Sheets API answered HTTP {status}"


async def test_a_transport_error_is_replaced_not_chained() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with pytest.raises(LeagueSheetError) as caught:
        await read_sheets_api(http(fail), SHEET_ID, token)
    assert str(caught.value) == "Sheets API request failed (ConnectError)"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


async def test_a_malformed_sheet_id_is_an_error_that_hides_it() -> None:
    bad_id = SHEET_ID + "\n"  # e.g. a trailing newline in the env var
    with pytest.raises(LeagueSheetError) as caught:
        await read_sheets_api(http(Google(200, {})), bad_id, token)
    assert str(caught.value) == "Sheets API request failed (InvalidURL)"
    assert SHEET_ID not in str(caught.value)


async def test_a_non_json_answer_is_an_error() -> None:
    with pytest.raises(LeagueSheetError, match="isn't JSON"):
        await read_sheets_api(http(Google(body=b"<html>")), SHEET_ID, token)


def test_the_source_never_shows_the_sheet_id() -> None:
    sheet = SheetsApiLeagueSheet(http(Google()), SHEET_ID, token)
    assert SHEET_ID not in repr(sheet)
