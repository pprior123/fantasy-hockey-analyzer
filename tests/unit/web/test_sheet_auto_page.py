"""Auto-update triggers, same-origin POSTs and results through the app."""

import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from html import unescape
from threading import Event
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from fha.services.free_agents import row_key
from fha.services.league_sheet import confirm_row
from fha.services.refresh import CACHE, Cached, RefreshService, save_cached
from fha.services.sheet_auto import (
    AUTO_LOG,
    AutoUpdate,
    _record_log,
    checked_generation,
    enabled,
    mark_checked,
    set_enabled,
)
from fha.services.sheet_update import WRITE_ATTEMPTS, WriteJournal
from fha.sources.league_sheet.models import LeagueSheetError
from fha.sources.league_sheet.writer import FakeLeagueSheetWriter, UncertainWriteError
from fha.sources.yahoo.fake import FakeYahooSource
from fha.storage.memory import InMemoryRepository
from fha.storage.repository import RepositoryError
from fha.web.context import Services
from tests.unit.services.test_refresh import FailingSource
from tests.unit.web.helpers import T0, FakeClock, logged_in, make_app
from tests.unit.web.test_screens import MINE, SNAP
from tests.unit.web.test_sheet_update_page import FIRST, LiveSheet, mine_tab, token_in, writable

AUTO = "/sheet/auto-update"
SWITCH = "/admin/sheet/auto"
MANUAL = "/admin/sheet/update"
ORIGIN = {"Origin": "http://testserver"}


def browser(svc: Services, *, demo: bool = False) -> TestClient:
    return logged_in(make_app(svc, demo=demo))


def trigger(html: str) -> float | None:
    found = re.search(r'<div\b[^>]*hx-post="/sheet/auto-update"[^>]*>', html)
    if found is None:
        return None
    element = found[0]
    assert 'hx-trigger="load"' in element
    assert 'hx-swap="outerHTML"' in element
    values = re.search(r"hx-vals='([^']+)'", element)
    assert values is not None
    assert html.index(element) < html.index("<main")
    return float(json.loads(unescape(values[1]))["fetched_at"])


def with_identified_drop() -> tuple[Services, LiveSheet]:
    svc, sheet = writable(mine_tab())
    asyncio.run(confirm_row(svc.repo, "Mine", row_key("Zed Nobody", "C"), "departed"))
    return svc, sheet


def advance(svc: Services, seconds: float) -> float:
    clock = cast(FakeClock, svc.clock)
    clock.t += seconds
    return clock.now()


@pytest.mark.parametrize("path", ["/players", "/rosters", "/league", "/matchup", "/admin"])
def test_full_pages_offer_the_first_check_for_a_snapshot(path: str) -> None:
    svc, sheet = writable(mine_tab())
    c = browser(svc)
    response = c.get(path)
    assert response.status_code == 200
    assert trigger(response.text) == T0
    assert sheet.writes == 0


def test_a_snapshot_saved_before_activation_can_start_an_update() -> None:
    svc, _ = writable(mine_tab())
    source = FakeYahooSource(SNAP)
    svc.refresh = RefreshService(source, svc.repo, svc.clock)
    asyncio.run(save_cached(svc.repo, Cached(SNAP, T0)))
    c = browser(svc)
    assert trigger(c.get("/players").text) == T0
    assert source.calls == []

    asyncio.run(set_enabled(svc.repo, False))
    assert trigger(c.get("/players").text) is None
    asyncio.run(set_enabled(svc.repo, True))
    assert trigger(c.get("/players").text) == T0
    assert source.calls == []


