"""The Admin screen (SPEC §7.5, §4a, §6): every section and action, through the app."""

import re
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from fha.services.refresh import RefreshService
from fha.services.settings import COLLECTION as SETTINGS_COLLECTION
from fha.services.settings import RATING
from fha.sources.league_sheet.models import LeagueSheetError, ParsedSheet
from fha.sources.league_sheet.parse import parse_sheet
from fha.sources.league_sheet.source import FakeLeagueSheet
from fha.sources.yahoo.client import YahooHTTPError
from fha.sources.yahoo.demo import demo_snapshot
from fha.sources.yahoo.models import LeagueSnapshot, Team
from fha.web.routes import admin
from fha.web.routes.admin import (
    FLASH_SALT,
    InputError,
    _ago,
    _sheet_source,
    parse_dollars,
    parse_percent,
    percent_text,
)
from tests.unit.league_sheet import sheets as s
from tests.unit.league_sheet.xlsx_build import to_xlsx
from tests.unit.web.helpers import SETTINGS, logged_in, make_app, make_services

DEMO = demo_snapshot()
TEAM1, TEAM2 = DEMO.teams[0], DEMO.teams[1]
FREE = [p for p in DEMO.available if not p.is_goalie]


def sheet_tab(title: str, team: Team, extra: list[s.Player] | None = None) -> s.TeamTab:
    """A tab listing a demo team's first healthy skaters (rows that match its roster)."""
    players = [
        s.Player(e.player.name, e.player.display_position.split(",")[0], "", 1_000_000)
        for e in team.roster[:8]
        if not e.player.is_goalie and e.selected_position not in ("IR", "IR+")
    ]
    return s.TeamTab(title, [*players, *(extra or [])])


GRID = s.grid(
    sheet_tab("Pinecone", TEAM1, [s.Player("Zed Nobody", "C", "TBL", 900_000)]),
    sheet_tab("Maple", TEAM2),
)
SHEET_BYTES = to_xlsx(GRID)
SIGNER = URLSafeTimedSerializer(SETTINGS.session_secret, salt=FLASH_SALT)


def client(services: Any = None) -> TestClient:
    return logged_in(make_app(services))


def flash_of(response: Any) -> dict[str, str]:
    assert response.status_code == 303
    token = parse_qs(urlsplit(response.headers["location"]).query)["flash"][0]
    value: dict[str, str] = SIGNER.loads(token)
    return value


def post(c: TestClient, path: str, **kw: Any) -> dict[str, str]:
    return flash_of(c.post(path, follow_redirects=False, **kw))


def upload_sheet(c: TestClient, data: bytes = SHEET_BYTES) -> dict[str, str]:
    return post(c, "/admin/sheet/upload", files={"file": ("sheet.xlsx", data)})


def upload_csv(c: TestClient, *lines: str) -> dict[str, str]:
    data = ("\n".join(("Player,Pos,Cap Hit,Team", *lines)) + "\n").encode()
    return post(c, "/admin/csv", files={"file": ("fa.csv", data)})


def last_first(name: str) -> str:
    first, last = name.split(" ", 1)
    return f'"{last}, {first}"'


def csv_row(name: str, pos: str, cap: str, team: str = "") -> str:
    return f'{last_first(name)},{pos},"{cap}",{team}'


# ---------------------------------------------------------------- the page


