"""The common chrome, placeholders, PWA files, errors and logging (SPEC §7)."""

import json
import logging
import re
import struct
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fha.services.refresh import RefreshService
from fha.sources.yahoo.demo import demo_snapshot
from fha.sources.yahoo.fake import FakeYahooSource
from fha.storage.memory import InMemoryRepository
from fha.storage.repository import RepositoryError
from fha.web.app import (
    HERE,
    MAX_BODY_BYTES,
    NAV,
    _length_ok,
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
        assert response.headers["content-security-policy"] == (
            "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'; object-src 'none'"
        )
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
    assert "That upload is over 4.5 MB." in response.text
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


@pytest.mark.parametrize(
    ("length", "ok"),
    [
        ("0", True),
        (str(MAX_BODY_BYTES), True),
        (str(MAX_BODY_BYTES + 1), False),
        ("9" * 5000, False),
        ("12a", False),
        ("١٢", False),
        ("", False),
    ],
)
def test_content_length_checks_never_parse_a_huge_number(length: str, ok: bool) -> None:
    """M4R2A-2: int() of a 5000-digit Content-Length raised (a 500, not a 413)."""
    assert _length_ok(length) is ok


def test_a_huge_content_length_is_a_413_not_a_500() -> None:
    import asyncio

    app = make_app()
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/login",
        "raw_path": b"/login",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"content-length", b"9" * 5000)],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    assert sent[0]["status"] == 413


def test_a_failed_lazy_build_is_retried_on_the_next_request() -> None:
    """M4R2A-3: an error other than ConfigError used to leave the instance broken."""
    attempts: list[int] = []

    def factory(http: object) -> Services:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("disk hiccup")
        return make_services()

    client = TestClient(create_app(AppContext(SETTINGS, factory)))
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert client.get("/players").status_code == 500  # the page without details
    first = client.app.state.http  # type: ignore[attr-defined]
    assert client.get("/players").status_code == 200
    assert client.app.state.http is first  # one client, not a new one per attempt
    assert client.get("/league").status_code == 200
    assert attempts == [1, 1]


def test_a_storage_outage_is_a_503_page_without_details(caplog: pytest.LogCaptureFixture) -> None:
    """M4R2B-3: Firestore down (every read fails) on a rated screen or Refresh."""

    class Down(InMemoryRepository):
        down = False

        async def get(self, collection: str, doc_id: str) -> Any:
            if self.down:
                raise RepositoryError("Firestore get x: HTTP 503 (projects/secret-project)")
            return await super().get(collection, doc_id)

    services = make_services()
    repo = Down()
    services = replace(
        services,
        repo=repo,
        refresh=RefreshService(FakeYahooSource(demo_snapshot()), repo, services.clock),
    )
    client = logged_in(make_app(services))
    assert client.get("/players").status_code == 200
    repo.down = True
    for response in (client.get("/players"), client.post("/refresh", data={"next": "/league"})):
        assert response.status_code == 503
        assert "The app&#39;s storage couldn&#39;t be reached." in response.text
        assert "secret-project" not in response.text
    assert "storage failed (RepositoryError) on /players" in caplog.text
    assert "secret-project" not in caplog.text


def test_damaged_rating_settings_point_every_rated_screen_at_admin() -> None:
    """M4R2A-1: every rated screen was a bare 500."""
    import asyncio

    from fha.services.settings import COLLECTION, RATING

    services = make_services()
    asyncio.run(services.repo.put(COLLECTION, RATING, {"gp_floor_fraction": 7}))
    client = logged_in(make_app(services))
    for path in ("/players", "/rosters", "/league", "/matchup", "/matchup/free-agents"):
        response = client.get(path)
        assert response.status_code == 500, path
        assert "The stored rating settings are invalid" in response.text
        assert 'href="/admin#settings"' in response.text
    assert client.get("/admin").status_code == 200


def test_stored_settings_with_fewer_categories_still_render_every_screen() -> None:
    """M4R4B-1: the engine accepts a category subset (only a store edit can set one),
    but the team profiles used all 7 and raised on the missing norms."""
    import asyncio

    from fha.services.settings import COLLECTION, RATING

    services = make_services()
    asyncio.run(services.repo.put(COLLECTION, RATING, {"categories": ["G", "A"]}))
    client = logged_in(make_app(services))
    for path in ("/players", "/rosters", "/league", "/matchup", "/matchup/free-agents"):
        for view in ("money", "cats"):
            assert client.get(f"{path}?view={view}").status_code == 200, (path, view)


@pytest.mark.parametrize(
    ("method", "path", "kw", "status", "text"),
    [
        ("GET", "/nowhere", {}, 404, "There&#39;s no such page."),
        ("GET", "/refresh", {}, 405, "That page can&#39;t be used that way."),
        (
            "POST",
            "/refresh",
            {"content": b"--x\r\nbroken", "headers": {"content-type": "multipart/form-data"}},
            400,
            "The request wasn&#39;t one the app understands.",
        ),
    ],
)
def test_http_errors_are_the_apps_pages_not_json(
    method: str, path: str, kw: dict[str, Any], status: int, text: str
) -> None:
    """M4R3B-3: FastAPI's own 404 / 405 / bad-body answers were JSON."""
    response = logged_in(make_app()).request(method, path, **kw)
    assert response.status_code == status
    assert response.headers["content-type"].startswith("text/html")
    assert text in response.text
    if status == 405:
        assert response.headers["allow"] == "POST"
    assert "Log out" in response.text


@pytest.mark.parametrize(("method", "path"), [("GET", "/static/nope.css"), ("PUT", "/login")])
def test_an_error_page_for_a_visitor_has_no_nav(method: str, path: str) -> None:
    """M4R4A-4: public paths reach the error pages without a login."""
    response = TestClient(make_app()).request(method, path)
    assert response.status_code in (404, 405)
    assert "Log out" not in response.text
    assert 'class="topnav"' not in response.text


def test_a_bad_login_form_from_a_visitor_has_no_nav() -> None:
    """M4R5A-1: a file posted as the password is the 400 page, still without the nav."""
    response = TestClient(make_app()).post("/login", files={"password": ("a.txt", b"x")})
    assert response.status_code == 400
    assert "Log out" not in response.text
    assert 'class="topnav"' not in response.text
