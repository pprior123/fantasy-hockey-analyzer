"""Update my tab (SPEC §4a "Writing", §7.5): the preview through the app."""

import asyncio
import re
from typing import Any

from itsdangerous import URLSafeTimedSerializer

from fha.domain.sheet_update import CellEdit, sheet_name
from fha.services.free_agents import set_aav_override
from fha.services.league_sheet import bind_tab, read_league_sheet
from fha.sources.league_sheet.grid import Cell, Grid, Tab, column_number
from fha.sources.league_sheet.models import LeagueSheetError, ParsedTab
from fha.sources.league_sheet.parse import parse_sheet
from fha.sources.league_sheet.source import FakeLeagueSheet
from fha.web.context import Services
from fha.web.routes.sheet_update import CONFIRM_SALT
from tests.unit.league_sheet import sheets as s
from tests.unit.web.helpers import SETTINGS, make_services
from tests.unit.web.test_screens import MINE, client, seed

FIRST = MINE.roster[0].player  # the player the tab is missing


def mine_tab(*, misspell: bool = False) -> s.TeamTab:
    """My tab without my first player, with a dropped player (Zed) and two blank rows."""
    counted, ir = [], []
    for e in MINE.roster[1:]:
        p = e.player
        name = f"Zqx {p.name}" if misspell and e is MINE.roster[1] else p.name
        row = s.Player(name, p.eligible_positions[0], p.nhl_team, 1_000_000)
        (ir if e.in_ir_slot else counted).append(row)
    counted.append(s.Player("Zed Nobody", "C", "TBL", 900_000))
    return s.TeamTab(
        "Mine", [*counted, None, None], below=[s.IRRow("IR+", r) for r in ir] + [s.IRRow("IR+")]
    )


def seeded(tab: s.TeamTab, *, priced: tuple[str, ...] = (FIRST.player_id,)) -> Services:
    svc = make_services()
    source = FakeLeagueSheet(parse_sheet(s.grid(tab)))
    svc.sheet = source

    async def go() -> None:
        await read_league_sheet(svc.repo, source, svc.clock)
        await bind_tab(svc.repo, "Mine", MINE.team_key)
        for pid in priced:
            await set_aav_override(svc.repo, pid, 2_000_000)

    asyncio.run(go())
    return svc


def page(svc: Services, query: str = "") -> str:
    response = client(svc).get("/admin/sheet/update" + query)
    assert response.status_code == 200
    return response.text


def test_the_preview_lists_the_changes_and_the_cells() -> None:
    svc = seeded(mine_tab())
    html = page(svc)
    name = sheet_name_of(FIRST)
    assert "Update my tab: Mine" in html
    assert "Drop <strong>Zed Nobody</strong>" in html
    assert f"Add <strong>{name}</strong>" in html
    assert f"<td>{name}</td>" in html  # the new name cell
    assert "<td>$2,000,000</td>" in html
    assert "Writing to the sheet is off, so apply these edits by hand." in html
    assert "Other columns in these rows are left as they are." in html
    assert svc.sheet is not None
    assert svc.sheet.fetches == 2  # type: ignore[attr-defined]  # seeding, then afresh


def sheet_name_of(player: Any) -> str:
    from fha.services.sheet_update import _member

    return sheet_name(_member(player, False, {}))


def test_an_unknown_cap_hit_is_a_refusal_with_its_reason() -> None:
    html = page(seeded(mine_tab(), priced=()))
    assert "Can't update the tab yet" in html
    assert f"{FIRST.name}&#39;s cap hit is unknown" in html
    assert "<h3>Cells</h3>" not in html


def test_a_review_row_can_be_ticked_as_dropped() -> None:
    second = MINE.roster[1].player
    svc = seeded(mine_tab(misspell=True), priced=(FIRST.player_id, second.player_id))
    html = page(svc)
    assert 'name="tick"' in html
    assert "awaits match review" in html
    row = html.split('name="tick" value="', 1)[1].split('"', 1)[0]
    ticked = page(svc, f"?tick={row}&tick=abc&tick=9999999")
    assert f'value="{row}" checked' in ticked
    assert "awaits match review" not in ticked
    assert f"Drop <strong>Zqx {second.name}</strong>" in ticked


