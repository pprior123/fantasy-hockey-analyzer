"""A synthetic league behind a mocked Yahoo API, for full-refresh tests."""

import asyncio
import re
from typing import Any

import httpx

from tests.unit.yahoo import builders as b

LK = b.LEAGUE_KEY
PREFIX = "/fantasy/v2/"


def skater(pid: int, gp: int, team: str = "TB") -> b.P:
    stats = {"0": str(gp), "1": str(pid % 7), "2": "3", "8": "2", "5": "4"}
    return b.P(str(pid), f"Player {pid}", team, "C", stats=stats)


# Two teams of 3, and 200 free agents (most tests read 60: 3 pages of 25).
MINE = b.T(
    1, [(skater(1, 10), "C"), (skater(2, 9), "IR"), (b.P("3", "Gee Oalie", pos="G"), "G")], True
)
THEIRS = b.T(2, [(skater(4, 8), "C"), (skater(5, 7), "BN"), (skater(6, 0), "IR+")])
FREE = [skater(100 + i, i % 5) for i in range(200)]
LAST = {"1": "82", "4": "70"}  # last season's GP for some players


class League:
    """Routes Yahoo paths to synthetic responses; tracks concurrency."""

    def __init__(self, *, strict: bool = True, **settings: Any) -> None:
        self.strict = strict  # unknown paths: fail the test, or answer 400 like Yahoo
        self.settings = settings
        self.paths: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.overrides: dict[str, Any] = {}

    def content(self, path: str) -> Any:
        if path in self.overrides:
            return self.overrides[path]
        if path == "games;game_codes=nhl":
            return b.games(("453", 2025), ("465", 2026))
        if path == f"league/{LK}/settings":
            return b.league_settings(**self.settings)
        if path == "game/465/stat_categories":
            return b.game_stat_categories()
        if path == f"league/{LK}/teams/roster":
            return b.teams_roster([MINE, THEIRS])
        if m := re.fullmatch(
            rf"league/{LK}/players;status=A;sort=AR;start=(\d+);count=(\d+)", path
        ):
            start, count = int(m[1]), int(m[2])
            return b.league_players(FREE[start : start + count])
        if m := re.fullmatch(rf"league/{LK}/scoreboard;week=(\d+)", path):
            week = int(m[1])
            return b.scoreboard(week, [(1, 2)] if week == 1 else [(2, 1)])
        if m := re.fullmatch(r"players;player_keys=([^/]+)/stats;type=season(;season=2025)?", path):
            keys, last = m[1].split(","), m[2]
            everyone = {
                p.key: p for p in [*FREE, *(p for t in (MINE, THEIRS) for p, _ in t.roster)]
            }
            players = [everyone[k] for k in keys]
            if last:
                players = [b.P(p.pid, p.name, stats={"0": LAST.get(p.pid, "-")}) for p in players]
            return b.player_stats(players, 2025 if last else 2026)
        raise LookupError(f"unexpected request: {path}")

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().removeprefix(PREFIX).removesuffix("?format=json")
        self.paths.append(path)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            for _ in range(10):
                await asyncio.sleep(0)
            try:
                content = self.content(path)
            except LookupError:
                if self.strict:
                    raise
                return httpx.Response(400, json={"error": {"description": "Invalid request"}})
            return httpx.Response(200, json=b.envelope(content))
        finally:
            self.in_flight -= 1
