"""Formatting and URL helpers for the screens' templates (installed on import).

Unknown money is always "—", never $0 (SPEC §5).
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlencode

from jinja2 import Environment

from fha.domain.engine import DivisorMethod, EngineConfig

DASH = "—"


def money(amount: float | None) -> str:
    """$7.25M; "—" when unknown; a minus sign for negative room."""
    if amount is None:
        return DASH
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount) / 1_000_000:.2f}M"


def number(value: float | None, digits: int = 2) -> str:
    return DASH if value is None else f"{value:.{digits}f}"


def signed(value: float | None, digits: int = 2) -> str:
    return DASH if value is None else f"{value:+.{digits}f}"


def percentile(value: float | None) -> str:
    return DASH if value is None else f"{value:.0f}"


def rating_settings(config: EngineConfig) -> str:
    """The settings in use, next to the season label (SPEC §7.5)."""
    method = "workbook" if config.divisor_method is DivisorMethod.WORKBOOK else "per-82"
    floor = f"{config.gp_floor_fraction * 100:g}%"
    return f"{method} top-{config.top_n}, floor {floor}"


def url(path: str, params: Mapping[str, str], **changes: str | None) -> str:
    """``path`` with ``params`` changed; a None or "" value drops the parameter."""
    merged = {k: v for k, v in params.items() if v}
    for key, value in changes.items():
        if value:
            merged[key] = value
        else:
            merged.pop(key, None)
    query = urlencode(merged)
    return f"{path}?{query}" if query else path


def install(env: Environment) -> None:
    env.filters.update(money=money, number=number, signed=signed, pctl=percentile)
    env.globals.update(url=url, rating_settings=rating_settings, DASH=DASH)


def _install_on_app_templates() -> None:
    from fha.web.app import templates

    install(templates.env)


_install_on_app_templates()
