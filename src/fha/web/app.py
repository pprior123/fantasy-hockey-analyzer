"""The FastAPI app (SPEC §2, §7): auth, the common chrome, the PWA bits.

``create_app(context)`` builds the app. Routes reach what they need through
``request.app.state.context`` (an ``AppContext``); its ``services`` are
built in the lifespan, inside the running event loop. Pages render with
``render(request, template, **values)``, which fills the base layout's
values (active nav item, season label, last refreshed).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fha.web.auth import COOKIE, LoginThrottle, Sessions, password_ok, safe_next
from fha.web.context import AppContext, ConfigError, Services

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

NAV = (
    ("players", "Players"),
    ("rosters", "Rosters"),
    ("league", "League"),
    ("matchup", "Matchup"),
    ("admin", "Admin"),
)
PUBLIC = ("/login", "/manifest.webmanifest")
THEME = "#0b3d91"
MANIFEST = {
    "name": "Fantasy Hockey Analyzer",
    "short_name": "FHA",
    "start_url": "/players",
    "scope": "/",
    "display": "standalone",
    "background_color": "#ffffff",
    "theme_color": THEME,
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}
QUIET_LOGGERS = ("httpx", "httpcore")  # at INFO they log URLs with the sheet ID and project


def configure_logging() -> None:
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def render(
    request: Request, template: str, *, status_code: int = 200, **values: Any
) -> HTMLResponse:
    """A page in the base layout. ``values`` may set ``active`` (a nav key),
    ``title``, ``season_label`` and ``refreshed_at`` (text for the header)."""
    return templates.TemplateResponse(
        request, template, {"nav": NAV, **values}, status_code=status_code
    )


def services(request: Request) -> Services:
    """The running app's services (raises ConfigError if they couldn't be built)."""
    context: AppContext = request.app.state.context
    if context.services is None:
        raise ConfigError(request.app.state.config_error or "the app is not configured")
    return context.services


def create_app(context: AppContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        http = None
        if context.services is None:
            from fha.sources.yahoo.client import make_http_client

            http = make_http_client()
            try:
                context.services = context.factory(http)
            except ConfigError as e:
                app.state.config_error = str(e)
        try:
            yield
        finally:
            if http is not None:
                await http.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.context = context
    app.state.config_error = None
    sessions = Sessions(context.settings.session_secret, context.settings.session_days * 86400)
    throttle = LoginThrottle()
    app.state.sessions = sessions
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def now() -> float:
        return context.services.clock.now() if context.services else time.time()

    @app.middleware("http")
    async def require_login(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if path in PUBLIC or path.startswith("/static/"):
            return await call_next(request)
        if sessions.valid(request.cookies.get(COOKIE)):
            return await call_next(request)
        target = path + (f"?{request.url.query}" if request.url.query else "")
        login = f"/login?{_query(next=target)}"
        if request.headers.get("HX-Request"):  # htmx: redirect the whole page
            return Response(status_code=401, headers={"HX-Redirect": login})
        return RedirectResponse(login, status_code=303)

    @app.exception_handler(ConfigError)
    async def config_error(request: Request, exc: ConfigError) -> HTMLResponse:
        return render(
            request, "error.html", status_code=500, title="Not configured", message=str(exc)
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> HTMLResponse:
        logging.getLogger(__name__).error(
            "unhandled %s on %s", type(exc).__name__, request.url.path
        )
        return render(
            request, "error.html", status_code=500, title="Something went wrong", message=None
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str | None = None) -> HTMLResponse:
        return render(request, "login.html", title="Log in", next=safe_next(next))

    @app.post("/login", response_model=None)
    async def login(request: Request, password: str = Form(""), next: str = Form("")) -> Response:
        target = safe_next(next)
        if throttle.blocked(now()):
            return render(
                request,
                "login.html",
                status_code=429,
                title="Log in",
                next=target,
                error="Too many attempts. Wait a minute and try again.",
            )
        if not password_ok(password, context.settings.app_password):
            throttle.failed(now())
            return render(
                request,
                "login.html",
                status_code=401,
                title="Log in",
                next=target,
                error="Wrong password.",
            )
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            COOKIE,
            sessions.issue(),
            max_age=sessions.max_age,
            httponly=True,
            secure=context.settings.secure_cookies,
            samesite="lax",
        )
        return response

    @app.post("/logout")
    async def logout() -> RedirectResponse:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    @app.get("/")
    async def home() -> RedirectResponse:
        return RedirectResponse("/players", status_code=303)

    @app.get("/manifest.webmanifest")
    async def manifest() -> Response:
        return Response(json.dumps(MANIFEST), media_type="application/manifest+json")

    from fha.web.routes import admin, league, matchup, players, rosters

    for module in (players, rosters, league, matchup, admin):
        app.include_router(module.router)
    return app


def _query(**params: str) -> str:
    from urllib.parse import urlencode

    return urlencode(params)


def create_error_app(message: str) -> FastAPI:
    """An app that answers every request with the configuration error (names only)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def anything(request: Request, path: str) -> HTMLResponse:
        return render(
            request, "error.html", status_code=500, title="Not configured", message=message
        )

    return app