def test_admin_needs_a_login() -> None:
    with TestClient(make_app()) as anon:
        response = anon.get("/admin", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login?next=%2Fadmin")


def test_the_page_shows_every_section_before_anything_is_set_up() -> None:
    html = client().get("/admin").text
    sections = ("sheet", "bindings", "discrepancies", "csv", "review", "aav", "refresh")
    for anchor in (*sections, "settings", "config"):
        assert f'id="{anchor}"' in html
    assert "Not read yet." in html
    assert "none configured: set LEAGUE_SHEET_ID (live) or LEAGUE_SHEET_XLSX" in html
    assert "Read the sheet now" not in html  # no sheet source configured
    assert "Demo league: on" in html
    assert "Storage: InMemoryRepository" in html
    assert "Top-N per-82 rates (default 10)" in html
    assert "In use: Workbook (top-N totals over top-N GP), top 20 (the method" in html
    assert "GP floor 2%." in html
    assert '<form method="post" action="/refresh">' in html
    assert '<input type="hidden" name="next" value="/admin">' in html
    assert "Nothing to review." in html
    assert "No bound tabs yet." in html
    assert "Read the sheet first." in html


# ---------------------------------------------------------------- the league sheet


def test_uploading_a_sheet_reads_and_stores_it_without_any_contact_detail() -> None:
    c = client()
    assert upload_sheet(c) == {"kind": "ok", "text": "Read 2 team tabs: 2 ok."}
    html = c.get("/admin").text
    assert "Pinecone" in html
    assert "Maple" in html
    assert "Last read just now." in html
    assert "$119,600,000" in html
    for secret in s.CONTACT:
        assert secret not in html
    post(c, "/admin/bind", data={"tab": "Pinecone", "team_key": TEAM1.team_key})
    html = c.get("/admin").text
    assert "needs a look" in html
    for secret in s.CONTACT:
        assert secret not in html


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "Upload: choose a file first."),
        (
            b"not a spreadsheet",
            "That file isn't a league sheet we can read: not a readable .xlsx file (BadZipFile)",
        ),
    ],
)
def test_a_bad_upload_is_refused_with_a_message(data: bytes, message: str) -> None:
    assert upload_sheet(client(), data) == {"kind": "error", "text": message}


def test_an_oversized_upload_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin, "MAX_SHEET_BYTES", 1024 * 1024)
    result = upload_sheet(client(), b"x" * (1024 * 1024 + 1))
    assert result == {"kind": "error", "text": "Upload: that file is over 1 MB."}


def test_reading_the_configured_sheet() -> None:
    services = replace(make_services(), sheet=FakeLeagueSheet(parse_sheet(GRID)))
    c = client(services)
    assert "Read the sheet now" in c.get("/admin").text
    assert post(c, "/admin/sheet/read") == {"kind": "ok", "text": "Read 2 team tabs: 2 ok."}


def test_reading_without_a_sheet_or_a_failing_one() -> None:
    assert post(client(), "/admin/sheet/read") == {
        "kind": "error",
        "text": "No league sheet is configured.",
    }

    class Broken:
        async def fetch(self) -> ParsedSheet:
            raise LeagueSheetError("Sheets API answered HTTP 403")

    c = client(replace(make_services(), sheet=Broken()))
    assert post(c, "/admin/sheet/read") == {
        "kind": "error",
        "text": "The sheet couldn't be read: Sheets API answered HTTP 403",
    }


def test_an_unrecognized_tab_shows_its_reason() -> None:
    broken = s.TeamTab("Broken", [s.Player("Ada Big")], payroll_formula="=F7+F8")
    c = client()
    assert upload_sheet(c, to_xlsx(s.grid(broken))) == {
        "kind": "ok",
        "text": "Read 1 team tabs: 0 ok, 1 unrecognized.",
    }
    assert "unrecognized: PAYROLL formula" in c.get("/admin").text


# ---------------------------------------------------------------- bindings and discrepancies


def test_tabs_are_suggested_and_bound() -> None:
    c = client()
    upload_sheet(c)
    html = c.get("/admin").text
    suggested = rf'<option value="{re.escape(TEAM1.team_key)}" selected>{TEAM1.name} \(suggested\)'
    assert re.search(suggested, html)
    assert post(c, "/admin/bind", data={"tab": "Pinecone", "team_key": TEAM1.team_key}) == {
        "kind": "ok",
        "text": "Pinecone is now bound.",
    }
    assert f"bound to {TEAM1.name}" in c.get("/admin").text
    taken = post(c, "/admin/bind", data={"tab": "Maple", "team_key": TEAM1.team_key})
    assert taken == {
        "kind": "error",
        "text": "Maple: that team is already bound to tab 'Pinecone'.",
    }
    assert post(c, "/admin/bind", data={"tab": "Pinecone", "team_key": ""}) == {
        "kind": "ok",
        "text": "Pinecone is unbound.",
    }


