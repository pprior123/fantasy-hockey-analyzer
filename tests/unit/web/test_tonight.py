"""Tonight's filter, opponent marks and midnight paging through the web routes."""

import asyncio
import re
from dataclasses import replace
from datetime import date, datetime
from html import unescape
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from fha.domain.names import NHL_TEAMS
from fha.domain.schedule import Game, Mark
from fha.services.players_table import PAGE_SIZE, Owner, PlayersQuery, SortKey, select
from fha.services.schedule import COLLECTION, EASTERN, team_code
from fha.sources.nhl.demo import DemoScheduleSource
from fha.web import context
from fha.web.format import game_label
from tests.unit.services.test_schedule import DAY, GAME, NOW, Source
from tests.unit.web.helpers import SETTINGS, FakeClock, make_services
from tests.unit.web.test_demo_screens import ENV
from tests.unit.web.test_screens import (
    EVERY_ROW,
    MINE,
    SNAP,
    active_owner_chip,
    build_view_for_test,
    clear_link,
    client,
    filter_form,
    hidden_inputs,
    names_in,
    player_link,
    search_form,
    services_for,
    show_more,
)

_REMAINING = sorted(NHL_TEAMS - {"TBL", "BOS", "EDM", "LAK", "ANA", "BUF", "SJS", "VAN"})
GAMES = (
    GAME,
    Game("EDM", "LAK", None, 2),
    *(
        Game(away, home, GAME.start_utc, 2)
        for away, home in zip(_REMAINING[::2], _REMAINING[1::2], strict=True)
    ),
)
PLAYING = {code for game in GAMES for code in (game.away, game.home)}


def params(link: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(link).query).items()}


def tonight_lit(html: str) -> bool:
    return bool(re.search(r'class="chip on" href="[^"]*">Tonight</a>', html))


def partial_names(part: str) -> list[str]:
    return names_in(f'<table class="data players">{part}</table>', "players")


def row_for(html: str, table: str, name: str) -> str:
    body = html.split(f'class="data {table}"', 1)[1].split("</table>", 1)[0]
    (row,) = [row for row in re.findall(r"<tr[^>]*>.*?</tr>", body, re.S) if name in row]
    return row


def test_schedule_toggle_combines_with_each_owner_chip() -> None:
    svc = make_services(FakeClock(NOW))
    source = Source(GAMES)
    svc.schedule_source = source
    c = client(svc)
    page = c.get(f"/players?tonight=1&{EVERY_ROW}").text
    assert active_owner_chip(page) == "Free Agents"
    assert tonight_lit(page)
    assert "Tonight's games (times ET)" in page
    view = build_view_for_test()
    for owner, label in [
        (Owner.ALL, "All"),
        (Owner.MINE, "My Team"),
        (Owner.FREE, "Free Agents"),
        (Owner.TAKEN, "Taken"),
    ]:
        link = player_link(page, label)
        assert params(link)["tonight"] == "1"
        filtered = c.get(link + "&start=99000").text
        assert tonight_lit(filtered)
        assert active_owner_chip(filtered) == label
        expected = [
            r.name
            for r in select(view, PlayersQuery(owner=owner, zero_gp=True))
            if team_code(r.nhl_team) in PLAYING
        ]
        assert names_in(filtered, "players") == expected
        assert f"{len(expected)} players" in filtered
    assert "tonight" not in params(player_link(page, "Tonight"))
    assert c.get("/players?tonight=2").status_code == 400
    assert source.calls == [DAY]


def test_today_filter_keeps_counts_and_order_across_both_paging_paths() -> None:
    svc = make_services(FakeClock(NOW))
    svc.schedule_source = Source(GAMES)
    c = client(svc)
    page = c.get("/players?tonight=1&sort=name&season=last").text
    view = build_view_for_test()
    eligible = [
        r
        for r in select(
            view, PlayersQuery(owner=Owner.FREE, sort=SortKey.NAME, descending=False, zero_gp=True)
        )
        if team_code(r.nhl_team) in PLAYING
    ]
    expected = [r.name for r in eligible if r.gp > 0]
    hidden = len(eligible) - len(expected)
    assert len(expected) > 2 * PAGE_SIZE
    assert hidden > 0
    assert f"{len(expected)} players ({hidden} with 0 GP hidden" in page
    appended = names_in(page, "players")
    for start in range(PAGE_SIZE, len(expected), PAGE_SIZE):
        links = show_more(page)
        assert links is not None
        for href in links:
            assert params(href) == {
                "tonight": "1",
                "sort": "name",
                "season": "last",
                "start": str(start),
                "night": DAY.isoformat(),
            }
        page = c.get(links[0]).text
        part = c.get(links[1]).text
        chunk = partial_names(part)
        assert chunk == expected[start : start + PAGE_SIZE]
        appended.extend(chunk)
        assert names_in(page, "players") == appended
        assert show_more(part) == show_more(page)
    assert show_more(page) is None
    assert appended == expected
    assert len(appended) == len(set(appended))


