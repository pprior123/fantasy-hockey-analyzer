"""Wiring from the environment: settings, demo, production, the lazy ASGI entry point."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from fha.sources.league_sheet.source import SheetsApiLeagueSheet, XlsxLeagueSheet
from fha.storage.firestore import FirestoreRepository
from fha.storage.local_json import PRIVATE_DIR, LocalJsonRepository
from fha.storage.memory import InMemoryRepository
from fha.web import main
from fha.web.context import (
    AppContext,
    ConfigError,
    Settings,
    demo_services,
    league_sheet_from_env,
    production_services,
)

BASE = {"APP_PASSWORD": "pw", "SESSION_SECRET": "secret-" * 5}
HTTP = httpx.AsyncClient()
KEY = json.dumps({"client_email": "fha@demo-fha.iam.gserviceaccount.com", "private_key": "x"})


def test_settings_need_the_password_and_the_secret_named_not_shown() -> None:
    with pytest.raises(ConfigError, match=r"^APP_PASSWORD and SESSION_SECRET not set$"):
        Settings.from_env({})
    with pytest.raises(ConfigError, match=r"^SESSION_SECRET not set$"):
        Settings.from_env({"APP_PASSWORD": "pw"})


def test_settings_defaults_and_overrides() -> None:
    plain = Settings.from_env(BASE)
    assert (plain.secure_cookies, plain.demo, plain.ttl_seconds, plain.baseline_min_gp) == (
        True,
        False,
        1800.0,
        10,
    )
    tuned = Settings.from_env(
        {
            **BASE,
            "FHA_INSECURE_COOKIES": "1",
            "FHA_DEMO": "1",
            "CACHE_TTL_MINUTES": "5",
            "BASELINE_MIN_GP": " 20 ",
        }
    )
    assert (tuned.secure_cookies, tuned.demo, tuned.ttl_seconds, tuned.baseline_min_gp) == (
        False,
        True,
        300.0,
        20,
    )
    assert Settings.from_env({**BASE, "CACHE_TTL_MINUTES": " "}).ttl_seconds == 1800.0


def test_the_session_secret_must_be_long_enough() -> None:
    """M4R1A-11: it signs the session cookie; a short one is guessable offline."""
    with pytest.raises(ConfigError, match=r"^SESSION_SECRET must be at least 32 characters$"):
        Settings.from_env({**BASE, "SESSION_SECRET": "x" * 31})
    assert Settings.from_env({**BASE, "SESSION_SECRET": "x" * 32}).session_secret == "x" * 32


def test_insecure_cookies_are_refused_on_vercel() -> None:
    env = {**BASE, "FHA_INSECURE_COOKIES": "1", "VERCEL": "1"}
    with pytest.raises(ConfigError, match=r"^FHA_INSECURE_COOKIES is for local http only"):
        Settings.from_env(env)
    assert Settings.from_env({**BASE, "VERCEL": "1"}).secure_cookies is True


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e400", "2e6"])
def test_non_finite_or_absurd_numbers_are_config_errors(value: str) -> None:
    """M4R2A-4: nan was accepted (a nan TTL) and 1e400 raised OverflowError."""
    for name in ("CACHE_TTL_MINUTES", "BASELINE_MIN_GP"):
        with pytest.raises(ConfigError, match=f"^{name} must be a finite number up to 1e"):
            Settings.from_env({**BASE, name: value})


def test_the_salary_cap_override() -> None:
    """M4R1B-4: SALARY_CAP, whole dollars, only a fallback for the sheet's cap."""
    assert Settings.from_env(BASE).salary_cap is None
    assert Settings.from_env({**BASE, "SALARY_CAP": " 119600000 "}).salary_cap == 119_600_000
    assert Settings.from_env({**BASE, "SALARY_CAP": "119,600,000"}).salary_cap == 119_600_000
    assert Settings.from_env({**BASE, "SALARY_CAP": ""}).salary_cap is None
    for bad in ("119.6M", "0", "-5", "1" * 13, "١٢"):
        with pytest.raises(ConfigError, match=r"^SALARY_CAP must be a whole number of dollars"):
            Settings.from_env({**BASE, "SALARY_CAP": bad})


@pytest.mark.parametrize(
    ("value", "message"), [("soon", "must be a number"), ("-1", "must be >= 0")]
)
def test_a_bad_number_is_named(value: str, message: str) -> None:
    with pytest.raises(ConfigError, match=f"^CACHE_TTL_MINUTES {message}$"):
        Settings.from_env({**BASE, "CACHE_TTL_MINUTES": value})


def test_settings_never_show_the_password_or_secret() -> None:
    text = repr(Settings.from_env(BASE))
    assert "pw" not in text
    assert "secret-" not in text


def test_demo_mode_needs_no_yahoo_and_keeps_data_in_memory() -> None:
    context = AppContext.from_env({**BASE, "FHA_DEMO": "1"})
    services = context.factory(HTTP)
    assert isinstance(services.repo, InMemoryRepository)
    assert type(services.sheet).__name__ == "DemoLeagueSheet"  # built-in salaries (issue #14)


