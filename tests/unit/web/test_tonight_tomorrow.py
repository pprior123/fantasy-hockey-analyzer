"""Tonight + tomorrow (plans/tonight-tomorrow.md): the option, its filter, marks, game count
and notes on Players and Rosters, its two concurrent cached schedule calls, and the demo."""

import asyncio
import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from html import unescape

import httpx
import pytest
from fastapi.testclient import TestClient

from fha.domain.names import NHL_TEAMS
from fha.domain.schedule import Game
from fha.services import schedule as schedule_service
from fha.services.players_table import Owner, PlayersQuery, QueryError, SortKey, select
from fha.services.refresh import RefreshService
from fha.services.schedule import COLLECTION, nights, team_code, tomorrow_of
from fha.sources.nhl.demo import DemoScheduleSource
from fha.sources.nhl.schedule import ScheduleError
from fha.sources.yahoo.fake import FakeYahooSource
from fha.sources.yahoo.models import LeagueSnapshot
from fha.storage.memory import InMemoryRepository
from fha.web import context
from fha.web.context import Services
from fha.web.format import schedule_notes, weekday
from tests.unit.services.test_schedule import DAY, GAME, NOW
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
    player_links,
    search_form,
    show_more,
)
from tests.unit.web.test_tonight import GAMES, params, partial_names, row_for

TOMORROW = date(2026, 11, 28)  # DAY is a Friday
SAT_7PM = datetime.fromisoformat("2026-11-29T00:00:00Z").timestamp()
# Tomorrow: BOS again (a back-to-back with tonight's TBL @ BOS), NYR and DAL only tomorrow.
TOMORROW_GAMES = (Game("NYR", "BOS", SAT_7PM, 2), Game("DAL", "CHI", None, 2))


class DaySource:
    """A schedule source with its own games (or a failure, ``None``) for each date."""

    def __init__(self, by_day: dict[date, tuple[Game, ...] | None]) -> None:
        self.by_day = by_day
        self.calls: list[date] = []

    async def games_on(self, day: date) -> tuple[Game, ...]:
        self.calls.append(day)
        games = self.by_day.get(day, ())
        if games is None:
            raise ScheduleError("test provider failure")
        return games


def two_day_client(
    tonight_games: tuple[Game, ...] | None = (GAME,),
    tomorrow_games: tuple[Game, ...] | None = TOMORROW_GAMES,
    clock: FakeClock | None = None,
) -> tuple[DaySource, TestClient]:
    source, c, _ = two_day_setup(tonight_games, tomorrow_games, clock)
    return source, c


def two_day_setup(
    tonight_games: tuple[Game, ...] | None = (GAME,),
    tomorrow_games: tuple[Game, ...] | None = TOMORROW_GAMES,
    clock: FakeClock | None = None,
) -> tuple[DaySource, TestClient, Services]:
    svc = make_services(clock or FakeClock(NOW))
    source = DaySource({DAY: tonight_games, TOMORROW: tomorrow_games})
    svc.schedule_source = source
    return source, client(svc), svc


def roster_client(team: object) -> TestClient:
    """My team replaced by ``team``, at NOW, with both days' games."""
    snapshot: LeagueSnapshot = replace(
        SNAP, teams=tuple(team if t.is_mine else t for t in SNAP.teams)
    )
    clock = FakeClock(NOW)
    repo = InMemoryRepository()
    svc = Services(repo, RefreshService(FakeYahooSource(snapshot), repo, clock), clock)
    svc.schedule_source = DaySource({DAY: (GAME,), TOMORROW: TOMORROW_GAMES})
    return client(svc)


def chip_state(html: str, nav: str = "schedule-filter") -> dict[str, bool]:
    chips = html.split(f'class="chips {nav}"', 1)[1].split("</nav>", 1)[0]
    return {
        unescape(label): bool(on)
        for on, label in re.findall(r'class="chip( on)?"[^>]*>([^<]*)</a>', chips)
    }


