"""Realistic synthetic snapshots: the fake Yahoo league read through HttpYahooSource."""

from typing import Any

import httpx

from fha.sources.yahoo.client import YahooClient
from fha.sources.yahoo.models import LeagueSnapshot
from fha.sources.yahoo.oauth import Credentials, Token
from fha.sources.yahoo.source import HttpYahooSource
from tests.unit.yahoo.fake_league import League


class _Store:
    async def load(self) -> Token:
        return Token("access", "refresh", 10_000.0)

    async def save(self, token: Token) -> None:
        raise AssertionError("no refresh expected")


async def synthetic_snapshot(
    *, last_season: bool = True, available: int = 60, **settings: Any
) -> LeagueSnapshot:
    http = httpx.AsyncClient(transport=httpx.MockTransport(League(**settings)))
    client = YahooClient(http, Credentials("c", "s"), _Store(), clock=lambda: 0.0)
    source = HttpYahooSource(client, available=available)
    return await source.fetch_snapshot(last_season=last_season)
