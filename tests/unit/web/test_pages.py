"""The common chrome, placeholders, PWA files, errors and logging (SPEC §7)."""

import json
import logging
import re
import struct

import pytest
from fastapi.testclient import TestClient

from fha.web.app import (
    HERE,
    MAX_BODY_BYTES,
    NAV,
    configure_logging,
    create_app,
    create_error_app,
)
from fha.web.context import AppContext, ConfigError, Services
from tests.unit.web.helpers import PASSWORD, SETTINGS, logged_in, make_app, make_services

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

    client = logged_in(app)  # raises server exceptions: none may escape the app
    response = client.get("/boom")
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


def test_every_response_carries_the_security_headers() -> None:
    client = logged_in(make_app())
    responses = [
        client.get("/players"),
        client.get("/login"),
        client.get("/static/app.css"),
        client.get("/players?pos=W"),  # a 400 page
        client.get("/nowhere"),  # a 404
    ]
    with TestClient(create_error_app("APP_PASSWORD not set")) as broken:
        responses.append(broken.get("/players"))
    for response in responses:
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "same-origin"
        assert response.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_the_error_app_answers_every_method(method: str) -> None:
    with TestClient(create_error_app("APP_PASSWORD not set")) as client:
        response = client.request(method, "/anything")
    assert response.status_code == 500


def test_an_oversized_body_is_refused_before_it_is_read() -> None:
    """M4R1A-9: Vercel refuses bodies over 4.5 MB; the app refuses them by their
    Content-Length, with a page, before a multipart parser spools them."""
    client = logged_in(make_app())
    big = b"x" * (MAX_BODY_BYTES + 1)
    response = client.post("/admin/sheet/upload", files={"file": ("sheet.xlsx", big)})
    assert response.status_code == 413
    assert "That upload is over 4 MB." in response.text
    assert 'href="/admin"' in response.text
    small = client.post("/refresh", data={"next": "/league"}, follow_redirects=False)
    assert small.status_code == 303


def test_services_are_built_on_the_first_request_without_a_lifespan() -> None:
    """M4R1A-10: a host that sends no lifespan events still gets working services,
    built once."""
    built: list[int] = []

    def factory(http: object) -> Services:
        built.append(1)
        return make_services()

    app = create_app(AppContext(SETTINGS, factory))
    client = TestClient(app)  # not entered: no lifespan
    login = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert login.status_code == 303
    assert client.get("/players").status_code == 200
    assert client.get("/league").status_code == 200
    assert built == [1]


def test_a_config_error_from_a_lazy_build_is_shown_once_and_kept() -> None:
    calls: list[int] = []

    def factory(http: object) -> Services:
        calls.append(1)
        raise ConfigError("LEAGUE_SHEET_ID needs FIRESTORE_SERVICE_ACCOUNT_JSON")

    client = TestClient(create_app(AppContext(SETTINGS, factory)))
    client.post("/login", data={"password": PASSWORD})
    for _ in range(2):
        response = client.get("/players")
        assert response.status_code == 500
        assert "LEAGUE_SHEET_ID needs FIRESTORE_SERVICE_ACCOUNT_JSON" in response.text
    assert calls == [1]


def test_the_stylesheet_keeps_table_headers_sticky_and_breakdowns_narrow() -> None:
    """M4R1A-3/B-3, B-10, A-12 (verified in a browser at 390 px; this guards the rules).
    A box that scrolls sideways is also the box sticky cells stick to, so it must
    scroll vertically too, within a screen's height."""
    css = (HERE / "static" / "app.css").read_text()
    wrap = re.search(r"\.table-wrap \{([^}]*)\}", css)
    assert wrap is not None
    assert "overflow: auto" in wrap[1]
    assert "max-height: calc(100dvh" in wrap[1]
    assert re.search(r"table\.data thead th \{[^}]*position: sticky; top: 0", css)
    assert re.search(r"dl\.norms \{[^}]*grid-template-columns: repeat\(2, auto\)", css)
    assert re.search(r"\.admin-inline \{[^}]*flex-wrap: wrap", css)
    assert "env(safe-area-inset-left)" in css
