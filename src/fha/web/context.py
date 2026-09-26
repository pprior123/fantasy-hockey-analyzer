"""What the routes need, and how the environment wires it (SPEC §2, §8).

``AppContext`` holds the settings and a factory for the services. The
services (repository, refresh service, league sheet) own httpx clients and
``asyncio.Lock``s, which bind to the event loop that first uses them, so the
factory runs inside the running app (its lifespan), not at import
(DECISIONS, "M3 review round 1", For M4). Tests pass ready-made services.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from fha.services.clock import Clock, SystemClock
from fha.services.refresh import DEFAULT_TTL_SECONDS, RefreshService
from fha.sources.league_sheet.source import LeagueSheetSource
from fha.storage.repository import Repository

DEFAULT_BASELINE_MIN_GP = 10  # SPEC §5, baseline season
SESSION_DAYS = 400  # browsers cap cookie lifetimes around here


class ConfigError(Exception):
    """The environment is missing something the app needs. Names variables, never values."""


@dataclass(frozen=True)
class Settings:
    app_password: str
    session_secret: str
    secure_cookies: bool = True  # False only in local dev (plain http://127.0.0.1)
    session_days: int = SESSION_DAYS
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    baseline_min_gp: int = DEFAULT_BASELINE_MIN_GP
    demo: bool = False

    def __repr__(self) -> str:  # the password and secret never reach a log
        return f"Settings(demo={self.demo}, secure_cookies={self.secure_cookies})"

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        missing = [n for n in ("APP_PASSWORD", "SESSION_SECRET") if not environ.get(n)]
        if missing:
            raise ConfigError(f"{' and '.join(missing)} not set")
        return cls(
            app_password=environ["APP_PASSWORD"],
            session_secret=environ["SESSION_SECRET"],
            secure_cookies=environ.get("FHA_INSECURE_COOKIES") != "1",
            ttl_seconds=_number(environ, "CACHE_TTL_MINUTES", DEFAULT_TTL_SECONDS / 60) * 60,
            baseline_min_gp=int(_number(environ, "BASELINE_MIN_GP", DEFAULT_BASELINE_MIN_GP)),
            demo=environ.get("FHA_DEMO") == "1",
        )


def _number(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None
    if value < 0:
        raise ConfigError(f"{name} must be >= 0")
    return value


@dataclass
class Services:
    repo: Repository
    refresh: RefreshService
    clock: Clock
    sheet: LeagueSheetSource | None = None  # None: no league sheet configured


ServicesFactory = Callable[[httpx.AsyncClient], Services]


@dataclass
class AppContext:
    settings: Settings
    factory: ServicesFactory
    services: Services | None = None  # built in the app's lifespan

    @classmethod
    def with_services(cls, settings: Settings, services: Services) -> AppContext:
        """Ready-made services (tests)."""
        return cls(settings, lambda http: services, services)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> AppContext:
        settings = Settings.from_env(environ)
        env = dict(environ)
        if settings.demo:
            return cls(settings, lambda http: demo_services(env, http, settings))
        _require_yahoo(env)
        return cls(settings, lambda http: production_services(env, http, settings))


def _require_yahoo(environ: Mapping[str, str]) -> None:
    missing = [n for n in ("YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET") if not environ.get(n)]
    if missing:
        raise ConfigError(f"{' and '.join(missing)} not set (or set FHA_DEMO=1 for demo data)")


def league_sheet_from_env(
    environ: Mapping[str, str], http: httpx.AsyncClient
) -> LeagueSheetSource | None:
    """The live sheet (``LEAGUE_SHEET_ID``), a downloaded one (``LEAGUE_SHEET_XLSX``), or None."""
    from fha.sources.league_sheet.source import SheetsApiLeagueSheet, XlsxLeagueSheet

    if environ.get("LEAGUE_SHEET_ID"):
        from fha.sources.google_auth import GoogleAuthError, ServiceAccountTokens
        from fha.sources.league_sheet.sheets_api import SCOPE

        key = environ.get("FIRESTORE_SERVICE_ACCOUNT_JSON")
        if not key:
            raise ConfigError("LEAGUE_SHEET_ID needs FIRESTORE_SERVICE_ACCOUNT_JSON")
        try:
            tokens = ServiceAccountTokens(key, [SCOPE], http)
        except GoogleAuthError as e:
            raise ConfigError(f"FIRESTORE_SERVICE_ACCOUNT_JSON: {e}") from None
        return SheetsApiLeagueSheet(http, environ["LEAGUE_SHEET_ID"], tokens)
    if environ.get("LEAGUE_SHEET_XLSX"):
        return XlsxLeagueSheet(Path(environ["LEAGUE_SHEET_XLSX"]))
    return None


def production_services(
    environ: Mapping[str, str], http: httpx.AsyncClient, settings: Settings
) -> Services:
    from fha.sources.yahoo.client import YahooClient
    from fha.sources.yahoo.oauth import Credentials
    from fha.sources.yahoo.source import HttpYahooSource
    from fha.storage.factory import repository_from_env
    from fha.storage.repository import RepositoryError
    from fha.storage.tokens import RepositoryTokenStore

    try:
        repo = repository_from_env(environ, http)
    except RepositoryError as e:
        raise ConfigError(str(e)) from None
    clock = SystemClock()
    creds = Credentials(environ["YAHOO_CLIENT_ID"], environ["YAHOO_CLIENT_SECRET"])
    client = YahooClient(http, creds, RepositoryTokenStore(repo), clock=clock.now)
    refresh = RefreshService(HttpYahooSource(client), repo, clock, ttl_seconds=settings.ttl_seconds)
    return Services(repo, refresh, clock, league_sheet_from_env(environ, http))


def demo_services(
    environ: Mapping[str, str], http: httpx.AsyncClient, settings: Settings
) -> Services:
    """The synthetic league (``FHA_DEMO=1``): no Yahoo, no cloud. Storage is the dev
    file if ``FHA_LOCAL_REPOSITORY`` is set, else in memory (lost on restart)."""
    from fha.sources.yahoo.demo import demo_snapshot
    from fha.sources.yahoo.fake import FakeYahooSource
    from fha.storage.memory import InMemoryRepository
    from fha.storage.repository import RepositoryError

    repo: Repository
    if environ.get("FHA_LOCAL_REPOSITORY"):
        from fha.storage.local_json import LocalJsonRepository

        try:
            repo = LocalJsonRepository(Path(environ["FHA_LOCAL_REPOSITORY"]), environ)
        except RepositoryError as e:
            raise ConfigError(str(e)) from None
    else:
        repo = InMemoryRepository()
    clock = SystemClock()
    refresh = RefreshService(
        FakeYahooSource(demo_snapshot()), repo, clock, ttl_seconds=settings.ttl_seconds
    )
    return Services(repo, refresh, clock, league_sheet_from_env(environ, http))
