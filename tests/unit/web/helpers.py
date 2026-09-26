"""A test app on the demo league, and a logged-in client."""

from dataclasses import replace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fha.services.refresh import RefreshService
from fha.sources.yahoo.demo import demo_snapshot
from fha.sources.yahoo.fake import FakeYahooSource
from fha.storage.memory import InMemoryRepository
from fha.web.app import create_app
from fha.web.context import AppContext, Services, Settings

PASSWORD = "correct horse battery staple"  # noqa: S105 - a test password
SETTINGS = Settings(app_password=PASSWORD, session_secret="s" * 32, secure_cookies=False, demo=True)
T0 = 1_800_000_000.0


class FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.t = now

    def now(self) -> float:
        return self.t


def make_services(clock: FakeClock | None = None) -> Services:
    clock = clock or FakeClock()
    repo = InMemoryRepository()
    return Services(repo, RefreshService(FakeYahooSource(demo_snapshot()), repo, clock), clock)


def make_app(services: Services | None = None, **settings: Any) -> FastAPI:
    return create_app(
        AppContext.with_services(replace(SETTINGS, **settings), services or make_services())
    )


def logged_in(app: FastAPI) -> TestClient:
    client = TestClient(app)
    client.__enter__()  # run the lifespan
    response = client.post(
        "/login", data={"password": PASSWORD, "next": "/players"}, follow_redirects=False
    )
    assert response.status_code == 303
    return client
