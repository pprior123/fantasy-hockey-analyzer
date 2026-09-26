"""An in-memory ``YahooSource`` for tests of the layers above (SPEC §3)."""

from __future__ import annotations

from dataclasses import replace

from fha.sources.yahoo.models import LeagueSnapshot


class FakeYahooSource:
    """Serves a fixed snapshot and counts how often it was asked."""

    def __init__(self, snapshot: LeagueSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[bool] = []  # the last_season flag of each fetch

    async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
        self.calls.append(last_season)
        return self.snapshot if last_season else replace(self.snapshot, last_season_stats=None)