def test_the_discrepancy_report_and_row_review() -> None:
    c = client()
    upload_sheet(c)
    post(c, "/admin/bind", data={"tab": "Pinecone", "team_key": TEAM1.team_key})
    html = c.get("/admin").text
    assert "<strong>Zed Nobody</strong>" in html
    assert "not on the Yahoo roster" in html
    assert "missing from the tab" in html
    assert "(differs)" in html
    row_key = "zed nobody|F"
    assert f'name="key" value="{row_key}"' in html
    pid = TEAM1.roster[-1].player.player_id
    form = {"tab": "Pinecone", "key": row_key, "player_id": pid}
    assert post(c, "/admin/sheet/confirm", data=form) == {
        "kind": "ok",
        "text": "Pinecone: row matched.",
    }
    form = {"tab": "Maple", "key": "x|F", "player_id": pid}
    assert post(c, "/admin/sheet/confirm", data=form) == {
        "kind": "error",
        "text": "Maple: tab 'Maple' isn't bound to a team yet.",
    }


def test_a_bound_unrecognized_tab_and_cap_and_team_notes() -> None:
    stale = s.TeamTab("Stale", [s.Player("Ada Big", team="Hamilton")], cap_value=95_000_000)
    broken = s.TeamTab("Broken", [s.Player("Ada Big")], payroll_formula="=F7+F8")
    c = client()
    upload_sheet(c, to_xlsx(s.grid(stale, broken)))
    post(c, "/admin/bind", data={"tab": "Stale", "team_key": TEAM1.team_key})
    post(c, "/admin/bind", data={"tab": "Broken", "team_key": TEAM2.team_key})
    html = c.get("/admin").text
    assert "differs from the summary cap ($119,600,000)" in html
    assert "Unknown NHL teams: Hamilton." in html
    assert "Unrecognized tab: PAYROLL formula" in html


# ---------------------------------------------------------------- the salary CSV and review


def test_importing_the_csv_then_again_is_no_change() -> None:
    c = client()
    rows = [csv_row(p.name, p.display_position[0], "$1,000,000", p.nhl_team) for p in FREE[:3]]
    assert upload_csv(c, *rows) == {
        "kind": "ok",
        "text": "Imported 3 rows: 3 bound now, 0 already bound, 0 to review.",
    }
    assert upload_csv(c, *rows) == {
        "kind": "ok",
        "text": "No changes: the same 3 rows as last time.",
    }


def test_conflicts_and_unknown_teams_are_reported() -> None:
    p = FREE[0]
    c = client()
    text = upload_csv(
        c,
        csv_row(p.name, "C", "$1", p.nhl_team),
        csv_row(p.name, "C", "$2", p.nhl_team),
        csv_row("Some Else", "C", "$3", "Hamilton"),
    )["text"]
    assert "Not imported (two cap hits for one player):" in text
    assert "Unknown teams: Hamilton." in text


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "Upload: choose a file first."),
        (b"Player,Pos\nA B,C\n", "The CSV wasn't imported: line 1: missing column: Cap Hit"),
    ],
)
def test_a_bad_csv_is_refused_with_its_message(data: bytes, message: str) -> None:
    result = post(client(), "/admin/csv", files={"file": ("fa.csv", data)})
    assert result["kind"] == "error"
    assert result["text"].startswith(message)


