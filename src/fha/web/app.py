"""The FastAPI app (SPEC §2, §7): auth, the common chrome, the PWA bits.

``create_app(context)`` builds the app. Routes reach what they need through
``request.app.state.context`` (an ``AppContext``); its ``services`` are
built in the lifespan, inside the running event loop, or on the first
request if the host never sent lifespan events. Pages render with
``render(request, template, **values)``, which fills the base layout's
values (active nav item, season label, last refreshed).

Errors: an unexpected exception becomes a page without details, and only
its type and the path are logged (the middleware answers it, so nothing
re-raises it to the server's traceback logger). No Yahoo data is a 503 page
with the reason; a query value a screen doesn't know is a 400 page.
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

from fha.web.auth import COOKIE, DEFAULT_NEXT, LoginThrottle, Sessions, password_ok, safe_next
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
# Vercel refuses request bodies over 4.5 MB; refuse them here too, before any is read.
MAX_BODY_BYTES = 4_500_000
SECURITY_HEADERS = {
    # Everything is served from this origin; htmx's inline indicator style is off.
    "Content-Security-Policy": (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
log = logging.getLogger(__name__)


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
    app = request.app
    context: AppContext = app.state.context
    if context.services is None and not app.state.built:
        _build(app)  # the host sent no lifespan events: build on the first request
    if context.services is None:
        raise ConfigError(app.state.config_error or "the app is not configured")
    return context.services


def _build(app: FastAPI) -> None:
    """Build the services once, inside the running event loop (their httpx client
    and locks bind to it). A ConfigError is kept for the error page; anything
    else propagates, and the next request tries again."""
    context: AppContext = app.state.context
    if context.services is None:
        from fha.sources.yahoo.client import make_http_client

        if app.state.http is None:
            app.state.http = make_http_client()
        try:
            context.services = context.factory(app.state.http)
        except ConfigError as e:
            app.state.config_error = str(e)
    app.state.built = True


def _active(path: str) -> str | None:
    """The nav item a path belongs to ("/rosters/replace" -> "rosters")."""
    first = path.strip("/").split("/", 1)[0]
    return first if first in dict(NAV) else None


def _referer_target(request: Request) -> str:
    """Where to return after logging in from a POST: the page it was sent from (same
    site only), since the POST's own path may have no GET (e.g. /refresh)."""
    from urllib.parse import urlsplit

    referer = urlsplit(request.headers.get("referer", ""))
    if referer.netloc != request.url.netloc:
        return DEFAULT_NEXT
    return safe_next(referer.path + (f"?{referer.query}" if referer.query else ""))


