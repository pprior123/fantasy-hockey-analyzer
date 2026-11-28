"""Suggest adds (/rosters/suggest), Replace's week columns, and the demo
(plans/weekly-suggester.md §3 phase 4)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import date, datetime
from html import unescape
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from fha.domain.engine import DEFAULT_CONFIG
from fha.domain.schedule import Game
from fha.services.league_view import Salaries, Season, build_view
from fha.services.refresh import RefreshService
from fha.sources.nhl.schedule import ScheduleError
from fha.sources.yahoo.demo import demo_snapshot, demo_this_week
from fha.sources.yahoo.fake import FakeYahooSource
from fha.sources.yahoo.models import LeagueSnapshot
from fha.storage.memory import InMemoryRepository
from fha.web import context
from fha.web.context import Services
from fha.web.format import day_name, days_span, fills, gap_text, week_plus
from tests.unit.web.helpers import SETTINGS, FakeClock, logged_in, make_app
from tests.unit.web.test_demo_screens import ENV
from tests.unit.web.test_tonight import GAMES

TODAY = date(2026, 11, 27)  # a Friday
NOW = datetime.fromisoformat("2026-11-27T15:00:00Z").timestamp()
SAT, SUN = date(2026, 11, 28), date(2026, 11, 29)
SNAP = demo_this_week(demo_snapshot(), TODAY)  # this week Mon Nov 23 to Sun Nov 29
MINE = SNAP.my_team
assert MINE is not None


class DaySource:
    """The same full slate every day, except dates set to fail."""

    def __init__(self, games: tuple[Game, ...] = GAMES, failing: frozenset[date] = frozenset()):
        self.games = games
        self.failing = failing
        self.calls: list[date] = []

    async def games_on(self, day: date) -> tuple[Game, ...]:
        self.calls.append(day)
        if day in self.failing:
            raise ScheduleError("test provider failure")
        return self.games


def setup(
    snapshot: LeagueSnapshot = SNAP,
    source: DaySource | None = None,
    now: float = NOW,
    clock: FakeClock | None = None,
) -> tuple[TestClient, DaySource]:
    clock = clock or FakeClock(now)
    repo = InMemoryRepository()
    svc = Services(repo, RefreshService(FakeYahooSource(snapshot), repo, clock), clock)
    src = source or DaySource()
    svc.schedule_source = src
    return logged_in(make_app(svc)), src


def params(link: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(unescape(link)).query).items()}


def table(html: str, name: str) -> str:
    return html.split(f'class="data {name}"', 1)[1].split("</table>", 1)[0]


def body_rows(html: str, name: str) -> list[str]:
    return re.findall(r"<tr[^>]*>(.*?)</tr>", table(html, name).split("</thead>", 1)[1], re.S)


def text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def my_skater(*, ir: bool = False) -> str:
    return next(
        e.player.player_id for e in MINE.roster if not e.player.is_goalie and e.in_ir_slot == ir
    )


# The page


def test_this_week_lists_the_horizon_adds_left_gaps_and_ten_adds() -> None:
    c, source = setup()
    page = c.get("/rosters/suggest").text
    words = text(page)
    assert "This week: Sat Nov 28 to Sun Nov 29" in words
    assert "Adds left: 3 of 5" in words
    assert "may be on waivers" in words
    assert len(body_rows(page, "gaps")) == 2
    rows = body_rows(page, "suggest")
    assert len(rows) == 10
    gains = [float(re.search(r'class="num good">\+([\d.]+)<', r)[1]) for r in rows]  # type: ignore[index]
    assert gains == sorted(gains, reverse=True)
    assert all(g >= 0.01 for g in gains)
    assert sorted(source.calls) == [SAT, SUN]


def test_next_week_covers_its_seven_days_with_the_limit_reset() -> None:
    c, source = setup()
    page = c.get("/rosters/suggest?week=next").text
    words = text(page)
    assert "Next week: Mon Nov 30 to Sun Dec 6" in words
    assert "Adds next week: 5" in words
    assert len(body_rows(page, "gaps")) == 7
    assert len(source.calls) == 7
    for link in re.findall(r'href="(/rosters/replace[^"]*)"', page):
        assert params(link)["week"] == "next"


def test_the_week_chips_and_this_week_spelled_out() -> None:
    c, _ = setup()
    page = c.get("/rosters/suggest?week=this").text
    assert 'class="chip on" href="/rosters/suggest">This week</a>' in page
    assert c.get("/rosters/suggest?week=later").status_code == 400
    no_next = replace(SNAP, next_scoreboard=None)
    c2, _ = setup(no_next)
    assert "Next week</a>" not in c2.get("/rosters/suggest").text


def test_rows_link_to_replace_with_the_horizon_and_show_the_drop() -> None:
    c, _ = setup()
    page = c.get("/rosters/suggest?season=last").text
    rows = body_rows(page, "suggest")
    links = re.findall(r'href="(/rosters/replace[^"]*)"', table(page, "suggest"))
    assert len(links) == len(rows)  # a full roster: every add has a drop
    for link in links:
        p = params(link)
        assert (p["week"], p["season"]) == ("this", "last")
        assert c.get(unescape(link)).status_code == 200


def test_an_open_spot_row_has_no_replace_link() -> None:
    victim = next(e for e in MINE.roster if e.selected_position == "BN")
    roomy = replace(MINE, roster=tuple(e for e in MINE.roster if e is not victim))
    snap = replace(SNAP, teams=tuple(roomy if t.is_mine else t for t in SNAP.teams))
    c, _ = setup(snap)
    page = c.get("/rosters/suggest").text
    open_rows = [r for r in body_rows(page, "suggest") if "open spot" in r]
    assert open_rows
    assert all("Replace" not in r for r in open_rows)
    assert "you have an open spot" in page


def test_the_drop_shows_his_status_badge() -> None:
    view = build_view(SNAP, DEFAULT_CONFIG, Salaries(), season=Season.LAST)
    rated = [
        r
        for r in view.roster(MINE.team_key)
        if not r.is_goalie and not r.in_ir_slot and r.rating is not None and r.rating.rated
    ]
    weakest = min(rated, key=lambda r: (r.ttltst or 0.0, r.player_id))
    team = replace(
        MINE,
        roster=tuple(
            replace(e, player=replace(e.player, status="O"))
            if e.player.player_id == weakest.player_id
            else e
            for e in MINE.roster
        ),
    )
    snap = replace(SNAP, teams=tuple(team if t.is_mine else t for t in SNAP.teams))
    c, _ = setup(snap)
    page = c.get("/rosters/suggest?season=last").text
    rows = [r for r in body_rows(page, "suggest") if weakest.name in r]
    assert rows  # out, he loses nothing, so he is the best drop for some adds
    assert f'{weakest.name} <span class="badge">O</span>' in rows[0]


def test_no_days_left_links_to_next_week() -> None:
    sunday = datetime.fromisoformat("2026-11-29T15:00:00Z").timestamp()
    c, source = setup(now=sunday)
    page = c.get("/rosters/suggest").text
    assert "No days left this week." in page
    assert 'href="/rosters/suggest?week=next">Plan next week</a>' in page
    assert source.calls == []


def test_no_adds_left_shows_gaps_but_no_adds() -> None:
    team = replace(MINE, roster_adds=(SNAP.settings.current_week, 5))
    snap = replace(SNAP, teams=tuple(team if t.is_mine else t for t in SNAP.teams))
    c, _ = setup(snap)
    page = c.get("/rosters/suggest").text
    assert "Adds left: 0 of 5" in text(page)
    assert "No adds left this week" in page
    assert 'class="data gaps"' in page
    assert 'class="data suggest"' not in page


def test_unknown_adds_left_still_suggests() -> None:
    team = replace(MINE, roster_adds=None)
    snap = replace(SNAP, teams=tuple(team if t.is_mine else t for t in SNAP.teams))
    c, _ = setup(snap)
    page = c.get("/rosters/suggest").text
    assert "Adds left: unknown" in text(page)
    assert body_rows(page, "suggest")


def test_an_unavailable_day_names_it_and_shows_no_suggestions() -> None:
    c, _ = setup(source=DaySource(failing=frozenset({SUN})))
    page = c.get("/rosters/suggest").text
    words = text(page)
    assert "The NHL schedule for Sun Nov 29 is unavailable" in words
    assert "tries again in a few minutes" in words
    assert 'class="data suggest"' not in page
    assert 'class="data gaps"' not in page


def test_a_week_without_dates_says_so() -> None:
    snap = replace(SNAP, scoreboard=replace(SNAP.scoreboard, week_end=None))
    c, source = setup(snap)
    assert "dates aren't known yet" in text(c.get("/rosters/suggest").text)
    assert source.calls == []


def test_suggest_works_only_for_my_roster() -> None:
    c, _ = setup()
    other = next(t for t in SNAP.teams if not t.is_mine)
    assert c.get(f"/rosters/suggest?team={other.team_key}").status_code == 400
    assert c.get(f"/rosters/suggest?team={MINE.team_key}").status_code == 200


def test_rosters_links_to_suggest_only_on_my_team() -> None:
    c, source = setup()
    assert ">Suggest adds</a>" in c.get("/rosters").text
    other = next(t for t in SNAP.teams if not t.is_mine)
    assert ">Suggest adds</a>" not in c.get(f"/rosters?team={other.team_key}").text
    assert SAT not in source.calls  # Rosters reads tonight only


# Replace


def test_replace_without_a_week_is_unchanged_and_reads_no_schedule() -> None:
    c, source = setup()
    page = c.get(f"/rosters/replace?drop={my_skater()}").text
    assert "Week +" not in page
    assert 'class="chip on" href="/rosters/replace?drop=' in page  # "Any time" lit
    assert source.calls == []


def test_replace_with_a_week_adds_week_plus_and_starts_and_sorts_by_them() -> None:
    c, source = setup()
    drop = my_skater()
    page = c.get(f"/rosters/replace?drop={drop}&week=this").text
    assert '<th class="num">Week +</th><th class="num">Starts</th>' in page
    rows = body_rows(page, "replace")
    cells = [re.findall(r"<td[^>]*>(.*?)</td>", r, re.S) for r in rows]
    gains = [float(c[4]) for c in cells]  # Name, Pos, TTLTST, ΔTTLTST, Week +, Starts
    assert gains == sorted(gains, reverse=True)
    assert len(set(gains)) > 1
    assert all(re.fullmatch(r"\d of [0-2]", c[5]) for c in cells)
    assert sorted(set(source.calls)) == [SAT, SUN]
    header = table(page, "replace").split("</thead>", 1)[0]
    assert len(re.findall(r"<th\b", header)) == 4 + 2 + 2  # with AAV and Room after
    plain = c.get(f"/rosters/replace?drop={drop}").text
    assert names_of(page) != names_of(plain)  # Week + reorders, and lists other positions


def drop_positions(html: str) -> set[str]:
    head = html.split('<section class="card">', 1)[1].split("</p>", 1)[0]
    found = re.search(r"</strong> \(([^)]*)\)", head)
    assert found is not None
    return set(found[1].split(","))


def row_positions(html: str) -> list[set[str]]:
    return [
        set(re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)[1].split(","))
        for r in body_rows(html, "replace")
    ]


def test_every_suggest_row_s_add_is_on_its_linked_replace_page() -> None:
    c, _ = setup()
    page = c.get("/rosters/suggest").text
    rows = body_rows(page, "suggest")
    assert rows
    crossed = 0
    for r in rows:
        add = re.search(r'<td class="first">([^<]*)', r)[1].strip()  # type: ignore[index]
        link = unescape(re.search(r'href="(/rosters/replace[^"]*)"', r)[1])  # type: ignore[index]
        replace_page = c.get(link).text
        assert add in names_of(replace_page)
        add_pos = set(re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)[1].split(","))
        crossed += not (add_pos & drop_positions(replace_page))
    assert crossed  # some suggested adds play another position than their drop


def test_replace_with_a_week_lists_other_positions_and_without_one_does_not() -> None:
    c, _ = setup()
    drop = my_skater()
    plain = c.get(f"/rosters/replace?drop={drop}").text
    week = c.get(f"/rosters/replace?drop={drop}&week=this").text
    wanted = drop_positions(plain) & {"C", "LW", "RW", "D"}
    assert all(p & wanted for p in row_positions(plain))
    assert any(not (p & wanted) for p in row_positions(week))
    assert all(p & {"C", "LW", "RW", "D"} for p in row_positions(week))  # skaters only
    week_text = text(week)
    assert "No free agents" not in week_text


def names_of(html: str) -> list[str]:
    return [n.strip() for n in re.findall(r'<td class="first">([^<]*)', table(html, "replace"))]


@pytest.mark.parametrize("kind", ["goalie", "ir"])
def test_replace_of_a_goalie_or_an_ir_slot_player_has_no_week_chips(kind: str) -> None:
    c, source = setup()
    if kind == "goalie":
        drop = next(e.player.player_id for e in MINE.roster if e.player.is_goalie)
    else:
        drop = my_skater(ir=True)
    page = c.get(f"/rosters/replace?drop={drop}&week=this").text
    assert "This week</a>" not in page
    assert "Week +" not in page
    assert source.calls == []


@pytest.mark.parametrize("case", ["unavailable", "no-days", "unknown"])
def test_an_unpriceable_week_on_replace_shows_dashes_and_the_default_sort(case: str) -> None:
    source = DaySource(failing=frozenset({SUN})) if case == "unavailable" else None
    snap = (
        replace(SNAP, scoreboard=replace(SNAP.scoreboard, week_end=None))
        if case == "unknown"
        else SNAP
    )
    now = datetime.fromisoformat("2026-11-29T15:00:00Z").timestamp() if case == "no-days" else NOW
    c, _ = setup(snap, source, now)
    drop = my_skater()
    plain = c.get(f"/rosters/replace?drop={drop}").text
    page = c.get(f"/rosters/replace?drop={drop}&week=this").text
    assert "can't be worked out" in text(page)
    rows = body_rows(page, "replace")
    assert rows
    for r in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
        assert (cells[4], cells[5]) == ("—", "—")  # Week + and Starts
    assert names_of(page) == names_of(plain)


def test_stale_days_show_their_age_on_suggest_and_replace() -> None:
    clock = FakeClock(NOW)
    c, source = setup(clock=clock)
    assert body_rows(c.get("/rosters/suggest").text, "suggest")  # caches Sat and Sun
    clock.t += 7 * 3600
    source.failing = frozenset({SAT, SUN})
    page = c.get("/rosters/suggest").text
    words = text(page)
    assert "Schedule for Sat Nov 28 from 7 h ago." in words
    assert "Schedule for Sun Nov 29 from 7 h ago." in words
    assert body_rows(page, "suggest")  # stale days are used
    replace_page = text(c.get(f"/rosters/replace?drop={my_skater()}&week=this").text)
    assert "Schedule for Sat Nov 28 from 7 h ago." in replace_page


def test_next_week_adds_are_unknown_while_yahoo_reports_the_old_week() -> None:
    old = replace(
        SNAP,
        scoreboard=replace(SNAP.scoreboard, week_start="2026-11-16", week_end="2026-11-22"),
        next_scoreboard=replace(SNAP.scoreboard, week_start="2026-11-23", week_end="2026-11-29"),
    )
    c, _ = setup(old)
    words = text(c.get("/rosters/suggest?week=next").text)
    assert "Next week: Sat Nov 28 to Sun Nov 29" in words
    assert "Adds next week: unknown" in words


def test_replace_refuses_an_unknown_week() -> None:
    c, _ = setup()
    assert c.get(f"/rosters/replace?drop={my_skater()}&week=soon").status_code == 400


def test_other_screens_read_no_horizon() -> None:
    c, source = setup()
    for path in ("/league", "/matchup", "/admin", f"/rosters/replace?drop={my_skater()}"):
        assert c.get(path).status_code == 200
    assert source.calls == []
    c.get("/players")
    assert set(source.calls) == {TODAY}


# The demo


def test_the_demo_suggests_adds_without_an_nhl_call(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock(datetime.fromisoformat("2026-11-24T15:00:00Z").timestamp())  # a Tuesday
    monkeypatch.setattr(context, "SystemClock", lambda: clock)

    def no_http(request: httpx.Request) -> httpx.Response:
        raise AssertionError("The demo should never fetch the NHL schedule")

    http = httpx.AsyncClient(transport=httpx.MockTransport(no_http))
    try:
        svc = context.demo_services(ENV, http, SETTINGS)
        c = logged_in(make_app(svc))
        page = c.get("/rosters/suggest").text
        words = text(page)
        assert "This week: Wed Nov 25 to Sun Nov 29" in words
        assert "Adds left: 3 of 5" in words
        assert body_rows(page, "suggest")
        assert body_rows(c.get("/rosters/suggest?week=next").text, "suggest")
    finally:
        asyncio.run(http.aclose())


def test_demo_this_week_dates_the_weeks_around_today() -> None:
    snap = demo_this_week(demo_snapshot(), date(2026, 12, 6))  # a Sunday
    assert (snap.scoreboard.week_start, snap.scoreboard.week_end) == ("2026-11-30", "2026-12-06")
    assert snap.next_scoreboard is not None
    assert snap.next_scoreboard.week_start == "2026-12-07"
    assert snap.scoreboard.week == demo_snapshot().scoreboard.week
    assert snap.settings.max_weekly_adds == 5
    assert [t.roster_adds for t in snap.teams if t.is_mine] == [(snap.settings.current_week, 2)]
    assert {t.roster_adds for t in snap.teams if not t.is_mine} == {None}
    last = replace(demo_snapshot(), next_scoreboard=None)
    assert demo_this_week(last, date(2026, 12, 6)).next_scoreboard is None


def test_the_suggester_formats() -> None:
    assert day_name(date(2026, 1, 5)) == "Mon Jan 5"
    assert days_span([SAT, SUN]) == "Sat Nov 28 to Sun Nov 29"
    assert days_span([SAT]) == "Sat Nov 28"
    assert days_span([]) == ""
    assert week_plus(1_234_567) == "+1.23"
    assert week_plus(-50_000) == "-0.05"
    assert fills(((SAT, "C"), (date(2026, 11, 30), "D"))) == "Sat C, Mon D"
    assert gap_text((("C", 1), ("D", 2))) == "C 1 · D 2"
    assert gap_text(()) == "none"


def test_a_priced_week_with_no_candidate_says_nobody_can_play() -> None:
    c, _ = setup(source=DaySource(games=()))  # no games all week
    page = c.get(f"/rosters/replace?drop={my_skater()}&week=this").text
    assert "No free agent can play that week." in page
    assert "No free agents at" not in page