def test_an_oversized_csv_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin, "MAX_CSV_BYTES", 1024 * 1024)
    result = post(client(), "/admin/csv", files={"file": ("fa.csv", b"x" * (1024 * 1024 + 1))})
    assert result == {"kind": "error", "text": "Upload: that file is over 1 MB."}


def test_review_then_confirm_with_an_alias() -> None:
    target = FREE[0]
    typo = target.name[:-1] + "q"  # one letter off: a fuzzy candidate, never a match
    c = client()
    upload_csv(c, csv_row(typo, target.display_position[0], "$2,000,000"))
    review = c.get("/admin").text.split('id="review"')[1].split("</section>")[0]
    assert f"<strong>{last_first(typo)[1:-1]}</strong>" in review
    assert target.name in review
    key = re.search(r'name="key" value="([^"]+)"', review)
    assert key is not None
    form = {
        "key": key[1],
        "player_id": target.player_id,
        "alias": "1",
        "salary_name": typo,
        "stats_name": target.name,
    }
    assert post(c, "/admin/fa/confirm", data=form) == {
        "kind": "ok",
        "text": f"Matched. Alias saved: {typo} = {target.name}.",
    }
    assert "Nothing to review." in c.get("/admin").text


def test_confirming_without_an_alias() -> None:
    target = FREE[0]
    typo = target.name[:-1] + "q"
    c = client()
    upload_csv(c, csv_row(typo, target.display_position[0], "$2,000,000"))
    key = f"{typo.casefold().split(' ', 1)[0]} {typo.casefold().split(' ', 1)[1]}|F"
    form = {"key": key, "player_id": target.player_id}
    assert post(c, "/admin/fa/confirm", data=form) == {"kind": "ok", "text": "Matched."}


def test_confirming_an_unknown_row_is_an_error_and_many_reviews_are_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = client()
    assert post(c, "/admin/fa/confirm", data={"key": "nope|F", "player_id": "1"}) == {
        "kind": "error",
        "text": "no imported row 'nope|F'.",
    }
    monkeypatch.setattr(admin, "REVIEW_LIMIT", 2)
    upload_csv(c, *(csv_row(f"Qqq Xxzzy{i}", "C", "$1") for i in range(3)))
    html = c.get("/admin").text
    assert "Showing 2 of 3." in html


def test_a_row_with_no_close_name_says_so() -> None:
    c = client()
    upload_csv(c, csv_row("Qqqq Zzzz", "C", "$1"))
    review = c.get("/admin").text.split('id="review"')[1].split("</section>")[0]
    assert "Qqqq" in review


# ---------------------------------------------------------------- the cap hit edit


def test_find_set_and_clear_an_override() -> None:
    c = client()
    p = FREE[1]
    html = c.get("/admin", params={"q": p.name}).text
    assert f"<strong>{p.name}</strong>" in html
    assert "free agent" in html
    result = post(c, "/admin/aav", data={"player_id": p.player_id, "aav": "7.25M", "q": p.name})
    assert result == {"kind": "ok", "text": "Cap hit set to $7,250,000."}
    html = c.get("/admin", params={"q": p.name}).text
    assert '$7,250,000 <span class="muted">(override)</span>' in html
    assert "Clear override" in html
    cleared = post(c, "/admin/aav", data={"player_id": p.player_id, "clear": "1", "q": p.name})
    assert cleared == {"kind": "ok", "text": "Override cleared."}
    bad = post(c, "/admin/aav", data={"player_id": p.player_id, "aav": "7.25"})
    assert bad["kind"] == "error"
    assert "enter a cap hit like $7,250,000" in bad["text"]


def test_a_redirect_keeps_the_search() -> None:
    c = client()
    p = FREE[1]
    response = c.post(
        "/admin/aav",
        data={"player_id": p.player_id, "aav": "1", "q": p.name},
        follow_redirects=False,
    )
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["q"] == [p.name]
    assert response.headers["location"].endswith("#aav")


