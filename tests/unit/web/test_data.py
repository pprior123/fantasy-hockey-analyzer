"""page_data: the rated view each screen renders, and its labels."""

from dataclasses import replace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from fha.services.league_view import Season
from fha.services.refresh import Cached
from fha.web.data import PageData, page_data, season_param
from tests.unit.web.helpers import T0, FakeClock, make_app, make_services


def request_for(client: TestClient) -> Request:
    return Request({"type": "http", "app": client.app, "headers": []})


async def loaded(season: Season | None = None, **kw: object) -> PageData:
    app = make_app(make_services(FakeClock(T0 + 90)))
    with TestClient(app) as client:
        return await page_data(request_for(client), season=season, **kw)  # type: ignore[arg-type]


async def test_page_data_rates_the_demo_league_with_the_default_season() -> None:
    data = await loaded()
    assert data.view.season is Season.LAST  # week 3: the baseline rule picks last season
    assert (
        data.season_label
        == f"Last season ({data.view.season_year}-{(data.view.season_year + 1) % 100:02d})"
    )
    assert len(data.view.teams) == 8
    assert data.reports == []  # no league sheet read yet
    assert data.stale_note is None


async def test_the_season_toggle_and_labels() -> None:
    data = await loaded(Season.CURRENT)
    assert data.view.season is Season.CURRENT
    assert data.season_label.startswith("This season (")


async def test_last_season_falls_back_when_it_wasnt_fetched() -> None:
    app = make_app()
    with TestClient(app) as client:
        svc = client.app.state.context.services  # type: ignore[attr-defined]
        cached = await svc.refresh.current()
        no_last = replace(cached.snapshot, last_season_stats=None)
        svc.refresh._memory = Cached(no_last, cached.fetched_at)
        data = await page_data(request_for(client), season=Season.LAST)
    assert data.view.season is Season.CURRENT


@pytest.mark.parametrize(
    ("age", "label"),
    [
        (0, "just now"),
        (59, "just now"),
        (60, "1 min ago"),
        (3599, "59 min ago"),
        (3600, "1 h ago"),
        (47 * 3600, "47 h ago"),
        (3 * 86400, "3 days ago"),
        (-5, "just now"),
    ],
)
async def test_refreshed_label(age: float, label: str) -> None:
    data = await loaded()
    assert replace(data, now=data.cached.fetched_at + age).refreshed_label == label


async def test_stale_notes_word_the_two_cases_differently() -> None:
    data = await loaded()
    unsaved = replace(data, cached=replace(data.cached, refresh_error="not saved: HTTP 503"))
    assert unsaved.stale_note == "Fresh from Yahoo, but it couldn't be saved; it will be retried."
    stale = replace(data, cached=replace(data.cached, refresh_error="YahooHTTPError: 503"))
    assert stale.stale_note == "Yahoo couldn't be reached, so this is data from just now."


def test_season_param() -> None:
    assert season_param("current") is Season.CURRENT
    assert season_param("last") is Season.LAST
    assert season_param(None) is None
    assert season_param("") is None
    assert season_param("bogus") is None