def chip_link(html: str, label: str, nav: str = "schedule-filter") -> str:
    chips = html.split(f'class="chips {nav}"', 1)[1].split("</nav>", 1)[0]
    (href,) = re.findall(rf'href="([^"]*)">{re.escape(label)}</a>', chips)
    return href.replace("&amp;", "&")


def gm_cells(html: str, table: str) -> dict[str, str]:
    """Each row's Gm cell, by player name (Gm is the column headed "Gm")."""
    body = html.split(f'class="data {table}"', 1)[1].split("</table>", 1)[0]
    head = body.split("</thead>", 1)[0]
    headers = re.findall(r"<th\b[^>]*>(.*?)</th>", head, re.S)
    index = [re.sub(r"<[^>]+>|\s", "", h) for h in headers].index("Gm")
    cells = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body.split("</thead>", 1)[1], re.S):
        tds = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)
        if len(tds) > index:
            name = re.sub(r"<dl.*?</dl>|<span.*?</span>|<[^>]+>", " ", tds[0], flags=re.S)
            cells[" ".join(name.split())] = tds[index].strip()
    return cells


def expected_names(owner: Owner, teams: set[str]) -> list[str]:
    return [
        r.name
        for r in select(build_view_for_test(), PlayersQuery(owner=owner, zero_gp=True))
        if team_code(r.nhl_team) in teams
    ]


def team_player(team: str) -> str:
    """A free agent with games last season on ``team``."""
    return next(p.name for p in SNAP.available if team_code(p.nhl_team) == team)


# The parameter and the chips


def test_tonight_two_parses_and_other_values_are_refused() -> None:
    assert PlayersQuery.from_params({"tonight": "2"}).nights == 2
    assert PlayersQuery.from_params({"tonight": "1"}).nights == 1
    assert PlayersQuery.from_params({}).nights == 0
    with pytest.raises(QueryError, match="tonight must be 1, 2 or absent"):
        PlayersQuery.from_params({"tonight": "3"})
    _, c = two_day_client()
    assert c.get("/players?tonight=3").status_code == 400
    assert c.get("/players?tonight=0").status_code == 400


def test_bare_two_day_option_opens_on_free_agents_with_only_its_chip_lit() -> None:
    _, c = two_day_client()
    page = c.get("/players?tonight=2").text
    assert active_owner_chip(page) == "Free Agents"
    assert chip_state(page) == {"Tonight": False, "Tonight + tomorrow": True}
    assert chip_state(c.get("/players?tonight=1").text) == {
        "Tonight": True,
        "Tonight + tomorrow": False,
    }
    assert chip_state(c.get("/players").text) == {"Tonight": False, "Tonight + tomorrow": False}


def test_the_two_chips_switch_to_each_other_and_toggle_off() -> None:
    _, c = two_day_client()
    base = "owner=all&sort=name"
    two = c.get(f"/players?{base}&tonight=2").text
    assert params(chip_link(two, "Tonight + tomorrow")) == {"owner": "all", "sort": "name"}
    assert params(chip_link(two, "Tonight"))["tonight"] == "1"
    one = c.get(f"/players?{base}&tonight=1").text
    assert params(chip_link(one, "Tonight + tomorrow"))["tonight"] == "2"
    assert "tonight" not in params(chip_link(one, "Tonight"))
    off = c.get(f"/players?{base}").text
    assert params(chip_link(off, "Tonight + tomorrow"))["tonight"] == "2"


