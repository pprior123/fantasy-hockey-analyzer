"""The demo (``FHA_DEMO=1``) shows salaries with no uploads (issue #14)."""

import logging
import re

import httpx
import pytest
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


def test_prepare_is_awaited_on_every_page() -> None:
    calls: list[str] = []

    async def prepare() -> None:
        calls.append("prepared")

    services = make_services()
    services.prepare = prepare
    client = logged_in(make_app(services))
    client.get("/players")
    client.get("/league")
    assert calls == ["prepared", "prepared"]  # DemoSalaries makes the repeats free


def test_without_prepare_no_payroll_is_known(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        html = logged_in(make_app(make_services())).get("/league").text
    assert "demo salaries" not in caplog.text  # nothing to prepare, nothing tried
    assert not re.findall(r'<td class="num">\$[\d.]+M</td>', html)


def test_a_failed_prepare_is_logged_and_the_pages_still_work(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R1-M3: Admin must stay usable, so the demo's salaries are optional."""
    calls: list[str] = []

    async def prepare() -> None:
        calls.append("tried")
        raise ValueError("secret detail")

    services = make_services()
    services.prepare = prepare
    client = logged_in(make_app(services))
    with caplog.at_level(logging.WARNING):
        assert client.get("/admin").status_code == 200
        assert client.get("/players").status_code == 200
    assert calls == ["tried", "tried"]  # tried again on the next page
    assert "demo salaries not stored: ValueError" in caplog.text
    assert "secret detail" not in caplog.text