def test_a_tab_that_matches_says_so() -> None:
    svc = make_services()
    seed(svc)
    html = page(svc)
    assert "Your tab matches your Yahoo roster. Nothing to change." in html


def test_without_a_sheet_the_page_says_so() -> None:
    html = page(make_services())
    assert "No league sheet has been read yet" in html


class BrokenSheet:
    async def fetch(self) -> Any:
        raise LeagueSheetError("Sheets API answered HTTP 403")


def test_a_failed_read_falls_back_to_the_stored_copy_with_a_note() -> None:
    svc = seeded(mine_tab())
    svc.sheet = BrokenSheet()
    html = page(svc)
    assert "couldn&#39;t be read just now (Sheets API answered HTTP 403)" in html
    assert "Drop <strong>Zed Nobody</strong>" in html  # from the stored copy


def test_admin_links_my_tab_to_the_update() -> None:
    svc = seeded(mine_tab())
    html = client(svc).get("/admin").text
    assert '<a class="btn secondary" href="/admin/sheet/update">Update my tab →</a>' in html
    assert html.count("/admin/sheet/update") == 1  # my tab only


def test_a_sheet_read_but_not_stored_uses_the_stored_copy(monkeypatch: Any) -> None:
    from fha.storage.repository import RepositoryError
    from fha.web.routes import sheet_update

    async def refuse(*args: Any) -> Any:
        raise RepositoryError("project x refused", detail="HTTP 503")

    svc = seeded(mine_tab())
    monkeypatch.setattr(sheet_update, "read_league_sheet", refuse)
    html = page(svc)
    assert "The sheet was read but couldn&#39;t be stored, so this is the stored copy." in html
    assert "project x" not in html


def test_without_yahoo_data_or_valid_settings_the_page_says_so(monkeypatch: Any) -> None:
    from fha.services.refresh import RefreshError
    from fha.services.settings import SettingsError
    from fha.web.routes import sheet_update

    for error, text in (
        (RefreshError("down"), "No Yahoo data yet (RefreshError)."),
        (SettingsError("bad"), "The stored rating settings are invalid (Admin)."),
    ):

        async def fail(*args: Any, error: Exception = error) -> Any:
            raise error

        monkeypatch.setattr(sheet_update, "page_data", fail)
        assert text in page(seeded(mine_tab()))


# ---------------------------------------------------------------- the confirm


SUM = re.compile(r"=SUM\(([A-Z]+)(\d+):([A-Z]+)(\d+)\)")


class LiveSheet:
    """Google, roughly: a grid the writer changes, SUMs recomputed, every tab but
    the owner's locked against the app."""

    def __init__(self, tab: s.TeamTab) -> None:
        self.tabs = list(s.grid(tab))
        self.writes = 0

    async def fetch(self) -> Any:
        tabs = tuple(Tab(t.title, t.cells, i, t.title != "Mine") for i, t in enumerate(self.tabs))
        return parse_sheet(Grid(tabs))

    async def write(self, tab: ParsedTab, edits: Any) -> None:
        self.writes += 1
        i = next(i for i, t in enumerate(self.tabs) if t.title == tab.name)
        cells = dict(self.tabs[i].cells)
        for e in edits:
            assert isinstance(e, CellEdit)
            key = (e.row, column_number(e.column))
            cells.pop(key, None)
            if e.new is not None:
                cells[key] = Cell(e.new)
        for key, cell in list(cells.items()):
            if cell.formula and (m := SUM.fullmatch(cell.formula)):
                col, r1, r2 = column_number(m[1]), int(m[2]), int(m[4])
                total = sum(
                    c.value
                    for (r, c_), c in cells.items()
                    if c_ == col and r1 <= r <= r2 and isinstance(c.value, int | float)
                )
                cells[key] = Cell(total, cell.formula)
        self.tabs[i] = Tab(self.tabs[i].title, cells)