def test_sort_view_season_zero_gp_and_apply_links_keep_tonight() -> None:
    svc = make_services(FakeClock(NOW))
    svc.schedule_source = Source(GAMES)
    c = client(svc)
    page = c.get("/players?tonight=1").text
    for label in ("Name", "This season", "Last season", "Categories", "Totals", "show them"):
        link = player_link(page, label)
        assert params(link)["tonight"] == "1"
        assert tonight_lit(c.get(link).text)
    shown = c.get(player_link(page, "show them")).text
    assert params(player_link(shown, "hide 0 GP"))["tonight"] == "1"
    team = MINE.team_key
    fields = hidden_inputs(filter_form(shown))
    assert fields == {"tonight": "1", "zero_gp": "1"}
    selected = c.get("/players", params={**fields, "team": team, "pos": "", "min_gp": ""}).text
    matches = {e.player.name for e in MINE.roster if team_code(e.player.nhl_team) in PLAYING}
    assert set(names_in(selected, "players")) == matches
    assert active_owner_chip(selected) is None
    fields = hidden_inputs(filter_form(selected))
    any_team = c.get("/players", params={**fields, "team": "", "pos": "", "min_gp": ""}).text
    assert active_owner_chip(any_team) == "Free Agents"
    assert tonight_lit(any_team)
    assert names_in(any_team, "players") == names_in(shown, "players")


def test_search_drops_tonight_then_the_chip_can_narrow_results_again() -> None:
    svc = make_services(FakeClock(NOW))
    svc.schedule_source = Source(GAMES)
    c = client(svc)
    filtered = c.get("/players?tonight=1&owner=free&season=last&sort=name").text
    fields = hidden_inputs(search_form(filtered))
    assert fields == {"season": "last", "sort": "name"}
    searched = c.get("/players", params={**fields, "q": "Stavik"}).text
    assert active_owner_chip(searched) == "All"
    assert not tonight_lit(searched)
    link = player_link(searched, "Tonight")
    assert params(link) == {**fields, "q": "Stavik", "tonight": "1"}
    tonight_matches = c.get(link).text
    assert tonight_lit(tonight_matches)
    by_name = {p.name: team_code(p.nhl_team) for p in SNAP.pool}
    names = names_in(searched, "players")
    assert names_in(tonight_matches, "players") == [n for n in names if by_name[n] in PLAYING]
    cleared = clear_link(tonight_matches)
    assert cleared is not None
    assert params(cleared) == fields
    assert not tonight_lit(c.get(cleared).text)


def test_a_free_agent_with_no_nhl_team_cannot_show_in_tonight() -> None:
    no_team = replace(SNAP.available[0], nhl_team="")
    svc = services_for(replace(SNAP, available=(no_team, *SNAP.available[1:])))
    svc.schedule_source = Source(GAMES)
    c = client(svc)
    assert no_team.name in names_in(c.get(f"/players?{EVERY_ROW}").text, "players")
    assert no_team.name not in names_in(c.get(f"/players?tonight=1&{EVERY_ROW}").text, "players")


def test_marks_display_below_the_team_code_with_or_without_a_time() -> None:
    svc = make_services(FakeClock(NOW))
    svc.schedule_source = Source(GAMES)
    page = client(svc).get(f"/players?owner=all&{EVERY_ROW}").text
    assert re.search(r'<td>TB[L]?<span class="game-mark muted">@BOS 7:00p</span></td>', page, re.I)
    assert re.search(r'<td>LA[K]?<span class="game-mark muted">vs EDM</span></td>', page, re.I)
    assert (
        game_label(Mark("vs", "BOS", datetime.fromisoformat("2026-11-27T05:05:00Z").timestamp()))
        == "vs BOS 12:05a"
    )


def test_unavailable_tonight_uses_the_normal_list_and_hides_the_roster_count() -> None:
    svc = make_services(FakeClock(NOW))
    source = Source(fail=True)
    svc.schedule_source = source
    c = client(svc)
    normal = c.get("/players").text
    page = c.get("/players?tonight=1").text
    assert "Tonight's schedule is unavailable." in page
    assert "No players match." not in page
    assert not tonight_lit(page)
    assert names_in(page, "players") == names_in(normal, "players")
    rosters = c.get("/rosters").text
    assert "Tonight's schedule is unavailable." in rosters
    assert "on teams with a game" not in rosters
    assert 'class="game-mark' not in rosters
    assert source.calls == [DAY]


