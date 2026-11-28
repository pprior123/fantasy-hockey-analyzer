"""An in-memory ``YahooSource`` for tests of the layers above (SPEC §3)."""

from __future__ import annotations

from dataclasses import replace

from fha.sources.yahoo.models import LeagueSnapshot, Rosters


class FakeYahooSource:
    """Serves a fixed snapshot and counts how often it was asked.

    ``rosters``: what a roster-only read returns; by default the snapshot's teams.
    """

    def __init__(self, snapshot: LeagueSnapshot, *, rosters: Rosters | None = None) -> None:
        self.snapshot = snapshot
        self.rosters = rosters
        self.calls: list[bool] = []  # the last_season flag of each fetch
        self.roster_calls: list[str] = []  # the league key of each roster read
        self.settles: list[float] = []  # the timeout of each settle

    async def fetch_snapshot(self, *, last_season: bool = True) -> LeagueSnapshot:
        self.calls.append(last_season)
        return self.snapshot if last_season else replace(self.snapshot, last_season_stats=None)

    async def fetch_rosters(self, league_key: str) -> Rosters:
        self.roster_calls.append(league_key)
        return self.rosters if self.rosters is not None else Rosters(self.snapshot.teams, None)

    async def settle(self, timeout: float) -> bool:
        self.settles.append(timeout)
        return True
