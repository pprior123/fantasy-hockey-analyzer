"""Formatting and URL helpers for the screens' templates (installed on import).

Unknown money is always "—", never $0 (SPEC §5). Rounding is half-up in
decimal, so $1,125,000 is $1.13M like $925,000 is $0.925M; nothing rounds
through binary floats.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

from jinja2 import Environment

from fha.domain.engine import DivisorMethod, EngineConfig

DASH = "—"


def _round(value: float | Decimal, digits: int) -> Decimal:
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def money(amount: float | None) -> str:
    """$7.25M from $1M up, $0.925M from $1,000, else whole dollars ($400, $0);
    "—" when unknown; a minus sign for negative room. A nonzero amount never
    shows as zero, so a team $400 over the cap reads "-$400", not "-$0.00M"."""
    if amount is None or not math.isfinite(amount):
        return DASH
    sign = "-" if amount < 0 else ""
    dollars = abs(Decimal(str(amount)))
    if dollars >= 999_500:  # rounds to $1.000M or more: two decimals, like $1.00M
        return f"{sign}${_round(dollars / 1_000_000, 2)}M"
    if dollars >= 1_000:
        return f"{sign}${_round(dollars / 1_000_000, 3)}M"
    whole = _round(dollars, 0)
    return f"{sign if whole else ''}${whole}"


def number(value: float | None, digits: int = 2) -> str:
    if value is None or not math.isfinite(value):
        return DASH
    rounded = _round(value, digits)
    return f"{abs(rounded) if rounded == 0 else rounded:f}"  # no "-0.00"


def signed(value: float | None, digits: int = 2) -> str:
    if value is None or not math.isfinite(value):
        return DASH
    rounded = _round(value, digits)
    return f"{'-' if rounded < 0 or (rounded == 0 and value < 0) else '+'}{abs(rounded):f}"


def percentile(value: float | None) -> str:
    return number(value, 0)


def ago(seconds: float) -> str:
    """How old something is ("just now", "12 min ago", "3 h ago", "2 days ago")."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"


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
    env.filters.update(money=money, number=number, signed=signed, pctl=percentile, ago=ago)
    env.globals.update(url=url, rating_settings=rating_settings, DASH=DASH)


def _install_on_app_templates() -> None:
    from fha.web.app import templates

    install(templates.env)


_install_on_app_templates()