def test_off_day_is_distinct_from_a_stale_slate() -> None:
    clock = FakeClock(NOW)
    svc = make_services(clock)
    source = Source()
    svc.schedule_source = source
    c = client(svc)
    initial = c.get(f"/players?tonight=1&{EVERY_ROW}").text
    assert names_in(initial, "players")
    clock.t += 7 * 3600
    source.fail = True
    stale = c.get(f"/players?tonight=1&{EVERY_ROW}").text
    assert "Schedule from 7 h ago." in stale
    assert tonight_lit(stale)
    assert names_in(stale, "players") == names_in(initial, "players")

    clock.t += 300
    source.fail = False
    source.games = ()
    off_day = c.get("/players?tonight=1").text
    assert "No games tonight." in off_day
    assert "unavailable" not in off_day
    assert tonight_lit(off_day)
    assert names_in(off_day, "players") == []
    assert "Tonight: 0 of" in c.get("/rosters").text


def test_show_more_after_eastern_midnight_asks_for_a_reload_without_appending_rows() -> None:
    clock = FakeClock(datetime.fromisoformat("2026-11-28T04:59:00Z").timestamp())
    svc = make_services(clock)
    source = Source(GAMES)
    svc.schedule_source = source
    c = client(svc)
    page = c.get("/players?owner=all&tonight=1&view=cats").text
    links = show_more(page)
    assert links is not None
    assert params(links[1])["night"] == DAY.isoformat()
    clock.t += 60
    response = c.get(links[1])
    assert response.status_code == 200
    assert "It's a new night" in unescape(response.text)
    assert response.text.count("<tr>") == 1
    assert partial_names(response.text) == []
    assert source.calls == [DAY]
    reload = player_link(response.text, "reload the list.")
    assert params(reload) == {"owner": "all", "tonight": "1", "view": "cats", "start": "100"}
    new_page = c.get(reload).text
    next_link = show_more(new_page)
    assert next_link is not None
    assert params(next_link[1])["night"] == "2026-11-28"


def test_roster_marks_goalies_and_ir_but_counts_only_players_outside_ir() -> None:
    skaters = [e for e in MINE.roster if not e.player.is_goalie]
    injured = replace(
        skaters[0],
        player=replace(skaters[0].player, nhl_team="TB", status="O"),
        selected_position="C",
    )
    on_ir = replace(
        skaters[1], player=replace(skaters[1].player, nhl_team="BOS"), selected_position="IR+"
    )
    idle = replace(
        skaters[2], player=replace(skaters[2].player, nhl_team="ANA"), selected_position="BN"
    )
    g = next(e for e in MINE.roster if e.player.is_goalie)
    goalie = replace(g, player=replace(g.player, nhl_team="BOS"), selected_position="G")
    my_team = replace(MINE, roster=(injured, on_ir, idle, goalie))
    svc = services_for(replace(SNAP, teams=tuple(my_team if t.is_mine else t for t in SNAP.teams)))
    svc.schedule_source = Source()
    html = client(svc).get("/rosters").text
    assert "Tonight: 2 of 3 on teams with a game" in html
    assert html.count("Tonight's games (times ET)") == 2
    injured_row = row_for(html, "roster", injured.player.name)
    assert 'class="badge">O</span>' in injured_row
    assert "@BOS 7:00p" in injured_row
    assert "vs TBL 7:00p" in row_for(html, "roster", on_ir.player.name)
    assert "game-mark" not in row_for(html, "roster", idle.player.name)
    assert "vs TBL 7:00p" in row_for(html, "goalies", goalie.player.name)


def test_only_players_and_main_rosters_read_the_schedule() -> None:
    svc = make_services(FakeClock(NOW))
    source = Source()
    svc.schedule_source = source
    c = client(svc)
    for path in (
        f"/rosters/replace?drop={MINE.roster[0].player.player_id}",
        "/league",
        "/matchup",
        "/matchup/free-agents",
        "/admin",
    ):
        assert c.get(path).status_code == 200
    assert source.calls == []
    assert c.get("/players/rows").status_code == 200
    assert c.get("/rosters").status_code == 200
    assert c.get("/players").status_code == 200
    assert source.calls == [DAY]


def test_demo_games_follow_the_date_without_using_http(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock(NOW)
    monkeypatch.setattr(context, "SystemClock", lambda: clock)

    def no_http(request: httpx.Request) -> httpx.Response:
        raise AssertionError("The demo should never fetch the NHL schedule")

    http = httpx.AsyncClient(transport=httpx.MockTransport(no_http))
    try:
        svc = context.demo_services(ENV, http, SETTINGS)
        assert isinstance(svc.schedule_source, DemoScheduleSource)
        c = client(svc)
        for day in (DAY, date(2026, 11, 28)):
            players = c.get("/players?tonight=1").text
            rosters = c.get("/rosters").text
            assert 'class="game-mark' in players
            assert 'class="game-mark' in rosters
            assert "on teams with a game" in rosters
            cached = asyncio.run(svc.repo.get(COLLECTION, day.isoformat()))
            assert cached is not None
            assert datetime.fromtimestamp(cached["games"][0]["start_utc"], EASTERN).date() == day
            clock.t += 24 * 3600
    finally:
        asyncio.run(http.aclose())
