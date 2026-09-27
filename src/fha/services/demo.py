"""The demo league's built-in salaries, stored on first use (issue #14).

``seed_demo_salaries`` does what the owner would do in Admin: read the
sheet, save each tab's suggested team, and import the free-agent rows. It
fills only what is empty, so a sheet or rows the owner already read or
uploaded (e.g. in a ``FHA_LOCAL_REPOSITORY`` file) are kept as they are,
and a second run writes nothing. ``DemoSalaries`` runs it once per process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from fha.services.aliases import load_aliases
from fha.services.clock import Clock
from fha.services.free_agents import ROWS, import_free_agent_salaries
from fha.services.league_sheet import (
    bind_tab,
    load_league_sheet,
    read_league_sheet,
    suggest_bindings,
)
from fha.sources.league_sheet.source import LeagueSheetSource
from fha.sources.puckpedia import SalaryRow
from fha.sources.yahoo.models import LeagueSnapshot
from fha.storage.chunks import load_chunked
from fha.storage.repository import Repository


async def seed_demo_salaries(
    repo: Repository,
    clock: Clock,
    snapshot: LeagueSnapshot,
    sheet: LeagueSheetSource,
    free_agent_rows: Sequence[SalaryRow],
) -> None:
    aliases = await load_aliases(repo)
    if await load_league_sheet(repo) is None:
        parsed = await read_league_sheet(repo, sheet, clock)
        for tab, team_key in suggest_bindings(parsed, snapshot.teams, aliases).items():
            if team_key is not None:
                await bind_tab(repo, tab, team_key)
    if await load_chunked(repo, ROWS) is None:
        await import_free_agent_salaries(repo, free_agent_rows, snapshot.pool, aliases)


class DemoSalaries:
    """``seed_demo_salaries`` once: ``ensure`` is awaited before every page."""

    def __init__(
        self,
        repo: Repository,
        clock: Clock,
        snapshot: LeagueSnapshot,
        sheet: LeagueSheetSource,
        free_agent_rows: Sequence[SalaryRow],
    ) -> None:
        self._args = (repo, clock, snapshot, sheet, free_agent_rows)
        self._done = False
        self._lock = asyncio.Lock()

    async def ensure(self) -> None:
        if self._done:
            return
        async with self._lock:
            if not self._done:
                await seed_demo_salaries(*self._args)
                self._done = True
