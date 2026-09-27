"""The rated screens on the demo league (SPEC §7.1-7.4): content, filters, sorts, toggles."""

import asyncio
import re
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from fha.domain.matcher import NO_ALIASES
from fha.services.free_agents import import_free_agent_salaries
from fha.services.league_sheet import bind_tab, read_league_sheet
from fha.services.refresh import RefreshService
from fha.sources.league_sheet.parse import parse_sheet
from fha.sources.league_sheet.source import FakeLeagueSheet
from fha.sources.puckpedia import SalaryRow
from fha.sources.yahoo.demo import demo_snapshot
from fha.sources.yahoo.fake import FakeYahooSource
from fha.sources.yahoo.models import LeagueSnapshot, Matchup, Scoreboard
from fha.storage.memory import InMemoryRepository
from fha.web.context import Services
from fha.web.format import money, number, percentile, rating_settings, signed, url
from tests.unit.league_sheet import sheets as s
from tests.unit.web.helpers import FakeClock, logged_in, make_app, make_services

FOOTER = "Fantasy data provided by Yahoo Fantasy"
SNAP = demo_snapshot()
MINE = SNAP.my_team
assert MINE is not None
FA_PRICED = [p for p in SNAP.available[:3]]


def services_for(snapshot: LeagueSnapshot) -> Services:
    clock = FakeClock()
    repo = InMemoryRepository()
    return Services(repo, RefreshService(FakeYahooSource(snapshot), repo, clock), clock)


def seed(
    svc: Services,
    *,
    salary: int = 1_000_000,
    skip: int = 0,
    misspell: str | None = None,
    unpriced: str | None = None,
) -> None:
    """A sheet tab for my team (bound), and cap hits for a few free agents. ``misspell``:
    a player whose row's name no match finds, so it waits for review. ``unpriced``: a
    player whose row's salary cell is "???"."""
    counted, ir = [], []
    for entry in MINE.roster[skip:]:
        p = entry.player
        name = f"Zqx {p.name}" if p.player_id == misspell else p.name
        cell = "???" if p.player_id == unpriced else salary
        row = s.Player(name, p.eligible_positions[0], p.nhl_team, cell)
        (ir if entry.in_ir_slot else counted).append((entry, row))
    tab = s.TeamTab("Mine", [r for _, r in counted], below=[s.IRRow("IR", r) for _, r in ir])

    async def go() -> None:
        await read_league_sheet(svc.repo, FakeLeagueSheet(parse_sheet(s.grid(tab))), svc.clock)
        await bind_tab(svc.repo, "Mine", MINE.team_key)
        rows = [
            SalaryRow(p.name, p.eligible_positions[0], None, 750_000 * (i + 1), i + 2)
            for i, p in enumerate(FA_PRICED)
        ]
        await import_free_agent_salaries(svc.repo, rows, SNAP.pool, NO_ALIASES)

    asyncio.run(go())


def client(svc: Services | None = None) -> TestClient:
    return logged_in(make_app(svc or make_services()))


def names_in(html: str, table: str) -> list[str]:
    """The player names in a table's first column, in order."""
    body = html.split(f'class="data {table}"', 1)[1].split("</table>", 1)[0]
    cells = re.findall(r'<td class="first">(.*?)</td>', body, flags=re.S)
    names = []
    for cell in cells:
        text = re.sub(r"<dl.*?</dl>|<span.*?</span>|<[^>]+>", " ", cell, flags=re.S)
        names.append(" ".join(text.split()))
    return names


# ---------------------------------------------------------------- formatting


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (None, "—"),
        (7_250_000, "$7.25M"),
        (-4_000_000, "-$4.00M"),
        (1_125_000, "$1.13M"),  # half-up in decimal, not a binary float's $1.12M
        (1_135_000, "$1.14M"),
        (925_000, "$0.925M"),  # under $1M: three decimals, so $925,000 isn't $0.93M
        (999_499, "$0.999M"),
        (999_500, "$1.00M"),
        (-4_000, "-$0.004M"),  # a team $4,000 over the cap never shows as "-$0.00M"
        (-400, "-$400"),
        (1_000, "$0.001M"),
        (999.5, "$0.001M"),  # rounds to $1,000: millions, not "$1000"
        (999.4, "$999"),
        (-0.4, "$0"),  # rounds to nothing: no sign
        (400.4, "$400"),
        (0, "$0"),
        (float("inf"), "—"),
    ],
)
def test_money(amount: float | None, text: str) -> None:
    assert money(amount) == text


