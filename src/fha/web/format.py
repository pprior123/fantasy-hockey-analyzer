"""Formatting and URL helpers for the screens' templates (installed on import).

Unknown money is always "—", never $0 (SPEC §5). Rounding is half-up in
decimal, so $1,125,000 is $1.13M like $925,000 is $0.925M; nothing rounds
through binary floats.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
from urllib.parse import urlencode

from jinja2 import Environment
from markupsafe import Markup

from fha.domain.engine import DivisorMethod, EngineConfig
from fha.domain.lineup import SCALE
from fha.domain.schedule import Mark
from fha.services.schedule import EASTERN, Tonight

DASH = "—"
# Fixed names, not strftime: the server's locale must not change the screens.
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


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
    if dollars >= 999.5:  # rounds to $1,000 or more
        return f"{sign}${_round(dollars / 1_000_000, 3)}M"
    whole = _round(dollars, 0)
    return f"{sign if whole else ''}${whole}"


def number(value: float | None, digits: int = 2) -> str:
    if value is None or not math.isfinite(value):
        return DASH
    rounded = _round(value, digits)
    return f"{abs(rounded) if rounded == 0 else rounded:f}"  # no "-0.00"


def up(value: float | None, digits: int = 2) -> str:
    """Rounded away from zero, so a positive gap never shows as 0 ("0.01", not "0.00").
    Float noise is settled first: 0.1 + 0.2 shows as 0.30, not 0.31."""
    if value is None or not math.isfinite(value):
        return DASH
    settled = _round(value, 9)  # unless that hides a tiny gap
    base = settled if settled != 0 else Decimal(str(value))
    return f"{base.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_UP):f}"


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


def game_label(assignment: Mark | None) -> str | None:
    if assignment is None:
        return None
    venue = "@" if assignment.venue == "at" else "vs "
    opponent = venue + assignment.opponent
    if assignment.start_utc is None:
        return opponent
    try:
        start = datetime.fromtimestamp(assignment.start_utc, EASTERN)
    except (ValueError, OverflowError, OSError):
        return opponent
    hour = start.hour % 12 or 12
    return f"{opponent} {hour}:{start.minute:02d}{'a' if start.hour < 12 else 'p'}"


def weekday(day: date) -> str:
    return WEEKDAYS[day.weekday()]


def day_name(day: date) -> str:
    """ "Sat Nov 28"."""
    return f"{weekday(day)} {MONTHS[day.month - 1]} {day.day}"


def days_span(days: Sequence[date]) -> str:
    """ "Sat Nov 28 to Sun Nov 29", or one day's name."""
    if not days:
        return ""
    first, last = day_name(days[0]), day_name(days[-1])
    return first if first == last else f"{first} to {last}"


def week_plus(gain: int) -> str:
    """A suggester gain (integer units) as TTLTST-starts, 2 decimals, signed."""
    return signed(gain / SCALE, 2)


def fills(filled: Sequence[tuple[date, str]]) -> str:
    """The days and slots an add starts in: "Sat C, Mon D"."""
    return ", ".join(f"{weekday(day)} {slot}" for day, slot in filled)


def gap_text(empty: Sequence[tuple[str, int]]) -> str:
    """A day's empty starting slots: "C 1 · D 2", or "none"."""
    return " · ".join(f"{slot} {count}" for slot, count in empty) or "none"


def day_labels(slates: Sequence[Tonight], team: str) -> list[str]:
    """The team's marks: tonight's as is, a later day's after its weekday ("Sat vs NYR 1:00p")."""
    labels = []
    for i, slate in enumerate(slates):
        label = game_label(slate.matchup(team))
        if label is not None:
            labels.append(label if i == 0 else f"{weekday(slate.date)} {label}")
    return labels


def game_count(slates: Sequence[Tonight], team: str) -> int | None:
    """The team's games over the slates; None (a dash) when any of them is unavailable."""
    if any(slate.state == "unavailable" for slate in slates):
        return None
    return sum(slate.matchup(team) is not None for slate in slates)


def schedule_notes(slates: Sequence[Tonight]) -> list[Markup]:
    """The notes above a table with marks: each day's state, then which days' times show.
    Markup from literal templates (the arguments are escaped), so apostrophes print as typed."""
    notes = []
    for i, slate in enumerate(slates):
        day = weekday(slate.date)
        if slate.state == "unavailable":
            notes.append(
                Markup("Tonight's schedule is unavailable.")
                if i == 0
                else Markup("Tomorrow's ({}) schedule is unavailable.").format(day)
            )
            continue
        if slate.state == "stale" and slate.age is not None:
            when = ago(slate.age)
            notes.append(
                Markup("Schedule from {}.").format(when)
                if i == 0
                else Markup("Schedule for {} from {}.").format(day, when)
            )
        if not slate.games:
            notes.append(
                Markup("No games tonight.") if i == 0 else Markup("No games {}.").format(day)
            )
    line = games_line(slates)
    return [*notes, line] if line is not None else notes


def games_line(slates: Sequence[Tonight]) -> Markup | None:
    """ "Tonight's games (times ET)", naming only the days that have games."""
    days = [
        Markup("Tonight's") if i == 0 else Markup("{}'s").format(weekday(slate.date))
        for i, slate in enumerate(slates)
        if slate.state != "unavailable" and slate.games
    ]
    return Markup(" and ").join(days) + Markup(" games (times ET)") if days else None


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
    env.filters.update(money=money, number=number, up=up, signed=signed, pctl=percentile, ago=ago)
    env.globals.update(url=url, rating_settings=rating_settings, DASH=DASH)
    env.filters.update(
        day_name=day_name, days_span=days_span, week_plus=week_plus, fills=fills, gap_text=gap_text
    )


def _install_on_app_templates() -> None:
    from fha.web.app import templates

    install(templates.env)


_install_on_app_templates()
