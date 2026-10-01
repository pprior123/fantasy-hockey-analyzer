"""The Admin screen (SPEC §7.5, §4a, §6): every section and action, through the app."""

import asyncio
import re
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from fha.domain.names import normalize_name
from fha.services.aliases import load_aliases
from fha.services.refresh import RefreshService
from fha.services.settings import COLLECTION as SETTINGS_COLLECTION
from fha.services.settings import RATING
from fha.sources.google_auth import GoogleAuthError
from fha.sources.league_sheet.models import LeagueSheetError, ParsedSheet
from fha.sources.league_sheet.parse import parse_sheet
from fha.sources.league_sheet.source import FakeLeagueSheet, SheetsApiLeagueSheet
from fha.sources.yahoo.client import YahooHTTPError
from fha.sources.yahoo.demo import demo_snapshot
from fha.sources.yahoo.fake import FakeYahooSource
from fha.sources.yahoo.models import LeagueSnapshot, Team
from fha.storage.memory import InMemoryRepository
from fha.storage.repository import RepositoryError
from fha.web.format import ago
from fha.web.routes import admin
from fha.web.routes.admin import (
    FLASH_SALT,
    STORE_REFUSED,
    InputError,
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


def test_a_binding_whose_tab_left_the_sheet_is_shown_and_can_be_undone() -> None:
    """M4R4A-1: a GM renames their tab; the old binding would hold the team forever."""
    c = client()
    upload_sheet(c)
    post(c, "/admin/bind", data={"tab": "Pinecone", "team_key": TEAM1.team_key})
    upload_sheet(c, to_xlsx(s.grid(sheet_tab("Pinecone FC", TEAM1), sheet_tab("Maple", TEAM2))))
    html = c.get("/admin").text
    (stale,) = [f for f in html.split("</form>") if 'admin-label">Pinecone<' in f]
    assert 'action="/admin/bind"' in stale
    assert '<input type="hidden" name="team_key" value="">' in stale
    assert f"bound to {TEAM1.name}; no longer on the sheet" in stale
    taken = post(c, "/admin/bind", data={"tab": "Pinecone FC", "team_key": TEAM1.team_key})
    assert taken["kind"] == "error"
    assert post(c, "/admin/bind", data={"tab": "Pinecone", "team_key": ""}) == {
        "kind": "ok",
        "text": "Pinecone is unbound.",
    }
    assert "no longer on the sheet" not in c.get("/admin").text
    assert post(c, "/admin/bind", data={"tab": "Pinecone FC", "team_key": TEAM1.team_key}) == {
        "kind": "ok",
        "text": "Pinecone FC is now bound.",
    }


@pytest.mark.parametrize(
    ("form", "text"),
    [
        (
            {"tab": "Pinecone", "team_key": "nope.l.1.t.9"},
            "Pinecone: that team isn't in the league.",
        ),
        (
            {"tab": "Nowhere", "team_key": TEAM1.team_key},
            "Nowhere: that tab isn't in the stored sheet.",
        ),
        ({"tab": "__x__", "team_key": ""}, "__x__: that tab isn't in the stored sheet."),
    ],
)
def test_binding_checks_the_tab_and_the_team(form: dict[str, str], text: str) -> None:
    """M4R1A-8: a posted tab or team must exist; nothing is stored otherwise."""
    c = client()
    upload_sheet(c)
    assert post(c, "/admin/bind", data=form) == {"kind": "error", "text": text}
    assert "bound to" not in c.get("/admin").text


def test_a_flash_quoting_huge_input_stays_short() -> None:
    """M4R5A-2: the flash rides in the redirect URL; random input doesn't compress."""
    import secrets

    c = client()
    upload_sheet(c)
    tab = secrets.token_urlsafe(30_000)
    response = c.post("/admin/bind", data={"tab": tab}, follow_redirects=False)
    assert len(response.headers["location"]) < 1000
    assert flash_of(response)["text"] == f"{tab[:39]}…: that tab isn't in the stored sheet."


def test_the_kept_search_is_trimmed_and_capped() -> None:
    """M4R6A-1/A-2: the search rides in the redirect URL too; padding doesn't count."""
    c = client()
    response = c.post(
        "/admin/fa/unbind", data={"key": "nope", "q": "  " + "y" * 5000}, follow_redirects=False
    )
    assert parse_qs(urlsplit(response.headers["location"]).query)["q"] == ["y" * 100]


def test_a_long_import_summary_keeps_its_counts_and_every_section() -> None:
    """M4R6A-3: a cut at the end of the message dropped the unknown teams."""
    rows = [csv_row(f"Player{i:02d} Name", "C", f"${n}", "TOR") for i in range(14) for n in (1, 2)]
    teams = [csv_row(f"Some Else{i}", "C", "$3", f"Nowhere City {i:02d}") for i in range(10)]
    long_names = [
        csv_row(f"A{'a' * 300} Long{i}", "C", f"${n}", "TOR") for i in range(2) for n in (1, 2)
    ]
    text = upload_csv(client(), *long_names, *rows, *teams)["text"]
    assert " and 8 more. Unknown teams: " in text  # each long name is cut, too (M4R7A-2)
    assert text.endswith("Nowhere City 07 and 2 more.")


def test_the_last_guard_bounds_a_message_that_quotes_input_in_full() -> None:
    """M4R7A-1: an engine error quotes the method whole; random text doesn't compress."""
    import secrets

    method = secrets.token_hex(10_000)
    response = client().post(
        "/admin/settings",
        data={"divisor_method": method, "gp_floor_percent": "2"},
        follow_redirects=False,
    )
    assert len(response.headers["location"]) < 1500
    text = flash_of(response)["text"]
    assert len(text) == 1000
    assert text.endswith("…")


def test_a_row_match_quotes_a_long_tab_short() -> None:
    tab = "t" * 5000
    form = {"tab": tab, "key": "k", "player_id": "p"}
    short = "t" * 39 + "…"
    assert post(client(), "/admin/sheet/confirm", data=form)["text"] == (
        f"{short}: tab '{short}' isn't bound to a team yet."
    )


def test_a_value_the_store_refuses_is_a_message_not_a_500() -> None:
    """A tab name Firestore can't hold as a map key (``__x__``) fails in the store."""
    c = client()
    upload_sheet(c, to_xlsx(s.grid(sheet_tab("__x__", TEAM1))))
    result = post(c, "/admin/bind", data={"tab": "__x__", "team_key": TEAM1.team_key})
    assert result["kind"] == "error"
    assert result["text"] == f"__x__: {STORE_REFUSED}."


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


@pytest.mark.parametrize(
    ("change", "text"),
    [
        ({"key": "nobody|F"}, "Pinecone: that row isn't on the tab."),
        (
            {"player_id": TEAM2.roster[0].player.player_id},
            "Pinecone: that player isn't on the tab's Yahoo roster.",
        ),
    ],
)
def test_a_row_match_must_be_a_tab_row_and_a_roster_player(
    change: dict[str, str], text: str
) -> None:
    """M4R1A-8: the row must be on the tab, the player on the bound team's roster."""
    c = client()
    upload_sheet(c)
    post(c, "/admin/bind", data={"tab": "Pinecone", "team_key": TEAM1.team_key})
    form = {
        "tab": "Pinecone",
        "key": "zed nobody|F",
        "player_id": TEAM1.roster[-1].player.player_id,
    }
    assert post(c, "/admin/sheet/confirm", data={**form, **change}) == {
        "kind": "error",
        "text": text,
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


def test_a_sheet_over_4_mb_is_refused_though_under_the_request_limit() -> None:
    """4.2 MB passes the 4.5 MB request limit but not the sheet's own 4 MiB cap."""
    result = upload_sheet(client(), b"x" * (4 * 1024 * 1024 + 1))
    assert result == {"kind": "error", "text": "Upload: that file is over 4 MB."}


@pytest.mark.parametrize(
    ("path", "kw"),
    [
        ("/admin/bind", {"data": {"team_key": "x"}}),
        ("/admin/csv", {"data": {"file": "Player,Pos,Cap Hit"}}),
        ("/admin/aav", {}),
    ],
)
def test_a_missing_or_mistyped_field_is_the_400_page_not_json(
    path: str, kw: dict[str, Any]
) -> None:
    """M4R2A-6: FastAPI's 422 JSON echoed the input back; the app's page doesn't."""
    response = client().post(path, **kw)
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "A form value was missing" in response.text
    assert 'class="topnav"' in response.text  # M4R6A-4: signed in, so the nav stays
    assert "Player,Pos" not in response.text
    assert '<a href="/admin">Start over</a>' in response.text


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
    form = {"key": key[1], "player_id": target.player_id, "alias": "1"}
    assert post(c, "/admin/fa/confirm", data=form) == {
        "kind": "ok",
        "text": f"Matched. Alias saved: {last_first(typo)[1:-1]} = {target.name}.",
    }
    assert "Nothing to review." in c.get("/admin").text


def test_an_alias_is_named_from_the_stored_row_and_the_pool_not_the_form() -> None:
    """M4R1A-8: the form's names are ignored; the alias is the imported row's name for
    the chosen pool player's name, whatever else is posted."""
    target = FREE[0]
    typo = target.name[:-1] + "q"
    c = client()
    upload_csv(c, csv_row(typo, target.display_position[0], "$2,000,000"))
    key = re.search(r'name="key" value="([^"]+)"', c.get("/admin").text)
    assert key is not None
    form = {
        "key": key[1],
        "player_id": target.player_id,
        "alias": "1",
        "salary_name": "Wayne Gretzky",
        "stats_name": "Connor McDavid",
    }
    result = post(c, "/admin/fa/confirm", data=form)
    assert result["text"] == f"Matched. Alias saved: {last_first(typo)[1:-1]} = {target.name}."
    repo = c.app.state.context.services.repo  # type: ignore[attr-defined]
    aliases = asyncio.run(load_aliases(repo))
    assert aliases.table[normalize_name(typo)] == (target.name,)  # the stored alias itself
    assert all(
        "Gretzky" not in names and "McDavid" not in names for names in aliases.table.values()
    )
    assert normalize_name("Wayne Gretzky") not in aliases.table


def test_confirming_a_player_outside_the_pool_is_refused() -> None:
    c = client()
    upload_csv(c, csv_row("Qqq Xxzzy", "C", "$1"))
    key = re.search(r'name="key" value="([^"]+)"', c.get("/admin").text)
    assert key is not None
    assert post(c, "/admin/fa/confirm", data={"key": key[1], "player_id": "nobody"}) == {
        "kind": "error",
        "text": "that player isn't in the pool.",
    }


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
    pid = FREE[0].player_id
    assert post(c, "/admin/fa/confirm", data={"key": "nope|F", "player_id": pid}) == {
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
    outside = post(c, "/admin/aav", data={"player_id": "nobody", "aav": "1M"})
    assert outside == {"kind": "error", "text": "that player isn't in the pool."}


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
        (  # M4R4A-3: not Python's "Exceeds the limit (4300 digits)" text
            {"divisor_method": "workbook", "divisor_top_n": "9" * 5000, "gp_floor_percent": "2"},
            "Not saved: divisor_top_n must be a whole number of at most 6 digits.",
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


async def test_damaged_settings_are_messages_on_the_admin_posts_that_rate() -> None:
    """M4R2A-1: these POSTs rate the league to check their values; with damaged
    settings they say so, pointing at the form that fixes them, never a 500."""
    services = make_services()
    await services.repo.put(SETTINGS_COLLECTION, RATING, {"gp_floor_fraction": 7})
    c = client(services)
    p = FREE[0]
    assert post(c, "/admin/aav", data={"player_id": p.player_id, "aav": "1M"}) == {
        "kind": "error",
        "text": "A cap hit needs valid rating settings first (below).",
    }
    assert post(c, "/admin/fa/confirm", data={"key": "k|F", "player_id": p.player_id}) == {
        "kind": "error",
        "text": "Matching needs valid rating settings first (below).",
    }
    confirm = post(c, "/admin/sheet/confirm", data={"tab": "T", "key": "k", "player_id": "1"})
    assert confirm["text"] == "T: Matching a row needs valid rating settings first (below)."
    assert upload_csv(c, csv_row("Ada Big", "C", "$1")) == {
        "kind": "error",
        "text": "Import needs valid rating settings first.",
    }


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
    assert "No Yahoo data, so the sections" in html
    assert "The search needs Yahoo data" in html  # nothing to search without the pool
    upload_sheet(c)
    assert "Needs Yahoo data (the teams)." in c.get("/admin").text
    assert upload_csv(c, csv_row("Ada Big", "C", "$1")) == {
        "kind": "error",
        "text": "Import needs Yahoo data first (RefreshError).",
    }
    bind = post(c, "/admin/bind", data={"tab": "Pinecone", "team_key": TEAM1.team_key})
    assert bind == {
        "kind": "error",
        "text": "Pinecone: Binding needs Yahoo data first (RefreshError).",
    }


def test_a_transport_error_without_a_cache_still_renders_admin() -> None:
    """M4R1A-1: not only Yahoo's HTTP errors; a timeout with no cache is "no data" too."""

    class TimingOut:
        async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
            raise httpx.ConnectTimeout("timed out")

    services = make_services()
    services = replace(services, refresh=RefreshService(TimingOut(), services.repo, services.clock))
    c = client(services)
    for path in ("/admin", "/admin?q=x"):
        response = c.get(path)
        assert response.status_code == 200
        assert "No Yahoo data, so the sections" in response.text
        assert "ConnectTimeout: timed out" in response.text
    assert upload_csv(c, csv_row("Ada Big", "C", "$1"))["kind"] == "error"


def test_a_refused_service_account_is_a_message() -> None:
    """M4R1A-2: a revoked key fails in the token call; the read says so, not a 500."""

    async def refused() -> str:
        raise GoogleAuthError("Google refused the token request: HTTP 400 (invalid_grant)")

    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    sheet = SheetsApiLeagueSheet(http, "sheet-id", refused)
    c = client(replace(make_services(), sheet=sheet))
    assert post(c, "/admin/sheet/read") == {
        "kind": "error",
        "text": "The sheet couldn't be read: service-account auth failed: "
        "Google refused the token request: HTTP 400 (invalid_grant)",
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


@pytest.mark.parametrize("parse", [parse_dollars, parse_percent])
def test_a_refused_value_is_quoted_short(parse: Any) -> None:
    with pytest.raises(InputError) as info:
        parse("9x" * 2500)
    assert f"(got '{'9x' * 19}9…')" in str(info.value)


def test_a_padded_cap_hit_over_the_limit_is_quoted_short() -> None:
    """M4R8A-1: spaces are dropped before the length check, not from the quote."""
    with pytest.raises(InputError) as info:
        parse_dollars("2" + " " * 3000 + "000000000")
    assert str(info.value).endswith(f"(got '2{' ' * 38}…')")


@pytest.mark.parametrize(
    "text", ["7.25", "1,5", "7M5", "7.1234567M", "0.5K5", "-5", "", "$", "7,25,000", chr(0x0667)]
)
def test_parse_dollars_refuses_anything_ambiguous(text: str) -> None:
    with pytest.raises(InputError, match="enter a cap hit like"):
        parse_dollars(text)


def test_parse_dollars_refuses_huge_amounts_and_strings() -> None:
    """M4R1A-4: 5000 digits was a 500 (int() refuses over 4300), and 23 digits a
    RepositoryError (Firestore integers are 64-bit)."""
    assert parse_dollars("1,000,000,000") == 1_000_000_000
    with pytest.raises(InputError, match=r"can't be over \$1,000,000,000"):
        parse_dollars("1000000001")
    with pytest.raises(InputError, match=r"can't be over"):
        parse_dollars("99999999999999999999")  # 20 digits: parsed, then refused
    for huge in ("9" * 5000, "9" * 21, "1" * 18 + ".5M"):
        with pytest.raises(InputError, match="enter a cap hit like"):
            parse_dollars(huge)


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

    class DemoLeagueSheet:
        pass

    assert _sheet_source(SheetsApiLeagueSheet()) == "the live Google Sheet (LEAGUE_SHEET_ID)"
    assert _sheet_source(DemoLeagueSheet()) == "the built-in demo sheet (FHA_DEMO)"
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
    assert ago(seconds) == label


class Refusing(InMemoryRepository):
    """A store that refuses every write once ``refuse`` is set (e.g. Firestore down)."""

    refuse = False

    async def put(self, collection: str, doc_id: str, doc: Any) -> None:
        if self.refuse:
            raise RepositoryError("Firestore answered HTTP 503", detail="HTTP 503 UNAVAILABLE")
        await super().put(collection, doc_id, doc)

    async def replace_all(self, collection: str, docs: Any) -> None:
        if self.refuse:
            raise RepositoryError("Firestore answered HTTP 503")
        await super().replace_all(collection, docs)


def test_a_store_that_refuses_writes_is_a_message_on_every_admin_post() -> None:
    repo = Refusing()
    base = make_services()
    services = replace(
        base,
        repo=repo,
        refresh=RefreshService(FakeYahooSource(DEMO), repo, base.clock),
        sheet=FakeLeagueSheet(parse_sheet(GRID)),
    )
    c = client(services)
    p = FREE[2]
    upload_csv(c, csv_row(p.name, p.display_position[0], "$1,500,000", p.nhl_team))
    key = re.search(r"Unbind his CSV row \(([^)]+)\)", c.get("/admin", params={"q": p.name}).text)
    assert key is not None
    repo.refuse = True
    refused = STORE_REFUSED  # never Firestore's own text: the flash is in the URL
    assert post(c, "/admin/sheet/read")["text"] == f"The sheet couldn't be saved: {refused}."
    assert upload_sheet(c)["text"] == f"The sheet couldn't be saved: {refused}."
    csv = upload_csv(c, csv_row(p.name, p.display_position[0], "$2,000,000", p.nhl_team))
    assert csv["text"] == f"The CSV couldn't be saved: {refused}."
    unbound = post(c, "/admin/fa/unbind", data={"key": key[1]})
    assert unbound == {"kind": "error", "text": f"{refused}."}
    aav = post(c, "/admin/aav", data={"player_id": p.player_id, "aav": "1M"})
    assert aav == {"kind": "error", "text": f"{refused}."}
    form = {"divisor_method": "workbook", "divisor_top_n": "", "gp_floor_percent": "2"}
    assert post(c, "/admin/settings", data=form)["text"] == f"Not saved: {refused}."


def test_a_store_that_refuses_writes_is_a_message_on_the_confirm_posts() -> None:
    repo = Refusing()
    base = make_services()
    services = replace(
        base, repo=repo, refresh=RefreshService(FakeYahooSource(DEMO), repo, base.clock)
    )
    c = client(services)
    upload_sheet(c)
    post(c, "/admin/bind", data={"tab": "Pinecone", "team_key": TEAM1.team_key})
    target = FREE[0]
    typo = target.name[:-1] + "q"
    upload_csv(c, csv_row(typo, target.display_position[0], "$2,000,000"))
    key = re.search(
        r'action="/admin/fa/confirm".*?name="key" value="([^"]+)"', c.get("/admin").text, re.S
    )
    assert key is not None
    repo.refuse = True
    refused = STORE_REFUSED  # never Firestore's own text: the flash is in the URL
    row = {"tab": "Pinecone", "key": "zed nobody|F", "player_id": TEAM1.roster[-1].player.player_id}
    assert post(c, "/admin/sheet/confirm", data=row)["text"] == f"Pinecone: {refused}."
    fa = {"key": key[1], "player_id": target.player_id}
    assert post(c, "/admin/fa/confirm", data=fa) == {"kind": "error", "text": f"{refused}."}


def test_a_store_error_never_puts_its_text_in_the_redirect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M4R3A-3: the flash is signed, not encrypted, and travels in the URL (history,
    request logs); Firestore's text can name the project."""
    repo = Refusing()
    base = make_services()
    services = replace(
        base, repo=repo, refresh=RefreshService(FakeYahooSource(DEMO), repo, base.clock)
    )
    c = client(services)
    c.get("/admin")
    repo.refuse = True
    response = c.post(
        "/admin/aav", data={"player_id": FREE[0].player_id, "aav": "1M"}, follow_redirects=False
    )
    assert "Firestore" not in flash_of(response)["text"]
    assert "Firestore" not in caplog.text
    expected = "storage refused an Admin change (RepositoryError: HTTP 503 UNAVAILABLE)"
    assert expected in caplog.text


def test_no_alias_is_saved_when_the_names_already_match() -> None:
    """An alias from a name to itself would be noise ("Alias saved: X = X")."""
    target = FREE[0]
    c = client()
    upload_csv(c, csv_row(target.name, "G" if target.is_goalie else "D", "$2,000,000", "XXX"))
    html = c.get("/admin").text
    key = re.search(r'action="/admin/fa/confirm".*?name="key" value="([^"]+)"', html, re.S)
    assert key is not None
    form = {"key": key[1], "player_id": target.player_id, "alias": "1"}
    assert post(c, "/admin/fa/confirm", data=form) == {"kind": "ok", "text": "Matched."}