def test_numbers_and_urls() -> None:
    assert (number(None), number(1.234), number(1.5, 0), number(2.5, 0)) == ("—", "1.23", "2", "3")
    assert (number(1.005), number(-0.001), number(float("nan"))) == ("1.01", "0.00", "—")
    assert (signed(None), signed(0.5), signed(-0.25, 1)) == ("—", "+0.50", "-0.3")
    assert (signed(0.0), signed(-0.001), signed(float("inf"))) == ("+0.00", "-0.00", "—")
    assert (percentile(None), percentile(87.4), percentile(86.5)) == ("—", "87", "87")
    assert url("/p", {"a": "1", "b": ""}, c="2", a=None) == "/p?c=2"
    assert url("/p", {}) == "/p"
    from fha.domain.engine import DivisorMethod, EngineConfig

    assert rating_settings(EngineConfig()) == "workbook top-20, floor 2%"
    per82 = EngineConfig(divisor_method=DivisorMethod.TOP_PER82, gp_floor_fraction=0.05)
    assert rating_settings(per82) == "per-82 top-10, floor 5%"
    assert rating_settings(EngineConfig(gp_floor_fraction=0.025)).endswith("floor 2.5%")
    assert rating_settings(EngineConfig(gp_floor_fraction=0.0125)).endswith("floor 1.25%")


# ---------------------------------------------------------------- players


def test_players_default_view() -> None:
    html = client().get("/players").text
    assert "Last season (2025-26)" in html  # week 3: the baseline rule
    assert "Ratings: workbook top-20, floor 2%" in html
    assert FOOTER in html
    assert 'action="/refresh"' in html
    assert ">This season</a>" in html  # the season toggle
    assert "<details><summary>" in html  # tap a row: the norm breakdown
    assert ">AAV</a>" in html  # the salary columns by default
    assert "—" in html  # no salaries imported: every AAV is unknown
    assert "Rank and percentile are over the whole league" in html


def test_players_season_toggle() -> None:
    html = client().get("/players?season=current").text
    assert "This season (2026-27)" in html


@pytest.mark.parametrize(
    ("query", "check"),
    [
        (
            "owner=mine",
            lambda rows: (
                set(rows)
                == {e.player.name for e in MINE.roster if not e.player.is_goalie}
                | {e.player.name for e in MINE.roster if e.player.is_goalie}
            ),
        ),
        ("owner=free", lambda rows: len(rows) == len(SNAP.available)),
        ("owner=taken", lambda rows: len(rows) == sum(len(t.roster) for t in SNAP.teams)),
        (
            f"team={SNAP.teams[1].team_key}",
            lambda rows: set(rows) == {e.player.name for e in SNAP.teams[1].roster},
        ),
        (
            "pos=G",
            lambda rows: (
                rows and all(n in {p.name for p in SNAP.pool if p.is_goalie} for n in rows)
            ),
        ),
        ("min_gp=60&season=last", lambda rows: 0 < len(rows) < len(SNAP.pool)),
    ],
)
def test_players_filters(query: str, check: Any) -> None:
    html = client().get(f"/players?{query}").text
    assert check(names_in(html, "players")), query


@pytest.mark.parametrize(("view", "columns"), [("", 9), ("&view=cats", 14)])
def test_an_empty_table_spans_all_its_columns(view: str, columns: int) -> None:
    html = client().get(f"/players?min_gp=9999{view}").text
    assert f'<td colspan="{columns}" class="muted">No players match.' in html
    head = html.split('class="data players"', 1)[1].split("</thead>", 1)[0]
    assert len(re.findall(r"<th\b", head)) == columns


def test_players_owner_chip_is_highlighted() -> None:
    html = client().get("/players?owner=free").text
    assert 'class="chip on" href="/players?owner=free">Free Agents' in html


@pytest.mark.parametrize(
    "key", ["name", "pos", "team", "owner", "gp", "ttltst", "pctl", "aav", "value", "G"]
)
def test_every_column_sorts_and_its_header_flips_the_direction(key: str) -> None:
    extra = "&view=cats" if key == "G" else ""  # category columns show in the Categories view
    response = client().get(f"/players?sort={key}{extra}")
    assert response.status_code == 200
    html = response.text
    assert re.search(rf'<th class="[^"]*sorted[^"]*">\s*<a href="[^"]*sort={key}[^"]*dir=', html)


def test_sorting_by_name_ascending_orders_the_rows() -> None:
    rows = names_in(client().get("/players?sort=name&dir=asc").text, "players")
    assert rows == sorted(rows, key=str.casefold)
    down = names_in(client().get("/players?sort=name&dir=desc").text, "players")
    assert down == sorted(down, key=str.casefold, reverse=True)


def test_the_categories_toggle_swaps_salary_columns_for_norms() -> None:
    money_view = client().get("/players").text
    cats = client().get("/players?view=cats").text
    assert "$/TTLTST" in money_view
    assert "$/TTLTST" not in cats
    for cat in ("PPP", "PIM", "HIT", "SOG", "BLK"):
        assert f"sort={cat}" in cats