def test_a_search_shows_owners_and_csv_sources_and_offers_unbind() -> None:
    c = client()
    p = FREE[2]
    upload_csv(c, csv_row(p.name, p.display_position[0], "$1,500,000", p.nhl_team))
    html = c.get("/admin", params={"q": p.name}).text
    assert '$1,500,000 <span class="muted">(csv)</span>' in html
    key = re.search(r"Unbind his CSV row \(([^)]+)\)", html)
    assert key is not None
    assert post(c, "/admin/fa/unbind", data={"key": key[1], "q": p.name}) == {
        "kind": "ok",
        "text": "Unbound: that row waits for review again.",
    }
    rostered = TEAM1.roster[0].player
    assert TEAM1.name in c.get("/admin", params={"q": rostered.name}).text


def test_a_search_with_no_match() -> None:
    assert "No player matches “zzzz”." in client().get("/admin", params={"q": "zzzz"}).text


def test_a_blank_search_finds_nothing() -> None:
    aav = client().get("/admin", params={"q": " ,. "}).text.split('id="aav"')[1]
    assert "<strong>" not in aav.split("</section>")[0]


def test_the_search_stops_at_its_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin, "SEARCH_LIMIT", 2)
    aav = client().get("/admin", params={"q": "e"}).text.split('id="aav"')[1].split("</section>")[0]
    assert aav.count('action="/admin/aav"') == 2


# ---------------------------------------------------------------- rating settings


def test_saving_rating_settings() -> None:
    c = client()
    form = {"divisor_method": "top_per82", "divisor_top_n": "", "gp_floor_percent": "2.5%"}
    assert post(c, "/admin/settings", data=form) == {
        "kind": "ok",
        "text": "Saved: Top-N per-82 rates, top 10, GP floor 2.5%. Every player is re-rated.",
    }
    html = c.get("/admin").text
    assert "In use: Top-N per-82 rates, top 10 (the method" in html
    assert 'name="gp_floor_percent" inputmode="decimal" value="2.5"' in html
    form = {"divisor_method": "workbook", "divisor_top_n": "15", "gp_floor_percent": "2"}
    typed = post(c, "/admin/settings", data=form)
    assert typed["text"].startswith(
        "Saved: Workbook (top-N totals over top-N GP), top 15, GP floor 2%."
    )
    assert 'name="divisor_top_n" inputmode="numeric" value="15"' in c.get("/admin").text


@pytest.mark.parametrize(
    ("form", "message"),
    [
        (
            {"divisor_method": "workbook", "divisor_top_n": "x", "gp_floor_percent": "2"},
            "Not saved: divisor_top_n must be a whole number, got 'x'.",
        ),
        (
            {"divisor_method": "workbook", "divisor_top_n": "", "gp_floor_percent": "two"},
            "Not saved: enter the GP floor as a percent, e.g. 2 (got 'two').",
        ),
        (
            {"divisor_method": "workbook", "divisor_top_n": "", "gp_floor_percent": "150"},
            "Not saved: gp_floor_fraction must be in [0, 1], got 1.5.",
        ),
    ],
)
def test_invalid_settings_are_refused(form: dict[str, str], message: str) -> None:
    assert post(client(), "/admin/settings", data=form) == {"kind": "error", "text": message}


async def test_damaged_stored_settings_are_shown_not_hidden() -> None:
    services = make_services()
    await services.repo.put(SETTINGS_COLLECTION, RATING, {"gp_floor_fraction": 7})
    html = client(services).get("/admin").text
    assert "stored rating settings are invalid: gp_floor_fraction must be in [0, 1]" in html
    assert "In use:" not in html


# ---------------------------------------------------------------- flash and no data