def writable(tab: s.TeamTab) -> tuple[Services, LiveSheet]:
    svc = seeded(tab)
    sheet = LiveSheet(tab)
    svc.sheet = sheet
    svc.sheet_writer = sheet
    return svc, sheet


def token_in(html: str) -> str:
    return html.split('name="token" value="', 1)[1].split('"', 1)[0]


def test_writing_from_the_preview_and_reading_back() -> None:
    svc, sheet = writable(mine_tab())
    c = client(svc)
    html = c.get("/admin/sheet/update").text
    assert "Write these 3 cells to Mine" in html
    response = c.post("/admin/sheet/update", data={"token": token_in(html)}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/sheet/update?flash=")
    after = c.get(response.headers["location"]).text
    assert sheet.writes == 1
    assert "Written: 3 cells on Mine. PAYROLL is now $26,000,000, as expected." in after
    assert "as expected." in after
    assert "Your tab matches your Yahoo roster. Nothing to change." in after


def test_a_tab_changed_after_the_preview_is_not_written() -> None:
    svc, sheet = writable(mine_tab())
    c = client(svc)
    token = token_in(c.get("/admin/sheet/update").text)
    sheet.tabs[-1] = s.grid(mine_tab(misspell=True)).tabs[-1]  # someone edits the tab
    response = c.post("/admin/sheet/update", data={"token": token}, follow_redirects=True)
    assert sheet.writes == 0
    assert "changed since the preview" in response.text


def test_ticks_travel_with_the_confirm_and_come_back_on_an_error() -> None:
    second = MINE.roster[1].player
    svc = seeded(mine_tab(misspell=True), priced=(FIRST.player_id, second.player_id))
    sheet = LiveSheet(mine_tab(misspell=True))
    svc.sheet, svc.sheet_writer = sheet, sheet
    c = client(svc)
    html = c.get("/admin/sheet/update").text
    row = html.split('name="tick" value="', 1)[1].split('"', 1)[0]
    html = c.get(f"/admin/sheet/update?tick={row}").text
    assert f'<input type="hidden" name="tick" value="{row}">' in html
    sheet.tabs[-1] = s.grid(mine_tab()).tabs[-1]  # changed meanwhile
    response = c.post(
        "/admin/sheet/update",
        data={"token": token_in(html), "tick": [row, "x"]},
        follow_redirects=False,
    )
    assert response.headers["location"].endswith(f"&tick={row}")


def test_a_bad_or_old_token_writes_nothing() -> None:
    svc, sheet = writable(mine_tab())
    c = client(svc)
    for token in (
        "forged",
        URLSafeTimedSerializer(SETTINGS.session_secret, salt=CONFIRM_SALT).dumps(7),
    ):
        response = c.post("/admin/sheet/update", data={"token": token}, follow_redirects=True)
        assert "That preview is too old: check it again." in response.text
    assert sheet.writes == 0


def test_without_a_writer_there_is_no_button_and_no_confirm() -> None:
    svc = seeded(mine_tab())
    c = client(svc)
    html = c.get("/admin/sheet/update").text
    assert 'name="token"' not in html
    assert c.post("/admin/sheet/update", data={"token": "x"}).status_code == 404


def test_without_yahoo_data_the_confirm_writes_nothing(monkeypatch: Any) -> None:
    from fha.services.refresh import RefreshError
    from fha.web.routes import sheet_update

    svc, sheet = writable(mine_tab())
    c = client(svc)
    token = token_in(c.get("/admin/sheet/update").text)

    async def fail(*args: Any) -> Any:
        raise RefreshError("down")

    monkeypatch.setattr(sheet_update, "page_data", fail)
    response = c.post("/admin/sheet/update", data={"token": token}, follow_redirects=True)
    assert "Nothing was written: no Yahoo data (RefreshError)." in response.text
    assert sheet.writes == 0