@pytest.mark.parametrize("state", ["checked", "off", "write-off", "demo", "stale", "errored"])
def test_the_page_omits_the_trigger_when_it_cannot_run(state: str) -> None:
    svc, _ = writable(mine_tab())
    if state == "checked":
        asyncio.run(mark_checked(svc.repo, T0))
    elif state == "off":
        asyncio.run(set_enabled(svc.repo, False))
    elif state == "write-off":
        svc.sheet_writer = None
    elif state == "stale":
        svc.refresh = RefreshService(FailingSource(), svc.repo, svc.clock)
        asyncio.run(save_cached(svc.repo, Cached(SNAP, T0 - 1800)))
    elif state == "errored":
        svc.refresh = RefreshService(FailingSource(), svc.repo, svc.clock)
        asyncio.run(save_cached(svc.repo, Cached(SNAP, T0)))
        asyncio.run(svc.refresh.current(force=True))
    response = browser(svc, demo=state == "demo").get("/players")
    assert response.status_code == 200
    assert trigger(response.text) is None


def test_refresh_and_the_first_full_page_after_a_fragment_or_preview() -> None:
    svc, _ = writable(mine_tab())
    c = browser(svc)
    assert trigger(c.get("/players").text) == T0
    asyncio.run(mark_checked(svc.repo, T0))
    refreshed_at = advance(svc, 1)
    assert trigger(c.post("/refresh", data={"next": "/admin"}).text) == refreshed_at

    for non_trigger_page in (MANUAL, "/players/rows"):
        refreshed_at = advance(svc, 1800)
        fragment = c.get(non_trigger_page)
        assert fragment.status_code == 200
        assert trigger(fragment.text) is None
        assert asyncio.run(svc.refresh.saved_generation()) == refreshed_at
        assert trigger(c.get("/players").text) == refreshed_at


@pytest.mark.parametrize("demo", [False, True], ids=["write-off", "demo"])
def test_both_auto_routes_are_unavailable_when_writing_is_off(demo: bool) -> None:
    svc, _ = writable(mine_tab())
    if not demo:
        svc.sheet_writer = None
    c = browser(svc, demo=demo)
    assert c.post(AUTO, data={"fetched_at": T0}, headers=ORIGIN).status_code == 404
    assert c.post(SWITCH, data={"enabled": "1"}, headers=ORIGIN).status_code == 404
    html = c.get("/admin").text
    assert "Auto-update my tab" not in html
    assert "Automatic updates" not in html


@pytest.mark.parametrize("path", [AUTO, SWITCH])
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://elsewhere.test"},
        {"Origin": "http://sub.testserver", "Sec-Fetch-Site": "same-site"},
        {"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"},
    ],
    ids=["neither", "cross-origin", "same-site", "mismatch-ignores-fallback"],
)
def test_the_auto_endpoints_reject_wrong_or_missing_origin(
    path: str, headers: dict[str, str]
) -> None:
    svc, sheet = writable(mine_tab())
    c = browser(svc)
    response = c.post(path, data={"fetched_at": T0, "enabled": "1"}, headers=headers)
    assert response.status_code == 403
    assert sheet.writes == 0