def test_links_forms_and_paging_carry_the_two_day_option_and_search_drops_it() -> None:
    _, c = two_day_client(GAMES)
    page = c.get("/players?tonight=2&season=last").text
    for label in ("Name", "This season", "Last season", "Categories", "Totals", "show them"):
        link = player_link(page, label)
        assert params(link)["tonight"] == "2"
        assert chip_state(c.get(link).text)["Tonight + tomorrow"]
    for label in ("All", "My Team", "Taken"):
        assert params(chip_link(page, label, "owner-chips"))["tonight"] == "2"
    fields = hidden_inputs(filter_form(page))
    assert fields == {"tonight": "2", "season": "last"}
    any_team = c.get("/players", params={**fields, "team": "", "pos": "", "min_gp": ""}).text
    assert chip_state(any_team)["Tonight + tomorrow"]
    links = show_more(page)
    assert links is not None
    for href in links:
        assert params(href)["tonight"] == "2"
    assert chip_state(c.get(links[0]).text)["Tonight + tomorrow"]
    assert "tonight" not in hidden_inputs(search_form(page))
    searched = c.get("/players", params={**hidden_inputs(search_form(page)), "q": "a"}).text
    link = chip_link(searched, "Tonight + tomorrow")
    chips = {link, chip_link(searched, "Tonight")}
    others = [href for href in player_links(searched) if href not in chips]
    assert others
    assert not any("tonight" in params(href) for href in others)
    narrowed = c.get(link).text
    cleared = clear_link(narrowed)
    assert cleared is not None
    assert "tonight" not in params(cleared)


# The filter and the count


def test_two_day_filter_keeps_teams_playing_tonight_or_tomorrow() -> None:
    _, c = two_day_client()
    tonight_teams = {"TBL", "BOS"}
    both = tonight_teams | {"NYR", "BOS", "DAL", "CHI"}
    two = c.get(f"/players?owner=all&tonight=2&{EVERY_ROW}").text
    one = c.get(f"/players?owner=all&tonight=1&{EVERY_ROW}").text
    assert names_in(two, "players") == expected_names(Owner.ALL, both)
    assert names_in(one, "players") == expected_names(Owner.ALL, tonight_teams)
    assert f"{len(expected_names(Owner.ALL, both))} players" in two
    only_tomorrow = team_player("NYR")
    only_tonight = team_player("TBL")
    neither = team_player("ANA")
    assert only_tomorrow in names_in(two, "players")
    assert only_tomorrow not in names_in(one, "players")
    assert only_tonight in names_in(two, "players")
    assert only_tonight in names_in(one, "players")
    assert neither not in names_in(two, "players")


def test_two_day_zero_gp_note_follows_the_filter() -> None:
    _, c = two_day_client()
    page = c.get("/players?tonight=2&sort=name").text
    teams = {"TBL", "BOS", "NYR", "DAL", "CHI"}
    eligible = [
        r
        for r in select(
            build_view_for_test(),
            PlayersQuery(owner=Owner.FREE, sort=SortKey.NAME, descending=False, zero_gp=True),
        )
        if team_code(r.nhl_team) in teams
    ]
    shown = [r for r in eligible if r.gp > 0]
    assert f"{len(shown)} players ({len(eligible) - len(shown)} with 0 GP hidden" in page


def test_off_day_tonight_with_games_tomorrow_filters_to_tomorrows_teams() -> None:
    _, c = two_day_client(tonight_games=())
    page = c.get(f"/players?owner=all&tonight=2&{EVERY_ROW}").text
    assert names_in(page, "players") == expected_names(Owner.ALL, {"NYR", "BOS", "DAL", "CHI"})
    assert chip_state(page)["Tonight + tomorrow"]
    text = unescape(page)
    assert "No games tonight." in text
    assert "Sat's games (times ET)" in text
    assert "Tonight's and Sat's" not in text
    assert "Tonight's games" not in text


# Mixed states


@pytest.mark.parametrize(
    ("tonight_games", "tomorrow_games", "note"),
    [
        ((GAME,), None, "Tomorrow's (Sat) schedule is unavailable."),
        (None, TOMORROW_GAMES, "Tonight's schedule is unavailable."),
    ],
    ids=["tomorrow-unavailable", "tonight-unavailable"],
)
def test_one_unavailable_day_shows_the_normal_list_and_dashes(
    tonight_games: tuple[Game, ...] | None, tomorrow_games: tuple[Game, ...] | None, note: str
) -> None:
    _, c = two_day_client(tonight_games, tomorrow_games)
    normal = c.get(f"/players?owner=all&{EVERY_ROW}").text
    page = c.get(f"/players?owner=all&tonight=2&{EVERY_ROW}").text
    assert note in unescape(page)
    assert chip_state(page) == {"Tonight": False, "Tonight + tomorrow": False}
    assert "No players match." not in page
    assert names_in(page, "players") == names_in(normal, "players")
    assert set(gm_cells(page, "players").values()) == {"—"}
    rosters = c.get("/rosters?tonight=2").text
    assert note in unescape(rosters)
    assert "Tonight + tomorrow:" not in rosters
    assert set(gm_cells(rosters, "roster").values()) == {"—"}
    assert set(gm_cells(rosters, "goalies").values()) == {"—"}


