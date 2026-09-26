"""LeagueSheetSource implementations: xlsx (file or upload bytes) and the fake."""

from pathlib import Path

import pytest

from fha.sources.league_sheet.models import LeagueSheetError, ParsedSheet
from fha.sources.league_sheet.parse import parse_sheet
from fha.sources.league_sheet.source import FakeLeagueSheet, LeagueSheetSource, XlsxLeagueSheet
from tests.unit.league_sheet.sheets import TeamTab, grid, roster
from tests.unit.league_sheet.xlsx_build import to_xlsx

SOURCE = grid(TeamTab("Aces", roster(2)), TeamTab("Bees", roster(3), payroll="sum_ref"))


async def test_an_uploaded_xlsx_parses() -> None:
    sheet: LeagueSheetSource = XlsxLeagueSheet(to_xlsx(SOURCE))
    assert await sheet.fetch() == parse_sheet(SOURCE)


async def test_a_downloaded_xlsx_parses(tmp_path: Path) -> None:
    path = tmp_path / "private" / "sheet.xlsx"
    path.parent.mkdir()
    path.write_bytes(to_xlsx(SOURCE))
    sheet = XlsxLeagueSheet(path)
    parsed = await sheet.fetch()
    assert [t.name for t in parsed.tabs] == ["Aces", "Bees"]
    assert str(path) not in repr(sheet)
    assert repr(XlsxLeagueSheet(b"x")) == "XlsxLeagueSheet(<bytes>)"


async def test_an_unreadable_upload_is_a_league_sheet_error() -> None:
    with pytest.raises(LeagueSheetError):
        await XlsxLeagueSheet(b"not a workbook").fetch()


async def test_the_fake_returns_its_sheet_and_counts_reads() -> None:
    parsed = ParsedSheet(1, None, ())
    fake: LeagueSheetSource = FakeLeagueSheet(parsed)
    assert await fake.fetch() is parsed
    assert await fake.fetch() is parsed
    assert isinstance(fake, FakeLeagueSheet)
    assert fake.fetches == 2