def test_demo_mode_can_use_the_dev_file_and_a_downloaded_sheet() -> None:
    env = {
        **BASE,
        "FHA_LOCAL_REPOSITORY": str(PRIVATE_DIR / "demo-test-never-created.json"),
        "LEAGUE_SHEET_XLSX": "private/sheet.xlsx",
    }
    services = demo_services(env, HTTP, Settings.from_env(env))
    assert isinstance(services.repo, LocalJsonRepository)
    assert isinstance(services.sheet, XlsxLeagueSheet)


def test_demo_mode_refuses_a_dev_file_outside_private(tmp_path: Path) -> None:
    env = {**BASE, "FHA_LOCAL_REPOSITORY": str(tmp_path / "repo.json")}
    with pytest.raises(ConfigError, match="inside the private/ directory"):
        demo_services(env, HTTP, Settings.from_env(env))


def test_production_needs_the_yahoo_credentials() -> None:
    with pytest.raises(ConfigError, match=r"^YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET not set"):
        AppContext.from_env(BASE)


def test_production_services_name_a_missing_repository() -> None:
    env = {**BASE, "YAHOO_CLIENT_ID": "id", "YAHOO_CLIENT_SECRET": "sec"}
    context = AppContext.from_env(env)
    with pytest.raises(ConfigError, match="no repository configured"):
        context.factory(HTTP)


def test_production_services_wire_firestore_and_the_live_sheet() -> None:
    env = {
        **BASE,
        "YAHOO_CLIENT_ID": "id",
        "YAHOO_CLIENT_SECRET": "sec",
        "FIRESTORE_PROJECT_ID": "fha-prod",
        "FIRESTORE_SERVICE_ACCOUNT_JSON": KEY,
        "LEAGUE_SHEET_ID": "sheet-id",
    }
    services = production_services(env, HTTP, Settings.from_env(env))
    assert isinstance(services.repo, FirestoreRepository)
    assert isinstance(services.sheet, SheetsApiLeagueSheet)
    assert "sheet-id" not in repr(services.sheet)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"LEAGUE_SHEET_ID": "s"}, "^LEAGUE_SHEET_ID needs FIRESTORE_SERVICE_ACCOUNT_JSON$"),
        (
            {"LEAGUE_SHEET_ID": "s", "FIRESTORE_SERVICE_ACCOUNT_JSON": "not json"},
            "^FIRESTORE_SERVICE_ACCOUNT_JSON: the service-account key is not valid JSON$",
        ),
    ],
)
def test_the_live_sheet_needs_a_usable_key(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        league_sheet_from_env(env, HTTP)


def test_no_sheet_configured_is_none() -> None:
    assert league_sheet_from_env({}, HTTP) is None


def test_the_entry_point_builds_an_error_app_without_configuration() -> None:
    with TestClient(main.build({})) as client:
        response = client.get("/players")
    assert response.status_code == 500
    assert "APP_PASSWORD and SESSION_SECRET not set" in response.text


def test_the_lazy_app_builds_once_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {**BASE, "FHA_DEMO": "1", "FHA_INSECURE_COOKIES": "1"}.items():
        monkeypatch.setenv(name, value)
    built: list[Any] = []
    real = main.build
    monkeypatch.setattr(main, "build", lambda: built.append(1) or real())
    lazy = main.LazyApp()
    with TestClient(lazy) as client:
        assert client.get("/login").status_code == 200
        login = client.post("/login", data={"password": "pw"}, follow_redirects=False)
        assert login.status_code == 303
        assert client.get("/players").status_code == 200
    assert built == [1]


@pytest.mark.parametrize(
    ("extra", "redirect"),
    [
        ({}, "https://localhost:8000"),
        ({"YAHOO_REDIRECT_URI": ""}, "https://localhost:8000"),
        ({"YAHOO_REDIRECT_URI": "https://example.test:9"}, "https://example.test:9"),
    ],
)
def test_production_refreshes_with_the_consent_redirect_uri(
    monkeypatch: pytest.MonkeyPatch, extra: dict[str, str], redirect: str
) -> None:
    """Yahoo's token refresh sends the redirect URI, so production must send the one
    consent used: the scripts' ``YAHOO_REDIRECT_URI``, else the same default."""
    from fha.sources.yahoo import client

    seen: list[Any] = []
    real = client.YahooClient

    def spy(http: httpx.AsyncClient, creds: Any, *args: Any, **kw: Any) -> Any:
        seen.append(creds)
        return real(http, creds, *args, **kw)

    monkeypatch.setattr(client, "YahooClient", spy)
    env = {
        **BASE,
        "YAHOO_CLIENT_ID": "id",
        "YAHOO_CLIENT_SECRET": "sec",
        "FIRESTORE_PROJECT_ID": "fha-prod",
        "FIRESTORE_SERVICE_ACCOUNT_JSON": KEY,
        **extra,
    }
    production_services(env, HTTP, Settings.from_env(env))
    assert [c.redirect_uri for c in seen] == [redirect]
