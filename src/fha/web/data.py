"""What every rated screen needs, loaded once per request (SPEC §2, §5, §7).

``page_data`` serves the cached snapshot (refreshing it when stale, or when
the Refresh button forces it), reads the rating settings and the salaries,
and rates the chosen season. The labels say which season is shown and how
fresh the data is; the UI always shows both (SPEC §5).

Without Yahoo data (no cache and a failed refresh) or with a snapshot that
can't be rated, ``page_data`` raises one of ``NO_DATA``; the app shows that
as a "no Yahoo data" page, and Admin keeps working around it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Request

from fha.domain.engine import EngineConfig
from fha.services.league_sheet import TabReport
from fha.services.league_view import LeagueView, Season, ViewError, build_view, load_salaries
from fha.services.refresh import Cached, RefreshError
from fha.services.settings import load_rating_settings
from fha.web.app import services
from fha.web.format import ago

NO_DATA = (RefreshError, ViewError)
RETRY_NOTE = "Yahoo is asked again a minute after a failure."
SIGN_IN_NOTE = "The app's Yahoo sign-in may need renewing (the consent flow)."
log = logging.getLogger(__name__)


def failure_kind(note: str) -> str:
    """The failed error's type, which a refresh note starts with (``refresh._describe``).
    The notes say what failed, not why (M4R9A-1)."""
    return note.removeprefix("refresh failed: ").split(":", 1)[0].split(" (", 1)[0]


def no_data_hint(exc: Exception) -> str:
    """What the no-data page says happens next (M4R10A-1)."""
    if isinstance(exc, ViewError):
        return "Admin works without it."
    hint = f"Admin works without it. {RETRY_NOTE}"
    return f"{hint} {SIGN_IN_NOTE}" if failure_kind(str(exc)) == "YahooAuthError" else hint


class BadQueryError(ValueError):
    """A query-string value a screen doesn't know; the app shows a 400 page."""


@dataclass(frozen=True)
class PageData:
    view: LeagueView
    cached: Cached
    reports: list[TabReport]
    config: EngineConfig
    now: float

    @property
    def season_label(self) -> str:
        """The season shown, e.g. "Last season (2025-26)"."""
        year = self.view.season_year
        which = "This season" if self.view.season is Season.CURRENT else "Last season"
        return f"{which} ({year}-{(year + 1) % 100:02d})"

    @property
    def refreshed_label(self) -> str:
        """How old the data is ("just now", "12 min ago", "3 h ago")."""
        return ago(self.now - self.cached.fetched_at)

    @property
    def stale_note(self) -> str | None:
        """The refresh note in the owner's words (SPEC §2; DECISIONS, rounds 2-3)."""
        error = self.cached.refresh_error
        if error is None:
            return None
        if error.startswith("not saved:"):
            return "Fresh from Yahoo, but it couldn't be saved; it will be retried."
        kind = failure_kind(error)
        note = (
            f"Refreshing from Yahoo failed ({kind}), so this is data from {self.refreshed_label}."
        )
        if kind == "YahooAuthError":
            note += f" {SIGN_IN_NOTE}"
        note += f" {RETRY_NOTE}"  # a Refresh within the minute won't ask Yahoo (M4R11A-3)
        return note


def season_param(raw: str | None) -> Season | None:
    """The season toggle from the URL: "current", "last", or blank for the default."""
    try:
        return Season(raw) if raw else None
    except ValueError:
        raise BadQueryError(f"season must be current or last, got {_shown(raw)!r}") from None


VIEWS = ("money", "cats")  # the Categories toggle (SPEC §7): salary columns, or the 7 norms


def view_param(raw: str | None) -> str:
    """The Categories toggle from the URL: "cats", or blank for the salary columns."""
    mode = raw or "money"
    if mode not in VIEWS:
        raise BadQueryError(f"view must be cats or left out, got {_shown(raw)!r}")
    return mode


def _shown(raw: str | None) -> str | None:
    """A bad value as the 400 page quotes it: at most 12 characters."""
    return raw if raw is None or len(raw) <= 12 else raw[:12] + "…"


async def page_data(
    request: Request, *, season: Season | None = None, force: bool = False
) -> PageData:
    svc = services(request)
    settings = request.app.state.context.settings
    if svc.prepare is not None:
        try:
            await svc.prepare()
        except Exception as e:  # optional (the demo's salaries): Admin must keep working
            log.warning("demo salaries not stored: %s", type(e).__name__)
    cached = await svc.refresh.current(force=force)
    config = await load_rating_settings(svc.repo)
    salaries, reports = await load_salaries(
        svc.repo, cached.snapshot, cap_override=settings.salary_cap
    )
    if season is Season.LAST and cached.snapshot.last_season_stats is None:
        season = None
    view = build_view(
        cached.snapshot, config, salaries, season=season, baseline_min_gp=settings.baseline_min_gp
    )
    return PageData(view, cached, reports, config, svc.clock.now())
