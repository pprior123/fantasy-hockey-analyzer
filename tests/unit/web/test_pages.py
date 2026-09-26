"""The common chrome, placeholders, PWA files, errors and logging (SPEC §7)."""

import json
import logging
import struct

import pytest
from fastapi.testclient import TestClient

from fha.web.app import NAV, configure_logging, create_app, create_error_app
from fha.web.context import AppContext, ConfigError
from tests.unit.web.helpers import SETTINGS, logged_in, make_app

FOOTER = "Fantasy data provided by Yahoo Fantasy"


@pytest.mark.parametrize(("key", "label"), NAV)
def test_every_screen_has_the_nav_and_the_attribution_footer(key: str, label: str) -> None:
    response = logged_in(make_app()).get(f"/{key}")
    assert response.status_code == 200
    html = response.text
    assert f"<h1>{label}</h1>" in html
    for other, text in NAV:
        assert f'href="/{other}"' in html
        assert f">{text}</a>" in html
    assert f'href="/{key}" class="on" aria-current="page"' in html
    assert FOOTER in html
    assert 'href="https://sports.yahoo.com/fantasy/"' in html
    assert 'src="/static/htmx.min.js"' in html
    assert 'rel="manifest"' in html
    assert 'name="viewport"' in html


def test_the_root_goes_to_players() -> None:
    response = logged_in(make_app()).get("/", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/players")


def test_the_manifest_is_public_json() -> None:
    with TestClient(make_app()) as client:
        response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/manifest+json"
    manifest = json.loads(response.text)
    assert (manifest["start_url"], manifest["display"]) == ("/players", "standalone")
    assert {i["sizes"] for i in manifest["icons"]} == {"192x192", "512x512"}


@pytest.mark.parametrize(
    ("path", "size"),
    [
        ("/static/icons/icon-192.png", 192),
        ("/static/icons/icon-512.png", 512),
        ("/static/icons/apple-touch-icon.png", 180),
    ],
)
def test_the_icons_are_public_pngs_of_their_size(path: str, size: int) -> None:
    with TestClient(make_app()) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    body = response.content
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", body[16:24]) == (size, size)


def test_static_assets_are_public() -> None:
    with TestClient(make_app()) as client:
        assert client.get("/static/app.css").status_code == 200
        htmx = client.get("/static/htmx.min.js")
    assert htmx.status_code == 200
    assert htmx.text.startswith("var htmx=")


def test_a_configuration_error_shows_the_variables_it_needs() -> None:
    def failing(http: object) -> object:
        raise ConfigError("YAHOO_CLIENT_ID not set")

    app = create_app(AppContext(SETTINGS, failing))  # type: ignore[arg-type]
    client = logged_in(app)
    response = client.get("/players")
    assert response.status_code == 500
    assert "YAHOO_CLIENT_ID not set" in response.text
    assert FOOTER in response.text


def test_the_error_app_answers_everything_with_the_configuration_error() -> None:
    with TestClient(create_error_app("APP_PASSWORD not set")) as client:
        for path in ("/", "/players", "/login"):
            response = client.get(path)
            assert response.status_code == 500
            assert "APP_PASSWORD not set" in response.text
        assert client.post("/login").status_code == 500
        assert client.get("/static/app.css").status_code == 200


def test_an_unexpected_error_shows_a_page_without_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = make_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    client = logged_in(app)
    client_raw = TestClient(app, raise_server_exceptions=False, cookies=client.cookies)
    response = client_raw.get("/boom")
    assert response.status_code == 500
    assert "unexpected error" in response.text
    assert "secret internal detail" not in response.text
    assert "unhandled RuntimeError on /boom" in caplog.text
    assert "secret internal detail" not in caplog.text


def test_the_http_loggers_are_quiet_so_urls_dont_leak() -> None:
    logging.getLogger("httpx").setLevel(logging.INFO)
    with TestClient(make_app()):
        pass
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    logging.getLogger("httpx").setLevel(logging.INFO)
    configure_logging()
    assert logging.getLogger("httpx").level == logging.WARNING


def test_the_app_has_no_public_api_docs() -> None:
    with TestClient(make_app()) as client:
        for path in ("/docs", "/openapi.json", "/redoc"):
            assert client.get(path, follow_redirects=False).status_code in (303, 404)
