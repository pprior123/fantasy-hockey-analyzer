"""Single-password login, the signed session cookie, redirects and throttling (SPEC §7)."""

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from fha.web.auth import COOKIE, LoginThrottle, Sessions, password_ok, safe_next
from tests.unit.web.helpers import PASSWORD, FakeClock, logged_in, make_app, make_services


def test_an_unauthenticated_page_redirects_to_login_with_next() -> None:
    with TestClient(make_app()) as client:
        response = client.get("/players?sort=gp&team=1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fplayers%3Fsort%3Dgp%26team%3D1"


def test_an_unauthenticated_htmx_request_gets_hx_redirect() -> None:
    with TestClient(make_app()) as client:
        response = client.get("/rosters", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login?next=%2Frosters"


def test_the_login_page_is_public_and_has_no_nav() -> None:
    with TestClient(make_app()) as client:
        response = client.get("/login?next=/league")
    assert response.status_code == 200
    assert 'name="password"' in response.text
    assert 'value="/league"' in response.text
    assert 'class="topnav"' not in response.text
    assert "Fantasy data provided by Yahoo Fantasy" in response.text


def test_the_right_password_sets_a_hardened_cookie_and_goes_next() -> None:
    with TestClient(make_app(secure_cookies=True)) as client:
        response = client.post(
            "/login", data={"password": PASSWORD, "next": "/league"}, follow_redirects=False
        )
    assert (response.status_code, response.headers["location"]) == (303, "/league")
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=34560000"):
        assert flag in cookie


def test_a_logged_in_client_can_see_pages() -> None:
    client = logged_in(make_app())
    assert client.get("/league", follow_redirects=False).status_code == 200


def test_a_wrong_password_is_refused_without_a_cookie() -> None:
    with TestClient(make_app()) as client:
        response = client.post("/login", data={"password": "nope", "next": "/players"})
        assert response.status_code == 401
        assert "Wrong password." in response.text
        assert "set-cookie" not in response.headers
        assert client.get("/players", follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    "target",
    ["//evil.example/x", "https://evil.example/", "/\\evil.example", "players", "", "/a\tb"],
)
def test_next_never_leaves_the_site(target: str) -> None:
    assert safe_next(target) == "/players"
    with TestClient(make_app()) as client:
        response = client.post(
            "/login", data={"password": PASSWORD, "next": target}, follow_redirects=False
        )
    assert response.headers["location"] == "/players"


def test_safe_next_keeps_a_local_path_with_its_query() -> None:
    assert safe_next("/rosters?team=3") == "/rosters?team=3"
    assert safe_next(None) == "/players"


def test_a_tampered_or_foreign_cookie_is_refused() -> None:
    client = logged_in(make_app())
    token = client.cookies[COOKIE]
    client.cookies.set(COOKIE, token[:-2] + ("AA" if not token.endswith("AA") else "BB"))
    assert client.get("/players", follow_redirects=False).status_code == 303
    other = Sessions("another secret" * 3, 3600).issue()
    client.cookies.set(COOKIE, other)
    assert client.get("/players", follow_redirects=False).status_code == 303


def test_sessions_expire_and_reject_other_payloads() -> None:
    sessions = Sessions("x" * 32, 3600)
    token = sessions.issue()
    assert sessions.valid(token)
    assert not Sessions("x" * 32, -1).valid(token)  # older than the maximum age
    assert not sessions.valid(None)
    assert not sessions.valid("")
    from itsdangerous import URLSafeTimedSerializer

    forged = URLSafeTimedSerializer("x" * 32, salt="fha-session-v1").dumps({"v": 2})
    assert not sessions.valid(forged)


def test_logout_ends_the_session() -> None:
    client = logged_in(make_app())
    response = client.post("/logout", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login")
    assert client.get("/players", follow_redirects=False).status_code == 303


def test_too_many_failures_block_logins_until_the_window_passes() -> None:
    clock = FakeClock()
    with TestClient(make_app(make_services(clock))) as client:
        for _ in range(5):
            assert client.post("/login", data={"password": "bad"}).status_code == 401
        blocked = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert blocked.status_code == 429
        assert "Too many attempts" in blocked.text
        clock.t += 60
        ok = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert ok.status_code == 303


def test_the_throttle_counts_only_recent_failures() -> None:
    throttle = LoginThrottle(max_failures=2, window=10)
    throttle.failed(0)
    assert not throttle.blocked(1)
    throttle.failed(5)
    assert throttle.blocked(9.9)
    assert not throttle.blocked(10)  # the first failure has aged out


def test_passwords_compare_exactly() -> None:
    assert password_ok("abc", "abc")
    assert not password_ok("abc", "abcd")
    assert not password_ok("ábc", "abc")


def test_the_password_never_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client = logged_in(make_app())
    client.post("/login", data={"password": "wrong-guess-123"})
    assert PASSWORD not in caplog.text
    assert "wrong-guess-123" not in caplog.text


def test_a_post_after_the_session_expired_returns_to_the_page_it_came_from() -> None:
    """M4R1A-7: /refresh has no GET, so ``next`` is the Referer (same site only)."""
    with TestClient(make_app()) as anonymous:
        here = anonymous.post(
            "/refresh",
            data={"next": "/rosters"},
            headers={"referer": "http://testserver/rosters?season=last"},
            follow_redirects=False,
        )
        foreign = anonymous.post(
            "/refresh",
            headers={"referer": "https://evil.example/rosters"},
            follow_redirects=False,
        )
        none = anonymous.post("/admin/aav", follow_redirects=False)
    assert here.headers["location"] == "/login?next=%2Frosters%3Fseason%3Dlast"
    assert foreign.headers["location"] == "/login?next=%2Fplayers"
    assert none.headers["location"] == "/login?next=%2Fplayers"


def api_routes(routes: Any) -> Iterator[APIRoute]:
    """Every APIRoute, including those of included routers (the routers carry no prefix)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from api_routes(route.original_router.routes)


def test_every_route_but_the_public_ones_needs_a_login() -> None:
    """Swept from the app's own routes, POSTs included, so a new route can't slip by."""
    app = make_app()
    public = {"/login", "/manifest.webmanifest"}
    checked = 0
    with TestClient(app) as anonymous:
        for route in api_routes(app.routes):
            if route.path in public:
                continue
            for method in route.methods:
                response = anonymous.request(method, route.path, follow_redirects=False)
                assert response.status_code == 303, (method, route.path)
                assert response.headers["location"].startswith("/login?next="), route.path
                htmx = anonymous.request(
                    method, route.path, headers={"HX-Request": "true"}, follow_redirects=False
                )
                assert htmx.status_code == 401
                checked += 1
    assert checked >= 16
