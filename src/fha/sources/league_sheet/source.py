"""``LeagueSheetSource`` (SPEC §3): the league sheet, parsed, from wherever it lives.

- ``SheetsApiLeagueSheet``: the Google Sheets API (production, from M5).
- ``XlsxLeagueSheet``: a downloaded file (dev, ``LEAGUE_SHEET_XLSX``) or the
  bytes of an Admin upload (any environment, the fallback).
- ``FakeLeagueSheet``: a fixed result, for tests.

Every one returns only the parsed result. The raw grid, with the managers'
contact details, is dropped as soon as it has been parsed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

import httpx

from fha.sources.league_sheet.models import ParsedSheet
from fha.sources.league_sheet.parse import parse_sheet
from fha.sources.league_sheet.sheets_api import TokenProvider, read_sheets_api
from fha.sources.league_sheet.xlsx import read_xlsx


class LeagueSheetSource(Protocol):
    async def fetch(self) -> ParsedSheet:
        """Read and parse the whole sheet (raises ``LeagueSheetError`` if unreadable)."""
        ...


class XlsxLeagueSheet:
    def __init__(self, source: bytes | str | Path) -> None:
        self._source = source

    async def fetch(self) -> ParsedSheet:
        # openpyxl is synchronous: keep the event loop free while it works.
        return await asyncio.to_thread(lambda: parse_sheet(read_xlsx(self._source)))

    def __repr__(self) -> str:
        return (
            "XlsxLeagueSheet(<bytes>)" if isinstance(self._source, bytes) else "XlsxLeagueSheet()"
        )


class SheetsApiLeagueSheet:
    def __init__(self, http: httpx.AsyncClient, sheet_id: str, token: TokenProvider) -> None:
        self._http = http
        self._sheet_id = sheet_id
        self._token = token

    async def fetch(self) -> ParsedSheet:
        return parse_sheet(await read_sheets_api(self._http, self._sheet_id, self._token))

    def __repr__(self) -> str:
        return "SheetsApiLeagueSheet(<sheet id hidden>)"


class FakeLeagueSheet:
    def __init__(self, sheet: ParsedSheet) -> None:
        self.sheet = sheet
        self.fetches = 0

    async def fetch(self) -> ParsedSheet:
        self.fetches += 1
        return self.sheet
