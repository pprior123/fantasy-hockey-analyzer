"""The demo (``FHA_DEMO=1``) shows salaries with no uploads (issue #14)."""

import re

import httpx
from fastapi.testclient import TestClient

from fha.sources.demo_salaries import CAP, DemoLeagueSheet
from fha.sources.league_sheet.source import XlsxLeagueSheet
from fha.web.app import create_app
from fha.web.context import AppContext, Settings, demo_services
from fha.web.format import money
from tests.unit.web.helpers import PASSWORD, logged_in, make_app, make_services

ENV = {
    "APP_PASSWORD": PASSWORD,
    "SESSION_SECRET": "s" * 32,
    "FHA_DEMO": "1",
    "FHA_INSECURE_COOKIES": "1",
}
HTTP = httpx.AsyncClient()


def demo_client() -> TestClient:
    return logged_in(create_app(AppContext.from_env(ENV)))


def test_the_demo_wires_the_built_in_sheet_and_salaries() -> None:
    services = demo_services(ENV, HTTP, Settings.from_env(ENV))
    assert isinstance(services.sheet, DemoLeagueSheet)
    assert services.prepare is not None


def test_a_configured_sheet_replaces_the_built_in_salaries() -> None:
    env = {**ENV, "LEAGUE_SHEET_XLSX": "private/sheet.xlsx"}
    services = demo_services(env, HTTP, Settings.from_env(env))
    assert isinstance(services.sheet, XlsxLeagueSheet)
    assert services.prepare is None


def test_the_league_shows_every_payroll_and_one_team_over_the_cap() -> None:
    html = demo_client().get("/league").text
    rows = re.findall(r'<td class="num">(\$[\d.]+M)</td>\s*<td class="num ([a-z]*)">', html)
    assert len(rows) == 8
    assert [cls for _, cls in rows].count("over") == 1


def test_players_and_rosters_show_salaries_and_room() -> None:
    client = demo_client()
    players = client.get("/players").text
    assert len(re.findall(r"\$\d+\.\d\dM", players)) > 50
    rosters = client.get("/rosters").text
    assert f"Cap {money(CAP)}" in rosters
    assert "Payroll —" not in rosters


def test_admin_names_the_built_in_sheet_and_holds_the_rows_to_review() -> None:
    html = demo_client().get("/admin").text
    assert "the built-in demo sheet (FHA_DEMO)" in html
    assert len(re.findall(r"<strong>[A-Z]\. \w+</strong>", html)) == 3
    assert html.count('<span class="good">matches</span>') == 5


def test_demo_salaries_are_prepared_before_the_first_page_only_when_wired() -> None:
    calls: list[str] = []

    async def prepare() -> None:
        calls.append("prepared")

    services = make_services()
    services.prepare = prepare
    client = logged_in(make_app(services))
    client.get("/players")
    client.get("/league")
    assert calls == ["prepared", "prepared"]  # awaited every page; DemoSalaries runs once
    assert "Payroll" in logged_in(make_app(make_services())).get("/league").text