def create_app(context: AppContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        _build(app)
        try:
            yield
        finally:
            if app.state.http is not None:
                await app.state.http.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.context = context
    app.state.config_error = None
    app.state.built = False
    app.state.http = None
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
        if request.method in ("GET", "HEAD"):
            target = path + (f"?{request.url.query}" if request.url.query else "")
        else:
            target = _referer_target(request)
        login = f"/login?{_query(next=target)}"
        if request.headers.get("HX-Request"):  # htmx: redirect the whole page
            return Response(status_code=401, headers={"HX-Redirect": login})
        return RedirectResponse(login, status_code=303)

    @app.middleware("http")
    async def limit_body(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Refuse an oversized body by its Content-Length, before anything reads it."""
        length = request.headers.get("content-length")
        if length is not None and not _length_ok(length):
            return render(
                request,
                "bad_request.html",
                status_code=413,
                title="Too large",
                message=f"That upload is over {MAX_BODY_BYTES / 1_000_000:g} MB.",
                back="/admin" if path_is_admin(request.url.path) else None,
            )
        return await call_next(request)

    @app.middleware("http")
    async def unexpected_errors(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Answer an unexpected exception here, so it isn't re-raised to the server
        (which would log its traceback and message)."""
        try:
            return await call_next(request)
        except Exception as exc:
            log.error("unhandled %s on %s", type(exc).__name__, request.url.path)
            return render(
                request, "error.html", status_code=500, title="Something went wrong", message=None
            )

    _security_headers(app)

    @app.exception_handler(ConfigError)
    async def config_error(request: Request, exc: ConfigError) -> HTMLResponse:
        return render(
            request, "error.html", status_code=500, title="Not configured", message=str(exc)
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

    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException

    from fha.services.league_view import ViewError
    from fha.services.settings import SettingsError
    from fha.storage.repository import RepositoryError
    from fha.web.data import NO_DATA, BadQueryError, no_data_hint
    from fha.web.routes import admin, league, matchup, players, rosters

    async def no_data(request: Request, exc: Exception) -> HTMLResponse:
        unratable = isinstance(exc, ViewError)  # M4R11A-2: Yahoo answered; the data didn't fit
        return render(
            request,
            "error.html",
            status_code=503,
            active=_active(request.url.path),
            title="Yahoo data can't be rated" if unratable else "No Yahoo data",
            message=(
                f"Yahoo's data arrived but can't be rated ({exc})."
                if unratable
                else f"There's no Yahoo data to show ({type(exc).__name__}: {exc})."
            ),
            hint=no_data_hint(exc),
        )

    async def bad_query(request: Request, exc: Exception) -> HTMLResponse:
        active = _active(request.url.path)
        return render(
            request,
            "bad_request.html",
            status_code=400,
            active=active,
            title="Not understood",
            message=f"{str(exc)[:1].upper()}{str(exc)[1:]}.",
            back=f"/{active}" if active else None,
        )

    def chrome(request: Request) -> dict[str, str | None]:
        """The nav and Log out, for a signed-in viewer only (the error pages also
        answer public paths)."""
        if not sessions.valid(request.cookies.get(COOKIE)):
            return {}
        return {"active": _active(request.url.path)}

    async def bad_form(request: Request, exc: Exception) -> HTMLResponse:
        """A missing or mistyped form field: the 400 page, never FastAPI's JSON (which
        echoes the input back)."""
        active = _active(request.url.path)
        return render(
            request,
            "bad_request.html",
            status_code=400,
            **chrome(request),
            title="Not understood",
            message="A form value was missing or not what the page expected.",
            back=f"/{active}" if active else None,
        )

    async def http_error(request: Request, exc: Exception) -> HTMLResponse:
        """404, 405, a malformed body: the app's page, not FastAPI's JSON."""
        status = exc.status_code if isinstance(exc, HTTPException) else 500
        headers = exc.headers if isinstance(exc, HTTPException) else None
        from http import HTTPStatus

        response = render(
            request,
            "bad_request.html",
            status_code=status,
            **chrome(request),
            title=HTTPStatus(status).phrase,
            message={
                404: "There's no such page.",
                405: "That page can't be used that way.",
            }.get(status, "The request wasn't one the app understands."),
            back=None,
        )
        response.headers.update(headers or {})
        return response

    async def bad_settings(request: Request, exc: Exception) -> HTMLResponse:
        return render(
            request,
            "error.html",
            status_code=500,
            active=_active(request.url.path),
            title="Rating settings invalid",
            message="The stored rating settings are invalid, so nothing can be rated.",
            hint="Fix them in Admin, under Rating settings.",
            link=("/admin#settings", "Open the rating settings"),
        )

    async def store_down(request: Request, exc: Exception) -> HTMLResponse:
        summary = exc.summary if isinstance(exc, RepositoryError) else type(exc).__name__
        log.error("storage failed (%s) on %s", summary, request.url.path)
        return render(
            request,
            "error.html",
            status_code=503,
            active=_active(request.url.path),
            title="Storage unavailable",
            message="Reading or writing the app's storage failed.",
            hint="Try again in a minute.",
        )

    for kind in NO_DATA:
        app.add_exception_handler(kind, no_data)
    app.add_exception_handler(BadQueryError, bad_query)
    app.add_exception_handler(RequestValidationError, bad_form)
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(SettingsError, bad_settings)
    app.add_exception_handler(RepositoryError, store_down)
    for module in (players, rosters, league, matchup, admin):
        app.include_router(module.router)
    return app


def _security_headers(app: FastAPI) -> None:
    """Added last, so outermost: every response carries them, error pages too."""

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response


def _length_ok(length: str) -> bool:
    """A Content-Length within the limit (digits only; never int() of a huge string)."""
    return (
        length.isascii()
        and length.isdigit()
        and len(length) <= 12
        and int(length) <= MAX_BODY_BYTES
    )


def path_is_admin(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


def _query(**params: str) -> str:
    from urllib.parse import urlencode

    return urlencode(params)


def create_error_app(message: str) -> FastAPI:
    """An app that answers every request with the configuration error (names only)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    _security_headers(app)

    @app.api_route("/{path:path}", methods=ALL_METHODS)
    async def anything(request: Request, path: str) -> HTMLResponse:
        return render(
            request, "error.html", status_code=500, title="Not configured", message=message
        )

    return app