def test_login_and_htmx_login_redirects_for_the_automatic_endpoint() -> None:
    svc, sheet = writable(mine_tab())
    c = TestClient(make_app(svc, demo=False))
    response = c.post(AUTO, data={"fetched_at": T0}, headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?")
    htmx = c.post(AUTO, data={"fetched_at": T0}, headers={**ORIGIN, "HX-Request": "true"})
    assert htmx.status_code == 401
    assert htmx.headers["HX-Redirect"].startswith("/login?")
    assert sheet.writes == 0


def test_the_admin_switch_saves_and_the_log_lists_newest_entries_first() -> None:
    svc, _ = writable(mine_tab())
    c = browser(svc)
    initial = c.get("/admin").text
    assert "Auto-update my tab" in initial
    assert "Automatic updates" in initial
    assert "None yet." in initial
    assert re.search(r'name="enabled" value="1" checked\b', initial)
    off = c.post(SWITCH, headers={"Sec-Fetch-Site": "same-origin"})
    assert off.status_code == 200
    assert not asyncio.run(enabled(svc.repo))
    assert "Auto-update my tab is off" in off.text
    assert not re.search(r'name="enabled" value="1" checked\b', off.text)
    on = c.post(SWITCH, data={"enabled": "1"}, headers=ORIGIN)
    assert on.status_code == 200
    assert asyncio.run(enabled(svc.repo))
    assert "Auto-update my tab is on" in on.text

    asyncio.run(_record_log(svc.repo, AutoUpdate(T0 - 3, "failed", "not_sent", "older reason")))
    asyncio.run(_record_log(svc.repo, AutoUpdate(T0 - 2, "skipped", "guard", "newer reason")))
    log = c.get("/admin").text
    assert log.index("newer reason") < log.index("older reason")


def test_written_and_silent_fragments_use_the_actual_saved_snapshot() -> None:
    svc, sheet = with_identified_drop()
    c = browser(svc)
    fetched_at = trigger(c.get("/players").text)
    assert fetched_at == T0
    response = c.post(AUTO, data={"fetched_at": fetched_at}, headers=ORIGIN)
    assert response.status_code == 200
    assert sheet.writes == 1
    assert "Your tab was updated" in response.text
    assert "drop Zed Nobody" in response.text
    assert "(3 cells)" in response.text
    assert "PAYROLL is now $26,000,000" in response.text
    assert 'href="/admin#automatic-updates">Log</a>' in response.text
    assert "<html" not in response.text
    assert c.post(AUTO, data={"fetched_at": fetched_at}, headers=ORIGIN).text == ""
    assert trigger(c.get("/players").text) is None


@pytest.mark.parametrize("case", ["skipped", "failed", "unknown", "storage"])
def test_other_outcomes_are_escaped_fragments_with_the_manual_link(
    case: str, monkeypatch: Any
) -> None:
    svc, sheet = writable(mine_tab()) if case == "skipped" else with_identified_drop()
    c = browser(svc)
    assert trigger(c.get("/players").text) == T0
    hidden = "SENTINEL_NOT_FOR_THE_PAGE"
    if case == "failed":

        async def unreadable() -> Any:
            raise LeagueSheetError(hidden)

        monkeypatch.setattr(sheet, "fetch", unreadable)
    elif case == "unknown":
        svc.sheet_writer = FakeLeagueSheetWriter(UncertainWriteError(hidden))
    elif case == "storage":

        async def down() -> Any:
            raise RepositoryError(hidden)

        monkeypatch.setattr(svc.refresh, "saved_generation", down)
    response = c.post(AUTO, data={"fetched_at": T0}, headers=ORIGIN)
    assert response.status_code == 200
    assert hidden not in response.text
    assert hidden not in repr(asyncio.run(svc.repo.all(AUTO_LOG)))
    assert f'href="{MANUAL}"' in response.text
    assert "<html" not in response.text
    if case == "unknown":
        assert "may not have gone through" in response.text
    else:
        assert "wasn't updated automatically" in unescape(response.text)
    if case == "skipped":
        assert re.search(r"row \d+ isn't matched", unescape(response.text))
        assert "Zed Nobody" not in response.text
        assert "Zed Nobody" not in repr(asyncio.run(svc.repo.all(AUTO_LOG)))


def test_the_manual_confirm_marks_the_forced_snapshot_and_can_override_an_attempt() -> None:
    svc, sheet = with_identified_drop()
    asyncio.run(WriteJournal(svc.repo, "manual").record("other", T0, 4))
    c = browser(svc)
    preview = c.get(MANUAL).text
    assert "An earlier write's outcome isn't known yet" in unescape(preview)
    fetched_at = advance(svc, 1)
    confirmed = c.post(MANUAL, data={"token": token_in(preview)})
    assert confirmed.status_code == 200
    assert "PAYROLL is now" in confirmed.text
    assert sheet.writes == 1
    assert asyncio.run(checked_generation(svc.repo)) == fetched_at
    assert trigger(confirmed.text) is None
    assert trigger(c.get("/players").text) is None
    assert set(asyncio.run(svc.repo.all(WRITE_ATTEMPTS))) == {"other"}


def test_a_lost_manual_write_blocks_the_later_automatic_post(monkeypatch: Any) -> None:
    svc, sheet = with_identified_drop()

    async def lost(*args: Any, on_send: Any = None, **kwargs: Any) -> None:
        if on_send is not None:
            on_send()
        sheet.writes += 1
        raise UncertainWriteError("the answer was lost")

    monkeypatch.setattr(sheet, "write", lost)
    c = browser(svc)
    preview = c.get(MANUAL).text
    response = c.post(MANUAL, data={"token": token_in(preview)})
    assert "may still apply it" in response.text
    [record] = asyncio.run(svc.repo.all(WRITE_ATTEMPTS)).values()
    assert (record["source"], record["cells"]) == ("manual", 3)
    fetched_at = advance(svc, 1)
    next_page = c.post("/refresh", data={"next": "/players"})
    assert trigger(next_page.text) == fetched_at
    skipped = c.post(AUTO, data={"fetched_at": fetched_at}, headers=ORIGIN)
    assert "an earlier write's outcome isn't known yet" in unescape(skipped.text)
    assert sheet.writes == 1


def test_the_automatic_post_silently_ignores_an_undecodable_cache() -> None:
    svc, sheet = with_identified_drop()
    yahoo = FakeYahooSource(SNAP)
    svc.refresh = RefreshService(yahoo, svc.repo, svc.clock)
    asyncio.run(save_cached(svc.repo, Cached(SNAP, T0)))
    meta = asyncio.run(svc.repo.get(CACHE, "meta"))
    assert meta is not None
    asyncio.run(svc.repo.put(CACHE, "meta", {**meta, "format": 999}))
    response = browser(svc).post(AUTO, data={"fetched_at": T0}, headers=ORIGIN)
    assert response.status_code == 200
    assert response.text == ""
    assert yahoo.calls == []
    assert sheet.writes == 0


def test_the_automatic_post_uses_the_old_saved_roster_while_a_new_save_is_stalled() -> None:
    entered, release = Event(), Event()

    class StalledSave(InMemoryRepository):
        async def replace_all(self, collection: str, docs: Any) -> None:
            if collection == CACHE and docs["meta"]["fetched_at"] == T0 + 1:
                entered.set()
                if not await asyncio.to_thread(release.wait, 5):
                    raise RuntimeError("test did not release the save")
            await super().replace_all(collection, docs)

    svc, sheet = with_identified_drop()
    repo = StalledSave()
    repo.collections = cast(InMemoryRepository, svc.repo).collections
    svc.repo = repo
    new_roster = replace(MINE, roster=MINE.roster[1:])  # B would not add FIRST
    newer = replace(SNAP, teams=tuple(new_roster if t == MINE else t for t in SNAP.teams))
    yahoo = FakeYahooSource(newer)
    svc.refresh = RefreshService(yahoo, repo, svc.clock)
    asyncio.run(save_cached(repo, Cached(SNAP, T0)))
    c = browser(svc)
    advance(svc, 1)
    with ThreadPoolExecutor(max_workers=1) as runner:
        refreshing = runner.submit(
            c.post, "/refresh", data={"next": "/players"}, follow_redirects=False
        )
        try:
            assert entered.wait(5)
            response = c.post(AUTO, data={"fetched_at": T0}, headers=ORIGIN)
            assert response.status_code == 200
            assert "Your tab was updated" in response.text
            assert sheet.writes == 1
            assert FIRST.last_name in unescape(response.text)
            assert yahoo.calls == [True]  # the one in /refresh, none in /sheet/auto-update
        finally:
            release.set()
        assert refreshing.result(timeout=5).status_code == 303