def test_a_stale_tomorrow_shows_its_age_and_still_filters() -> None:
    clock = FakeClock(NOW)
    source, c, svc = two_day_setup(clock=clock)
    first = c.get(f"/players?owner=all&tonight=2&{EVERY_ROW}").text
    clock.t += 7 * 3600
    source.by_day[TOMORROW] = None
    asyncio.run(svc.repo.delete(COLLECTION, DAY.isoformat()))  # tonight refetched: fresh
    page = c.get(f"/players?owner=all&tonight=2&{EVERY_ROW}").text
    assert "Schedule for Sat from 7 h ago." in page
    assert "Schedule from" not in page.replace("Schedule for Sat from", "")
    assert chip_state(page)["Tonight + tomorrow"]
    assert names_in(page, "players") == names_in(first, "players")


# Dates


@pytest.mark.parametrize(
    ("instant", "tonight_date", "tomorrow_date"),
    [
        ("2026-11-28T04:59:00Z", date(2026, 11, 27), date(2026, 11, 28)),
        ("2026-11-28T05:00:00Z", date(2026, 11, 28), date(2026, 11, 29)),
        ("2027-03-13T16:00:00Z", date(2027, 3, 13), date(2027, 3, 14)),  # the day before DST
        ("2027-03-14T16:00:00Z", date(2027, 3, 14), date(2027, 3, 15)),  # the DST change day
        ("2026-11-01T16:00:00Z", date(2026, 11, 1), date(2026, 11, 2)),  # DST ends
    ],
    ids=["before-eastern-midnight", "after-eastern-midnight", "eve-of-dst", "dst-day", "dst-end"],
)
def test_tomorrow_is_the_eastern_date_plus_one_day(
    instant: str, tonight_date: date, tomorrow_date: date
) -> None:
    assert tomorrow_of(tonight_date) == tomorrow_date
    svc = make_services(FakeClock(datetime.fromisoformat(instant).timestamp()))
    source = DaySource({})
    svc.schedule_source = source
    c = client(svc)
    assert c.get("/players?tonight=2").status_code == 200
    assert sorted(source.calls) == [tonight_date, tomorrow_date]


# Cost


def test_tomorrow_is_read_only_with_the_two_day_option_on_players_and_rosters() -> None:
    source, c = two_day_client()
    for path in ("/players", "/players?tonight=1", "/rosters", "/rosters?tonight=1"):
        assert c.get(path).status_code == 200
    assert set(source.calls) == {DAY}
    for path in (
        f"/rosters/replace?tonight=2&drop={MINE.roster[0].player.player_id}",
        "/league?tonight=2",
        "/matchup?tonight=2",
        "/matchup/free-agents?tonight=2",
        "/admin?tonight=2",
    ):
        assert c.get(path).status_code == 200
    assert TOMORROW not in source.calls
    assert c.get("/players?tonight=2").status_code == 200
    assert TOMORROW in source.calls
    source.calls.clear()
    assert c.get("/rosters?tonight=2").status_code == 200
    assert source.calls == []  # both days cached