def test_a_forged_odd_or_expired_flash_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    c = client()
    forged = URLSafeTimedSerializer("not the secret", salt=FLASH_SALT).dumps(
        {"kind": "ok", "text": "PWNED"}
    )
    assert "PWNED" not in c.get("/admin", params={"flash": forged}).text
    odd = SIGNER.dumps({"kind": "boom", "text": "X1"})
    assert "X1" not in c.get("/admin", params={"flash": odd}).text
    not_a_dict = SIGNER.dumps(["ok", "X2"])
    assert "X2" not in c.get("/admin", params={"flash": not_a_dict}).text
    old = SIGNER.dumps({"kind": "ok", "text": "OLD1"})
    monkeypatch.setattr(admin, "FLASH_MAX_AGE", -1)
    assert "OLD1" not in c.get("/admin", params={"flash": old}).text


def test_a_valid_flash_is_shown() -> None:
    token = SIGNER.dumps({"kind": "error", "text": "Heads up"})
    html = client().get("/admin", params={"flash": token}).text
    assert '<p class="admin-flash error" role="status">Heads up</p>' in html


class DownSource:
    async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
        raise YahooHTTPError(403, "games", "This application is not authorized")


def no_yahoo() -> Any:
    services = make_services()
    return replace(services, refresh=RefreshService(DownSource(), services.repo, services.clock))


def test_without_yahoo_data_the_page_still_works_and_imports_wait() -> None:
    c = client(no_yahoo())
    html = c.get("/admin", params={"q": "anyone"}).text
    assert "No Yahoo data yet" in html
    assert "No player matches" in html  # nothing to search without the pool
    upload_sheet(c)
    assert "Needs Yahoo data (the teams)." in c.get("/admin").text
    assert upload_csv(c, csv_row("Ada Big", "C", "$1")) == {
        "kind": "error",
        "text": "Import needs Yahoo data first (RefreshError).",  # within the retry backoff
    }


# ---------------------------------------------------------------- parsing helpers


@pytest.mark.parametrize(
    ("text", "dollars"),
    [
        ("$7,250,000", 7_250_000),
        ("7250000", 7_250_000),
        ("7.25M", 7_250_000),
        ("7.25m", 7_250_000),
        (" 725K ", 725_000),
        ("725k", 725_000),
        ("$1.5M", 1_500_000),
        ("0", 0),
        ("$ 775,000", 775_000),
    ],
)
def test_parse_dollars(text: str, dollars: int) -> None:
    assert parse_dollars(text) == dollars


@pytest.mark.parametrize(
    "text", ["7.25", "1,5", "7M5", "7.1234567M", "0.5K5", "-5", "", "$", "7,25,000", chr(0x0667)]
)
def test_parse_dollars_refuses_anything_ambiguous(text: str) -> None:
    with pytest.raises(InputError, match="enter a cap hit like"):
        parse_dollars(text)


def test_parse_dollars_refuses_fractional_dollars() -> None:
    with pytest.raises(InputError, match="enter a cap hit like"):
        parse_dollars("1.2345K")


def test_parse_percent_and_back() -> None:
    assert (parse_percent("2"), parse_percent("2.5 %"), parse_percent(" 0 ")) == (
        "0.02",
        "0.025",
        "0",
    )
    assert (percent_text(0.02), percent_text(0.025), percent_text(1.0)) == ("2", "2.5", "100")
    assert percent_text(0.0) == "0"
    with pytest.raises(InputError, match="as a percent"):
        parse_percent("2 percent")


def test_the_sheet_source_names_each_kind() -> None:
    class SheetsApiLeagueSheet:
        pass

    class Other:
        pass

    assert _sheet_source(SheetsApiLeagueSheet()) == "the live Google Sheet (LEAGUE_SHEET_ID)"
    assert _sheet_source(Other()) == "Other"


@pytest.mark.parametrize(
    ("seconds", "label"),
    [
        (-3, "just now"),
        (59, "just now"),
        (60, "1 min ago"),
        (3600, "1 h ago"),
        (47 * 3600, "47 h ago"),
        (3 * 86400, "3 days ago"),
    ],
)
def test_ago(seconds: float, label: str) -> None:
    assert _ago(seconds) == label