@pytest.mark.parametrize(
    "query",
    [
        "pos=W",
        "owner=someone",
        "view=bogus",
        "team=nope",
        "min_gp=x",
        "sort=salary",
        "season=bogus",
        "min_gp=" + "9" * 4301,  # M4R1B-9: int() of a huge string was a 500
    ],
)
def test_bad_player_queries_are_a_400_page(query: str) -> None:
    response = client().get(f"/players?{query}")
    assert response.status_code == 400
    assert 'class="error"' in response.text
    assert FOOTER in response.text


def test_salaries_show_as_millions_once_imported() -> None:
    svc = make_services()
    seed(svc)
    html = client(svc).get("/players?owner=free&sort=aav&dir=asc").text
    assert "$0.750M" in html
    assert "$1.50M" in html


# ---------------------------------------------------------------- refresh


def test_refresh_forces_a_fetch_and_goes_back() -> None:
    snap = demo_snapshot()
    source = FakeYahooSource(snap)
    repo = InMemoryRepository()
    clock = FakeClock()
    svc = Services(repo, RefreshService(source, repo, clock), clock)
    c = client(svc)
    c.get("/players")
    before = len(source.calls)
    response = c.post("/refresh", data={"next": "/league?season=last"}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/league?season=last")
    assert len(source.calls) == before + 1


@pytest.mark.parametrize("target", ["//evil.example", "https://evil.example/", ""])
def test_refresh_never_redirects_off_site(target: str) -> None:
    response = client().post("/refresh", data={"next": target}, follow_redirects=False)
    assert response.headers["location"] == "/players"


class FlakySource(FakeYahooSource):
    """Answers once, then Yahoo is down."""

    async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
        if self.calls:
            self.calls.append(last_season)
            raise ConnectionError("Yahoo is down")
        return await super().fetch_snapshot(last_season=last_season)


def test_a_failed_refresh_says_so_on_the_page_it_returns_to() -> None:
    """M4R1B-2: the cache is still fresh, so the page after a failed Refresh used to
    look like a success. Now it carries the failure until the retry window passes."""
    clock = FakeClock()
    repo = InMemoryRepository()
    svc = Services(repo, RefreshService(FlakySource(demo_snapshot()), repo, clock), clock)
    c = client(svc)
    assert "Refreshing from Yahoo failed" not in c.get("/rosters").text
    clock.t += 120
    response = c.post("/refresh", data={"next": "/rosters"})
    assert response.status_code == 200
    assert str(response.url).endswith("/rosters")
    assert (
        "Refreshing from Yahoo failed (ConnectionError), so this is data from 2 min ago."
        in response.text
    )
    clock.t += 61
    assert "Refreshing from Yahoo failed" not in c.get("/rosters").text  # fresh again, not retried


def test_without_any_yahoo_data_the_screens_say_so_instead_of_crashing() -> None:
    """M4R1A-1: no cache and Yahoo failing (here a timeout) is a 503 page with the
    reason, the nav and a pointer to Admin, on every rated screen and on Refresh."""

    class TimingOut(FakeYahooSource):
        async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
            raise httpx.ConnectTimeout("timed out")

    clock = FakeClock()
    repo = InMemoryRepository()
    svc = Services(repo, RefreshService(TimingOut(SNAP), repo, clock), clock)
    c = client(svc)
    paths = ["/players", "/rosters", "/rosters/replace?drop=1", "/league", "/matchup"]
    for path in [*paths, "/matchup/free-agents"]:
        response = c.get(path)
        assert response.status_code == 503, path
        assert "No Yahoo data" in response.text
        assert (
            "There&#39;s no Yahoo data to show (RefreshError: refresh failed: ConnectTimeout"
            in (response.text)
        )
        assert "Admin works without it. Yahoo is asked again a minute after" in response.text
        assert "sign-in" not in response.text
        assert 'aria-current="page"' in response.text  # the nav, to reach Admin
    refreshed = c.post("/refresh", data={"next": "/league"})
    assert refreshed.status_code == 503


def test_data_that_cant_be_rated_says_so_not_no_yahoo_data() -> None:
    """M4R11A-2: Yahoo answered, so "No Yahoo data" contradicted the reason."""
    unratable = replace(SNAP, game_stat_categories=())
    response = client(services_for(unratable)).get("/players")
    assert response.status_code == 503
    assert "<h1>Yahoo data can&#39;t be rated</h1>" in response.text
    assert "Yahoo&#39;s data arrived but can&#39;t be rated (" in response.text
    assert "No Yahoo data" not in response.text


def test_a_stale_note_is_shown_when_yahoo_fails() -> None:
    clock = FakeClock()
    repo = InMemoryRepository()
    svc = Services(repo, RefreshService(FlakySource(demo_snapshot()), repo, clock), clock)
    c = client(svc)
    c.get("/players")
    clock.t += 3 * 3600  # past the TTL: the refresh fails, the old data is served
    html = c.get("/players").text
    assert "Refreshing from Yahoo failed (ConnectionError), so this is data from 3 h ago." in html


# ---------------------------------------------------------------- rosters


@pytest.mark.parametrize(
    "path",
    [
        "/rosters?season=bogus",
        "/rosters?view=bogus",
        "/rosters/replace?drop=1&view=x",
        "/league?season=2025",
        "/league?view=list",
        "/matchup?season=next",
        "/matchup/free-agents?season=x",
    ],
)
def test_bad_season_or_view_values_are_a_400_everywhere(path: str) -> None:
    response = client().get(path)
    assert response.status_code == 400
    assert 'class="error"' in response.text
    assert FOOTER in response.text


def test_the_refresh_form_is_not_inside_the_team_picker() -> None:
    """M4R1B-1: a form nested in another is dropped by browsers, so Refresh submitted
    the picker's GET. Each form must close before the next opens."""
    html = client().get("/rosters").text
    forms = re.findall(r"<form\b|</form>", html)
    assert forms == ["<form", "</form>"] * (len(forms) // 2)  # never two opens in a row
    picker = html.split('class="toolbar team-picker"', 1)[1].split("</form>", 1)[0]
    assert "/refresh" not in picker
    refresh = re.search(r'<form method="post" action="/refresh".*?</form>', html, flags=re.S)
    assert refresh is not None
    assert 'name="next" value="/rosters"' in refresh[0]


@pytest.mark.parametrize("path", ["/players", "/rosters", "/league", "/matchup", "/admin"])
def test_no_page_nests_forms(path: str) -> None:
    html = client().get(path).text
    forms = re.findall(r"<form\b|</form>", html)
    assert forms == ["<form", "</form>"] * (len(forms) // 2), path


def test_rosters_defaults_to_my_team_with_payroll_unavailable() -> None:
    html = client().get("/rosters").text
    assert "(my team)" in html
    assert "unavailable" in html
    assert set(names_in(html, "roster")) == {
        e.player.name for e in MINE.roster if not e.player.is_goalie
    }
    assert "Replace</a>" in html
    assert "Profiles compare rates" in html
    assert "TTLTST</th>" in html
    assert 'class="data goalies"' in html
    assert "Raw stats: goalies aren" in html


def test_another_teams_roster_has_no_replace_links() -> None:
    other = SNAP.teams[1]
    html = client().get(f"/rosters?team={other.team_key}").text
    assert other.name in html
    assert "Replace</a>" not in html


def test_an_unknown_team_is_a_400() -> None:
    assert client().get("/rosters?team=nope").status_code == 400


def test_payroll_room_and_over_the_cap() -> None:
    svc = make_services()
    seed(svc, salary=6_000_000)  # 25 counted rows at $6M is over the $119.6M cap
    html = client(svc).get("/rosters").text
    assert "over the cap" in html
    assert 'class="over"' in html
    within = make_services()
    seed(within, salary=1_000_000)
    html = client(within).get("/rosters").text
    assert "over the cap" not in html
    assert 'class="good"' in html
    assert "$119.60M" in html


def seed_without_cap(svc: Services) -> None:
    """My team's tab, bound, in a sheet whose cap cell couldn't be read."""
    tab = s.TeamTab("Mine", [s.Player(e.player.name, "C", "", 1_000_000) for e in MINE.roster])

    async def go() -> None:
        sheet = replace(parse_sheet(s.grid(tab)), cap=None)
        await read_league_sheet(svc.repo, FakeLeagueSheet(sheet), svc.clock)
        await bind_tab(svc.repo, "Mine", MINE.team_key)

    asyncio.run(go())


def test_the_salary_cap_setting_fills_in_for_an_unreadable_cap() -> None:
    """M4R1B-4: SALARY_CAP (SPEC §5) applies only when the sheet gives no cap."""
    svc = make_services()
    seed_without_cap(svc)
    payroll = len(MINE.roster) * 1_000_000
    html = logged_in(make_app(svc, salary_cap=100_000_000)).get("/rosters").text
    assert f"Payroll {money(payroll)} · Cap $100.00M" in html
    assert money(100_000_000 - payroll) in html
    without = logged_in(make_app(svc)).get("/rosters").text
    assert "Cap —" in without  # no cap anywhere: unknown, never $0
    assert 'Room <span class="">—</span>' in without  # M4R7B-4: not green
    replace_page = logged_in(make_app(svc)).get(f"/rosters/replace?drop={my_skater()}").text
    assert "(my cap room is unavailable)" in replace_page  # M4R6B-3: the tab is bound
    assert "room also needs the cap" in logged_in(make_app(svc)).get("/league").text
    sheet_cap = make_services()
    seed(sheet_cap)
    html = logged_in(make_app(sheet_cap, salary_cap=100_000_000)).get("/rosters").text
    assert "Cap $119.60M" in html  # the sheet's own cap wins


def test_a_sheet_discrepancy_shows_a_badge_linking_to_admin() -> None:
    svc = make_services()
    seed(svc, skip=1)  # the first rostered player is missing from the tab
    html = client(svc).get("/rosters").text
    assert '<a class="badge" href="/admin">Sheet differs</a>' in html


@pytest.mark.parametrize("path", ["/rosters", "/league", "/rosters/replace?drop=MY"])
def test_the_categories_toggle_on_rosters_replace_and_league(path: str) -> None:
    """M4R1B-7: SPEC §7 has the toggle on every table: salary columns by default, the
    seven category columns instead with view=cats."""
    svc = make_services()
    seed(svc)
    c = client(svc)
    path = path.replace("MY", my_skater())
    money_view = c.get(path).text
    cats = c.get(path + ("&" if "?" in path else "?") + "view=cats").text
    table = lambda html: html.split('class="data ', 1)[1].split("</thead>", 1)[0]  # noqa: E731
    assert ">Categories</a>" in money_view
    assert 'class="chip on"' in cats.split('aria-label="Columns"', 1)[1].split("</nav>", 1)[0]
    salary = ("AAV", "Room after") if "replace" in path else ("Payroll", "AAV")
    assert any(f">{h}</th>" in table(money_view) for h in salary)
    assert not any(f">{h}</th>" in table(cats) for h in ("AAV", "Payroll", "Room after", "Room"))
    for cat in ("PPP", "HIT", "BLK"):
        assert cat in table(cats)
        assert f">{cat}</th>" not in table(money_view)


def test_links_keep_the_season_and_view() -> None:
    c = client()
    html = c.get("/league?season=current&view=cats").text
    assert "season=current&amp;view=cats&amp;team=" in html
    assert 'href="/players?season=current"' in html  # the nav keeps the season
    assert 'href="/admin"' in html
    roster = c.get("/rosters?season=current&view=cats").text
    assert "view=cats&amp;drop=" in roster


# ---------------------------------------------------------------- replace


def my_skater() -> str:
    return next(
        e.player.player_id for e in MINE.roster if not e.player.is_goalie and not e.in_ir_slot
    )


def test_replace_lists_free_agents_with_deltas() -> None:
    svc = make_services()
    seed(svc)
    html = client(svc).get(f"/rosters/replace?drop={my_skater()}").text
    assert "ΔTTLTST" in html
    assert "Room after" in html
    assert re.search(r'class="num[^"]*">[+-]\d\.\d{3}</td>', html)  # a signed TTLTST delta
    assert "Only swaps that fit the cap" in html
    assert "Back to my roster" in html


def test_the_swap_ok_toggle_counts_unknown_rows() -> None:
    svc = make_services()
    seed(svc)
    html = client(svc).get(f"/rosters/replace?drop={my_skater()}&swap_ok=1").text
    assert "left out: a cap hit or the room isn" in html


@pytest.mark.parametrize(
    "query", ["drop=nope", f"drop={SNAP.teams[1].roster[0].player.player_id}", "drop=1&swap_ok=2"]
)
def test_replace_needs_my_player_and_a_known_toggle(query: str) -> None:
    assert client().get(f"/rosters/replace?{query}").status_code == 400


def test_replacing_a_player_missing_from_the_sheet_frees_nothing() -> None:
    svc = make_services()
    drop = my_skater()
    seed(svc, skip=[e.player.player_id for e in MINE.roster].index(drop) + 1)
    html = client(svc).get(f"/rosters/replace?drop={drop}").text
    assert "(no sheet row matched him: frees nothing)" in html


def test_a_drop_whose_row_may_await_review_has_an_unknown_room_after() -> None:
    """M4R9B-1: his counted row is in match review, so his cap hit is in PAYROLL; treating
    him as freeing nothing understated the room after every swap."""
    svc = make_services()
    drop = my_skater()
    seed(svc, misspell=drop)
    html = client(svc).get(f"/rosters/replace?drop={drop}").text
    assert (
        "counted rows on my tab match no roster player, or share one (see Admin),"
        " so the room after is unknown" in html
    )
    body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    rooms = [row.split('<td class="num')[-1] for row in body.split("</tr>")[:-1]]
    assert rooms
    assert all(">—</td>" in cell for cell in rooms)  # the last cell: Room after
    swaps = client(svc).get(f"/rosters/replace?drop={drop}&swap_ok=1").text
    assert "left out: a cap hit or the room isn" in swaps


def test_an_ir_row_in_review_leaves_a_missing_drop_freeing_nothing() -> None:
    """M4R10B-1: only a *counted* row in review can be the drop's; an IR row frees nothing."""
    svc = make_services()
    ir_player = next(e.player.player_id for e in MINE.roster if e.in_ir_slot)
    drop = my_skater()
    seed(svc, skip=[e.player.player_id for e in MINE.roster].index(drop) + 1, misspell=ir_player)
    html = client(svc).get(f"/rosters/replace?drop={drop}").text
    assert "(no sheet row matched him: frees nothing)" in html
    body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert re.search(r'<td class="num ">\$\d', body)  # a room after, not "—"


def test_a_drop_with_no_cap_hit_on_his_row_says_why_the_room_is_unknown() -> None:
    """M4R11B-2."""
    svc = make_services()
    drop = my_skater()
    seed(svc, unpriced=drop)
    html = client(svc).get(f"/rosters/replace?drop={drop}").text
    assert "(his sheet row has no cap hit, so the room after is unknown)" in html


def test_without_my_tab_replace_says_the_room_is_unavailable() -> None:
    """M4R5B-2: with no sheet every player's ``counts`` is unknown; that isn't
    "not on the sheet"."""
    html = client().get(f"/rosters/replace?drop={my_skater()}").text
    assert "(my cap room is unavailable)" in html
    assert "frees nothing" not in html


# ---------------------------------------------------------------- league


def test_league_has_a_row_per_team_linking_to_rosters() -> None:
    html = client().get("/league").text
    for team in SNAP.teams:
        assert f'team={team.team_key}">{team.name}</a>' in html
    assert html.count('href="/rosters?team=') == len(SNAP.teams)
    assert "Profiles compare rates" in html


def test_league_shows_my_payroll_once_bound() -> None:
    svc = make_services()
    seed(svc)  # $1M a row
    counted = sum(not e.in_ir_slot for e in MINE.roster)
    table = client(svc).get("/league").text.split('class="data league"', 1)[1]
    mine = table.split('<tr class="mine">', 1)[1].split("</tr>", 1)[0]
    assert money(counted * 1_000_000) in mine
    assert money(119_600_000 - counted * 1_000_000) in mine


# ---------------------------------------------------------------- matchup


def test_this_weeks_matchup_highlights_trailing_categories() -> None:
    html = client().get("/matchup").text
    assert "Week 3" in html
    assert " vs " in html
    assert 'class="trailing"' in html
    assert "behind or close" in html
    assert "Profiles compare rates" in html
    assert "Free agents who help here" in html
    assert html.count('class="data goalies"') == 2


def test_next_weeks_matchup() -> None:
    assert "Week 4" in client().get("/matchup?week=next").text


def test_a_bad_week_is_a_400() -> None:
    assert client().get("/matchup?week=later").status_code == 400
    assert client().get("/matchup/free-agents?week=later").status_code == 400
    assert client().get("/matchup/free-agents?fits=yes").status_code == 400


def test_free_agents_who_help_are_sorted_by_need() -> None:
    html = client().get("/matchup/free-agents").text
    needs = [
        float(x)
        for x in re.findall(
            r'<td class="num">(-?\d+\.\d\d)</td>\s*<td class="num">(?:-?\d|—)', html
        )
    ]
    assert needs == sorted(needs, reverse=True)
    assert "Fits my cap" in html


def test_when_no_category_trails_the_list_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    from fha.web.routes import matchup as route

    real = route.matchup_view

    def leading(*args: Any, **kw: Any) -> Any:
        game = real(*args, **kw)
        return replace(game, comparison=replace(game.comparison, trailing=frozenset()))

    monkeypatch.setattr(route, "matchup_view", leading)
    html = client().get("/matchup/free-agents").text
    assert "You lead everywhere" in html


def test_fits_my_cap_counts_the_unknown() -> None:
    svc = make_services()
    seed(svc)
    html = client(svc).get("/matchup/free-agents?fits=1").text
    assert "left out: a cap hit or the room isn" in html


def test_with_no_rated_skaters_the_list_doesnt_claim_a_lead() -> None:
    """M4R5B-1: before opening night with "This season" forced (SPEC §5), no team has a
    profile, so nothing trails; the list said "You lead everywhere"."""
    preseason = replace(SNAP, stats={}, last_season_stats=SNAP.last_season_stats or SNAP.stats)
    html = client(services_for(preseason)).get("/matchup/free-agents?season=current").text
    assert "You lead everywhere" not in html
    assert f"Nothing to compare yet: {MINE.name} has no rated skaters outside IR / IR+" in html
    opp = next(t for t in SNAP.teams if t.team_key == SNAP.scoreboard.opponent(MINE.team_key))
    gone = {e.player.player_key for e in opp.roster}
    empty_opp = replace(SNAP, stats={k: v for k, v in SNAP.stats.items() if k not in gone})
    html = client(services_for(empty_opp)).get("/matchup/free-agents?season=current").text
    assert f"Nothing to compare yet: {opp.name} has no rated skaters" in html


def test_out_of_the_playoffs_the_page_gives_no_reason() -> None:
    """M4R8B-1: the others play, so neither "a bye" nor "no bracket yet" would be true."""
    others = [t.team_key for t in SNAP.teams if not t.is_mine][:2]
    board = Scoreboard(25, None, None, (Matchup(25, (others[0], others[1])),))
    html = (
        client(services_for(replace(SNAP, scoreboard=board, next_scoreboard=None)))
        .get("/matchup")
        .text
    )
    assert "Week 25: Yahoo lists no opponent for you." in html
    assert "bye" not in html
    assert "bracket" not in html


def test_a_bye_and_no_next_week() -> None:
    bye = replace(SNAP, scoreboard=Scoreboard(3, None, None, ()), next_scoreboard=None)
    c = client(services_for(bye))
    assert "Week 3: Yahoo lists no opponent for you." in c.get("/matchup").text
    assert (
        '<td colspan="6" class="muted">No opponent this week' in c.get("/matchup/free-agents").text
    )
    assert (
        '<td colspan="11" class="muted">No opponent this week'
        in c.get("/matchup/free-agents?view=cats").text
    )
    assert "There is no next week" in c.get("/matchup?week=next").text
    assert "No matchup next week" in c.get("/matchup/free-agents?week=next").text
    next_bye = replace(SNAP, next_scoreboard=Scoreboard(4, None, None, ()))
    page = client(services_for(next_bye)).get("/matchup/free-agents?week=next").text
    assert "No opponent next week, so" in page  # M4R4B-7


def test_without_my_team() -> None:
    orphan = replace(SNAP, teams=tuple(replace(t, is_mine=False) for t in SNAP.teams))
    c = client(services_for(orphan))
    assert "Your team isn" in c.get("/matchup").text
    assert "Your team isn" in c.get("/matchup?week=next").text  # M4R6B-1: not "no next week"
    assert "Your team isn" in c.get("/matchup/free-agents?week=next").text  # M4R7B-2
    html = c.get("/rosters").text
    assert SNAP.teams[0].name in html  # the first team instead
    assert c.get(f"/rosters/replace?drop={my_skater()}").status_code == 400


# ---------------------------------------------------------------- auth


@pytest.mark.parametrize(
    "path",
    [
        "/players",
        "/rosters",
        "/rosters/replace?drop=1",
        "/league",
        "/matchup",
        "/matchup/free-agents",
    ],
)
def test_every_screen_needs_a_login(path: str) -> None:
    with TestClient(make_app()) as anonymous:
        response = anonymous.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


def test_refresh_needs_a_login() -> None:
    with TestClient(make_app()) as anonymous:
        response = anonymous.post("/refresh", data={"next": "/league"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_a_negative_salary_on_the_sheet_is_unknown_not_a_crash() -> None:
    """M4R2B-1: a typo'd negative cell crashed every rated screen and Admin."""
    svc = make_services()
    seed(svc, salary=-500_000)
    c = client(svc)
    for path in ("/admin", "/players", "/rosters", "/league", "/matchup", "/matchup/free-agents"):
        assert c.get(path).status_code == 200, path
    mine = c.get("/rosters").text.split('class="data roster"', 1)[1].split("</table>", 1)[0]
    assert "-$" not in mine


@pytest.mark.parametrize("path", ["/rosters/replace?drop=MY", "/matchup/free-agents"])
def test_replace_and_the_need_list_have_the_season_toggle_and_refresh(path: str) -> None:
    """M4R2B-4: SPEC §7: the season toggle is common to the tables."""
    c = client()
    html = c.get(
        path.replace("MY", my_skater()) + ("&" if "?" in path else "?") + "season=current"
    ).text
    assert 'class="chips season-toggle"' in html
    assert 'class="chip on" href="' in html.split("season-toggle", 1)[1].split("</nav>", 1)[0]
    refresh = re.search(r'<form method="post" action="/refresh".*?</form>', html, flags=re.S)
    assert refresh is not None
    assert "season=current" in refresh[0]


@pytest.mark.parametrize(
    ("path", "back"),
    [
        ("/matchup?view=bogus", "/matchup"),
        ("/matchup/free-agents?view=bogus", "/matchup"),
        ("/rosters/replace?drop=1&view=x", "/rosters"),  # not /rosters/replace, itself a 400
        ("/league?season=x", "/league"),
    ],
)
def test_a_bad_value_links_back_to_the_screen(path: str, back: str) -> None:
    response = client().get(path)
    assert response.status_code == 400
    assert f'<a href="{back}">Start over</a>' in response.text


def test_the_replace_back_link_and_the_team_picker_keep_the_view() -> None:
    c = client()
    replace_page = c.get(f"/rosters/replace?drop={my_skater()}&view=cats&season=current").text
    assert 'href="/rosters?season=current&amp;view=cats">Back to my roster' in replace_page
    picker = c.get("/rosters?view=cats&season=last").text.split("team-picker", 1)[1]
    picker = picker.split("</form>", 1)[0]
    assert '<input type="hidden" name="view" value="cats">' in picker
    assert '<input type="hidden" name="season" value="last">' in picker


@pytest.mark.parametrize(("view", "columns"), [("", 9), ("&view=cats", 14)])
def test_empty_roster_and_replace_tables_span_their_columns(view: str, columns: int) -> None:
    empty = replace(
        SNAP,
        teams=tuple(
            replace(t, roster=tuple(e for e in t.roster if e.player.is_goalie)) if t.is_mine else t
            for t in SNAP.teams
        ),
    )
    html = client(services_for(empty)).get(f"/rosters?x=1{view}").text
    head = html.split('class="data roster"', 1)[1].split("</thead>", 1)[0]
    assert len(re.findall(r"<th\b", head)) == columns
    assert f'<td colspan="{columns}" class="muted">No skaters.' in html
    no_fa = replace(SNAP, available=())
    c = client(services_for(no_fa))
    page = c.get(f"/rosters/replace?drop={my_skater()}{view}").text
    head = page.split('class="data replace"', 1)[1].split("</thead>", 1)[0]
    want = len(re.findall(r"<th\b", head))
    assert want == (6 if not view else 11)
    assert f'<td colspan="{want}" class="muted">No free agents at' in page


def badges_of(html: str) -> list[str]:
    """The need list's trailing categories, as its intro lists them."""
    intro = html.split("behind or close in:", 1)[1].split("</p>", 1)[0]
    return re.findall(r'<span class="badge">([A-Z]+)</span>', intro)


def test_the_need_list_has_the_categories_toggle() -> None:
    """M4R3B-1: SPEC §7, "common to the tables": the norms explain a Need score.
    The trailing categories are highlighted, in SPEC order."""
    c = client()
    money_view = c.get("/matchup/free-agents").text
    cats = c.get("/matchup/free-agents?view=cats").text
    head = lambda html: html.split('class="data need"', 1)[1].split("</thead>", 1)[0]  # noqa: E731
    toggle = money_view.split('aria-label="Columns"', 1)[1].split("</nav>", 1)[0]
    assert 'href="/matchup/free-agents?view=cats">Categories</a>' in toggle
    assert ">AAV</th>" in head(money_view)
    assert ">AAV</th>" not in head(cats)
    assert ">Fits</th>" not in head(cats)
    for cat in ("G", "PPP", "BLK"):
        assert f">{cat}</th>" in head(cats)
    assert 'class="num trailing"' in head(cats)
    body = cats.split('class="data need"', 1)[1].split("<tbody>", 1)[1]
    first = body.split("</tr>", 1)[0]
    cells = re.findall(r'<td class="num( trailing)?">([^<]*)</td>', first)
    assert len(cells) == 2 + 7  # Need, TTLTST, then the 7 norms
    norms = cells[2:]
    assert all(re.fullmatch(r"-?\d+\.\d\d", value) for _, value in norms)  # M4R4B-2
    assert sum(1 for trailing, _ in norms if trailing) == len(badges_of(cats))
    order = ["G", "A", "PPP", "PIM", "HIT", "SOG", "BLK"]
    badges = badges_of(cats)
    assert badges == [c for c in order if c in badges]
    assert len(badges) >= 2


def test_the_players_filter_form_keeps_the_view() -> None:
    html = client().get("/players?view=cats&season=last").text
    form = html.split('class="toolbar filters"', 1)[1].split("</form>", 1)[0]
    assert '<input type="hidden" name="view" value="cats">' in form
    assert '<input type="hidden" name="season" value="last">' in form


@pytest.mark.parametrize(
    "query",
    [
        "/league?season=",
        "/league?view=",
        *(f"/players?{k}=" for k in ("owner", "pos", "sort", "dir")),
    ],
)
def test_a_bad_value_is_quoted_short_on_the_400_page(query: str) -> None:
    """M4R3B-6, M4R4B-3: every quoted query value, not only ``season``."""
    response = client().get(query + "x" * 13)  # one past the 12 quoted
    assert response.status_code == 400
    assert "&#39;xxxxxxxxxxxx…&#39;" in response.text
    assert "x" * 13 not in response.text


def test_the_stale_note_says_when_yahoo_is_asked_again() -> None:
    clock = FakeClock()
    repo = InMemoryRepository()
    svc = Services(repo, RefreshService(FlakySource(demo_snapshot()), repo, clock), clock)
    c = client(svc)
    c.get("/players")
    html = c.post("/refresh", data={"next": "/players"}).text
    assert "Yahoo is asked again a minute after a failure." in html