def test_the_two_days_load_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each call waits until both dates were asked for: a sequential load times out the first
    day and reports it unavailable."""
    monkeypatch.setattr(schedule_service, "SCHEDULE_TIMEOUT", 0.5)

    class Barrier:
        def __init__(self) -> None:
            self.asked: set[date] = set()
            self.both: asyncio.Event | None = None

        async def games_on(self, day: date) -> tuple[Game, ...]:
            if self.both is None:
                self.both = asyncio.Event()  # on the loop that runs the calls
            self.asked.add(day)
            if len(self.asked) == 2:
                self.both.set()
            await self.both.wait()
            return (GAME,) if day == DAY else TOMORROW_GAMES

    async def load() -> tuple[str, ...]:
        slates = await nights(InMemoryRepository(), Barrier(), FakeClock(NOW), count=2, night=DAY)
        return tuple(s.state for s in slates)

    assert asyncio.run(load()) == ("fresh", "fresh")
    svc = make_services(FakeClock(NOW))
    svc.schedule_source = Barrier()
    page = client(svc).get("/players?tonight=2").text
    assert chip_state(page)["Tonight + tomorrow"]


# Marks


def test_tomorrows_marks_follow_tonights_with_the_weekday_only_with_the_option() -> None:
    _, c = two_day_client()
    boston = team_player("BOS")
    two = c.get(f"/players?owner=all&tonight=2&{EVERY_ROW}").text
    row = row_for(two, "players", boston)
    assert re.search(
        r'<span class="game-mark muted">vs TBL 7:00p</span>'
        r'<span class="game-mark muted">Sat vs NYR 7:00p</span>',
        row,
    )
    assert '<span class="game-mark muted">Sat @CHI</span>' in row_for(
        two, "players", team_player("DAL")
    )
    for query in ("tonight=1", "owner=all"):
        page = c.get(f"/players?owner=all&{query}&{EVERY_ROW}").text
        assert "Sat " not in row_for(page, "players", boston)
        assert "vs TBL 7:00p" in row_for(page, "players", boston)


def test_roster_goalie_marks_and_counts_cover_both_days() -> None:
    g = next(e for e in MINE.roster if e.player.is_goalie)
    goalie = replace(g, player=replace(g.player, nhl_team="BOS"), selected_position="G")
    c = roster_client(replace(MINE, roster=(*(e for e in MINE.roster if e is not g), goalie)))
    two = c.get("/rosters?tonight=2").text
    row = row_for(two, "goalies", goalie.player.name)
    assert "vs TBL 7:00p" in row
    assert "Sat vs NYR 7:00p" in row
    assert gm_cells(two, "goalies")[goalie.player.name] == "2"
    assert 'title="team games">Gm</th>' in two.split('class="data goalies"', 1)[1]
    one = c.get("/rosters").text
    assert "Sat vs NYR" not in one
    assert gm_cells(one, "goalies")[goalie.player.name] == "1"


# Show more


def _paged(c: TestClient) -> tuple[str, str]:
    page = c.get("/players?owner=all&tonight=2&sort=name").text
    links = show_more(page)
    assert links is not None
    return links


@pytest.mark.parametrize("change", ["label", "unavailable"])
def test_a_change_only_in_tomorrow_reloads_the_two_day_list(change: str) -> None:
    clock = FakeClock(NOW)
    source, c, svc = two_day_setup(GAMES, clock=clock)
    _, htmx = _paged(c)
    asyncio.run(svc.repo.delete(COLLECTION, TOMORROW.isoformat()))
    if change == "label":
        source.by_day[TOMORROW] = (replace(TOMORROW_GAMES[0], start_utc=SAT_7PM + 3600),)
    else:
        source.by_day[TOMORROW] = None
    response = c.get(htmx)
    assert response.status_code == 200
    assert "The schedule changed." in response.text
    assert "Tonight's schedule changed" not in unescape(response.text)
    assert partial_names(response.text) == []
    assert response.text.count("<tr>") == 1


def test_an_unchanged_two_day_slate_appends_the_next_page() -> None:
    _, c = two_day_client(GAMES)
    plain, htmx = _paged(c)
    slate = params(htmx)["slate"]
    assert re.fullmatch(r"[0-9a-f]{12}", slate)
    part = c.get(htmx).text
    assert partial_names(part)
    assert "Sat vs NYR 7:00p" in c.get(plain).text


# The game count


def test_players_gm_column_shows_each_teams_games_only_with_the_option() -> None:
    _, c = two_day_client()
    two = c.get(f"/players?owner=all&tonight=2&{EVERY_ROW}").text
    cells = gm_cells(two, "players")
    assert cells[team_player("BOS")] == "2"
    assert cells[team_player("NYR")] == "1"
    assert cells[team_player("TBL")] == "1"
    one = gm_cells(c.get(f"/players?owner=all&tonight=1&{EVERY_ROW}").text, "players")
    assert one[team_player("BOS")] == "1"
    assert set(one.values()) == {"1"}
    plain = c.get(f"/players?owner=all&{EVERY_ROW}").text
    assert ">Gm<" not in plain


def test_players_gm_header_is_plain_and_no_link_sorts_by_it() -> None:
    _, c = two_day_client(GAMES)
    page = c.get("/players?tonight=2").text
    assert '<th class="num" title="team games">Gm</th>' in page
    assert not any("sort=gm" in link for link in player_links(page))
    assert c.get("/players?tonight=2&sort=gm").status_code == 400


def test_players_gm_shows_with_an_unlit_chip_as_dashes() -> None:
    _, c = two_day_client(None, None)
    for query in ("tonight=1", "tonight=2"):
        page = c.get(f"/players?owner=all&{query}&{EVERY_ROW}").text
        assert set(gm_cells(page, "players").values()) == {"—"}


def test_matchup_goalie_tables_have_no_gm_column() -> None:
    _, c = two_day_client()
    page = c.get("/matchup?tonight=2").text
    assert "goalies" in page
    assert ">Gm<" not in page


def test_colspans_follow_the_gm_column() -> None:
    _, c = two_day_client(GAMES)
    empty = c.get("/players?tonight=2&min_gp=9999").text
    head = empty.split('class="data players"', 1)[1].split("</thead>", 1)[0]
    columns = len(re.findall(r"<th\b", head))
    assert columns == 7 + 2 + 1
    assert f'<td colspan="{columns}" class="muted">No players match.' in empty
    page = c.get("/players?owner=all&tonight=2&sort=name").text
    assert f'<tr class="more"><td colspan="{columns}">' in page
    links = show_more(page)
    assert links is not None
    late = c.get(links[1].replace(f"night={DAY.isoformat()}", "night=2026-11-26")).text
    assert f'colspan="{columns}"' in late
    assert "new night" in late


# Notes


def test_both_days_with_games_name_both_in_the_times_line() -> None:
    _, c = two_day_client()
    page = unescape(c.get("/players?tonight=2").text)
    assert "Tonight's and Sat's games (times ET)" in page
    assert "No games" not in page


def test_an_empty_tomorrow_says_so() -> None:
    _, c = two_day_client(tomorrow_games=())
    page = unescape(c.get("/players?tonight=2").text)
    assert "No games Sat." in page
    assert "Tonight's games (times ET)" in page


def test_the_weekday_comes_from_tomorrows_date_across_a_week_boundary() -> None:
    sunday = datetime.fromisoformat("2026-11-29T16:00:00Z").timestamp()
    svc = make_services(FakeClock(sunday))
    svc.schedule_source = DaySource(
        {date(2026, 11, 29): (), date(2026, 11, 30): (Game("TBL", "BOS", None, 2),)}
    )
    page = unescape(client(svc).get(f"/players?owner=all&tonight=2&{EVERY_ROW}").text)
    assert "Mon's games (times ET)" in page
    assert '<span class="game-mark muted">Mon @BOS</span>' in page
    assert [weekday(DAY + timedelta(days=i)) for i in range(7)] == [
        "Fri",
        "Sat",
        "Sun",
        "Mon",
        "Tue",
        "Wed",
        "Thu",
    ]


def test_one_day_notes_are_unchanged() -> None:
    from fha.services.schedule import Tonight

    assert schedule_notes([Tonight(DAY, {}, "unavailable")]) == [
        "Tonight's schedule is unavailable."
    ]
    assert schedule_notes([Tonight(DAY, {}, "stale", 7 * 3600)]) == [
        "Schedule from 7 h ago.",
        "No games tonight.",
    ]


# Rosters


def test_rosters_chips_default_to_tonight_and_the_picker_carries_the_option() -> None:
    _, c = two_day_client()
    for query in ("", "?tonight=1", "?tonight=x"):
        page = c.get(f"/rosters{query}").text
        assert chip_state(page) == {"Tonight": True, "Tonight + tomorrow": False}
    two = c.get("/rosters?tonight=2&view=cats").text
    assert chip_state(two) == {"Tonight": False, "Tonight + tomorrow": True}
    tonight_link = chip_link(two, "Tonight")
    assert tonight_link.startswith("/rosters")
    assert params(tonight_link) == {"view": "cats"}
    picker = two.split("team-picker", 1)[1].split("</form>", 1)[0]
    assert '<input type="hidden" name="tonight" value="2">' in picker
    one = c.get("/rosters?view=cats").text
    assert params(chip_link(one, "Tonight + tomorrow")) == {"view": "cats", "tonight": "2"}


def test_rosters_two_day_line_counts_games_and_players_outside_ir() -> None:
    skaters = [e for e in MINE.roster if not e.player.is_goalie]
    b2b = replace(skaters[0], player=replace(skaters[0].player, nhl_team="BOS"))
    tomorrow_only = replace(
        skaters[1], player=replace(skaters[1].player, nhl_team="NYR"), selected_position="BN"
    )
    idle = replace(skaters[2], player=replace(skaters[2].player, nhl_team="ANA"))
    on_ir = replace(
        skaters[3], player=replace(skaters[3].player, nhl_team="BOS"), selected_position="IR"
    )
    c = roster_client(replace(MINE, roster=(b2b, tomorrow_only, idle, on_ir)))
    two = c.get("/rosters?tonight=2").text
    assert "Tonight + tomorrow: 3 games for 2 of 3 players" in two
    cells = gm_cells(two, "roster")
    assert cells[b2b.player.name] == "2"
    assert cells[tomorrow_only.player.name] == "1"
    assert cells[idle.player.name] == "0"
    assert cells[on_ir.player.name] == "2"
    assert "Sat vs NYR 7:00p" in row_for(two, "roster", b2b.player.name)
    one = c.get("/rosters").text
    assert "Tonight: 1 of 3 on teams with a game" in one
    assert gm_cells(one, "roster")[b2b.player.name] == "1"


# Demo


def test_demo_two_day_option_on_consecutive_dates_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(NOW)
    monkeypatch.setattr(context, "SystemClock", lambda: clock)

    def no_http(request: httpx.Request) -> httpx.Response:
        raise AssertionError("The demo should never fetch the NHL schedule")

    http = httpx.AsyncClient(transport=httpx.MockTransport(no_http))
    try:
        svc = context.demo_services(ENV, http, SETTINGS)
        c = client(svc)
        for _ in range(2):
            players = c.get(f"/players?owner=all&tonight=2&{EVERY_ROW}").text
            assert chip_state(players)["Tonight + tomorrow"]
            assert "2" in gm_cells(players, "players").values()
            rosters = c.get("/rosters?tonight=2").text
            assert "Tonight + tomorrow:" in rosters
            clock.t += 24 * 3600
    finally:
        asyncio.run(http.aclose())


def test_demo_slates_alternate_by_date_and_share_one_team() -> None:
    source = DemoScheduleSource()
    first = asyncio.run(source.games_on(DAY))
    second = asyncio.run(source.games_on(TOMORROW))
    teams = [{t for g in games for t in (g.away, g.home)} for games in (first, second)]
    assert teams[0] != teams[1]
    assert len(teams[0] & teams[1]) == 1
    assert teams[0] | teams[1] <= NHL_TEAMS
    later = asyncio.run(source.games_on(DAY + timedelta(days=2)))
    assert [(g.away, g.home) for g in later] == [(g.away, g.home) for g in first]
