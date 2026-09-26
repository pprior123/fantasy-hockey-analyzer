"""Record real Yahoo responses as sanitized fixtures (SPEC §9, M2).

The owner runs this once, after ``scripts.yahoo_auth``:

    uv run --env-file .env python -m scripts.record_yahoo

It performs one real full refresh (``HttpYahooSource.fetch_snapshot``) and
records every API response it makes, then a few probe requests that settle
questions the docs don't answer (see ``PROBES``). Only responses from the
Fantasy API host are recorded, never the token endpoint. Each response is
sanitized (``scripts.sanitize_yahoo``) and re-checked; if any check fails
nothing is written.

- the refresh → ``tests/fixtures/yahoo/`` (committed; replayed offline by the
  tests) with ``manifest.json`` mapping each request path to its file;
- the probes → ``private/yahoo_probes/`` (gitignored; read to make decisions);
- if the refresh fails, what it recorded → ``private/yahoo_probes/failed/``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from fha.sources.yahoo.client import DEFAULT_TIMEOUT, YahooClient, YahooError
from fha.sources.yahoo.models import LeagueSnapshot
from fha.sources.yahoo.oauth import Credentials, TokenStore, YahooAuthError
from fha.sources.yahoo.parse import YahooParseError
from fha.sources.yahoo.source import (
    DEFAULT_AVAILABLE,
    DEFAULT_AVAILABLE_SORT,
    DEFAULT_LEAGUE_ID,
    HttpYahooSource,
)
from fha.sources.yahoo.stat_map import StatMapError, build_stat_map, to_player_season
from scripts.sanitize_yahoo import private_names, problems, sanitize
from scripts.yahoo_common import REPO, JsonFileTokenStore, SetupError, credentials_from_env

API_HOST = "fantasysports.yahooapis.com"
API_PREFIX = "/fantasy/v2/"
FIXTURE_DIR = REPO / "tests" / "fixtures" / "yahoo"
PROBE_DIR = REPO / "private" / "yahoo_probes"
MANIFEST = "manifest.json"
PROBE_PLAYERS = 5


class SanitizeError(Exception):
    """A sanitized response still holds something that must not be written."""


@dataclass(frozen=True)
class Record:
    path: str  # the API path as the client wrote it, without ?format=json
    status: int
    body: Any  # parsed JSON, or None if the body wasn't JSON


def api_path(url: httpx.URL) -> str:
    path = url.raw_path.decode()
    path = path.removesuffix("?format=json")
    return path.removeprefix(API_PREFIX)


class RecordingTransport(httpx.AsyncBaseTransport):
    """Passes requests through; keeps a copy of every Fantasy API response."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.records: list[Record] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if request.url.host == API_HOST:
            raw = await response.aread()
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            self.records.append(Record(api_path(request.url), response.status_code, body))
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def file_name(index: int, path: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-")[:60].rstrip("-")
    return f"{index:03d}-{slug}.json"


def latest_per_path(records: Sequence[Record]) -> list[Record]:
    """One record per path, the last: a request retried after a 401 was recorded
    twice, and a replay must serve the answer the refresh actually used."""
    last = {r.path: i for i, r in enumerate(records)}
    return [r for i, r in enumerate(records) if last[r.path] == i]


def write_records(records: Sequence[Record], directory: Path, params: dict[str, Any]) -> None:
    """Sanitize, check, then (only if every record is clean) replace ``directory``'s files."""
    names = frozenset(n for r in records for n in private_names(r.body))
    cleaned = [sanitize(r.body) for r in records]
    issues = [
        f"{r.path}: {p}" for r, c in zip(records, cleaned, strict=True) for p in problems(c, names)
    ]
    if issues:
        shown = "\n  ".join(issues[:10])
        raise SanitizeError(f"{len(issues)} problems after sanitizing, nothing written:\n  {shown}")
    directory.mkdir(parents=True, exist_ok=True)
    for old in [*directory.glob("[0-9][0-9][0-9]-*.json"), directory / MANIFEST]:
        old.unlink(missing_ok=True)
    calls = []
    for i, (record, body) in enumerate(zip(records, cleaned, strict=True)):
        name = file_name(i, record.path)
        # Compact: the full pool makes these several MB; a test checks them, not a reader.
        compact = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        (directory / name).write_text(compact + "\n")
        calls.append({"path": record.path, "status": record.status, "file": name})
    manifest = {"params": params, "calls": calls}
    (directory / MANIFEST).write_text(json.dumps(manifest, indent=1) + "\n")


def probe_paths(snapshot: LeagueSnapshot) -> list[str]:
    """Requests whose answers decide open questions (recorded to private/, not fixtures).

    - ``game/nhl``: does the game-code alias resolve to the same game?
    - league-scoped stats: do they include GP (so stats could ride along
      with the player pages)?
    - last season by last season's game key vs. by ``;season=``: same totals?
    - the top available by preseason rank (OR) and by last season's rank.
    """
    lk, season = snapshot.settings.league_key, snapshot.game.season
    goalies = [p for p in snapshot.pool if p.is_goalie][:1]
    sample = [*[p for p in snapshot.pool if not p.is_goalie][: PROBE_PLAYERS - 1], *goalies]
    keys = ",".join(p.player_key for p in sample)
    return [
        "game/nhl",
        f"games;game_codes=nhl;seasons={season - 1}",
        f"league/{lk}/players;player_keys={keys}/stats;type=season",
        f"league/{lk}/players;player_keys={keys}/stats;type=season;season={season - 1}",
        f"league/{lk}/players;status=A;sort=OR;start=0;count=25",
        f"league/{lk}/players;status=A;sort=AR;sort_type=season;sort_season={season - 1};"
        "start=0;count=25",
        f"league/{lk}/players;status=T;start=0;count=25",
    ]


async def run_probes(client: YahooClient, snapshot: LeagueSnapshot) -> list[str]:
    """Probe requests one at a time; a failed probe is noted, not fatal."""
    notes = []
    for path in probe_paths(snapshot):
        try:
            content = await client.get(path)
        except YahooError as e:
            notes.append(f"probe failed: {e}")
            continue
        if path.startswith("games;"):  # last season's game key, then its player keys
            games = content.get("games", {})
            game = games.get("0", {}).get("game", [{}]) if isinstance(games, dict) else [{}]
            prev = game[0].get("game_key") if isinstance(game, list) and game else None
            if prev:
                ids = [p.player_id for p in snapshot.pool[:PROBE_PLAYERS]]
                keys = ",".join(f"{prev}.p.{i}" for i in ids)
                try:
                    await client.get(f"players;player_keys={keys}/stats;type=season")
                except YahooError as e:
                    notes.append(f"probe failed: {e}")
    return notes


@dataclass
class Summary:
    calls: int
    seconds: float
    snapshot: LeagueSnapshot
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        snap = self.snapshot
        mine = snap.my_team
        out = [
            f"Recorded {self.calls} API calls in {self.seconds:.1f} s "
            f"(game {snap.game.game_key}, season {snap.game.season}).",
            f"Teams: {len(snap.teams)}; rostered players: "
            f"{sum(len(t.roster) for t in snap.teams)}; available: {len(snap.available)}; "
            f"pool: {len(snap.pool)}.",
            f"My team: {mine.team_key if mine else 'NOT FOUND (is_owned_by_current_login)'}.",
            f"Week {snap.settings.current_week} of {snap.settings.start_week}-"
            f"{snap.settings.end_week}; matchups this week: {len(snap.scoreboard.matchups)}; "
            f"next week: {len(snap.next_scoreboard.matchups) if snap.next_scoreboard else 'none'}.",
        ]
        try:
            stat_map = build_stat_map(snap.settings.stat_categories, snap.game_stat_categories)
            out.append(
                f"PPP: {'direct' if stat_map.ppp_direct else 'PPG + PPA'}; stat IDs "
                f"{ {c.value: ids for c, ids in stat_map.categories.items()} }; "
                f"GP {stat_map.skater_gp} (goalies {stat_map.goalie_gp})."
            )
            for label, stats in (("this", snap.stats), ("last", snap.last_season_stats or {})):
                played = sum(
                    to_player_season(p, stats.get(p.player_key), stat_map).gp > 0 for p in snap.pool
                )
                out.append(f"Players with GP > 0 {label} season: {played} of {len(snap.pool)}.")
        except StatMapError as e:
            names = sorted({c.display_name for c in snap.game_stat_categories})
            out.append(f"STAT MAP FAILED: {e}. Yahoo's stat names: {', '.join(names)}")
        out.extend(self.notes)
        return out


async def record(
    creds: Credentials,
    store: TokenStore,
    inner: httpx.AsyncBaseTransport,
    *,
    fixture_dir: Path = FIXTURE_DIR,
    probe_dir: Path = PROBE_DIR,
    clock: Callable[[], float] = time.time,
    available: int | None = DEFAULT_AVAILABLE,
) -> Summary:
    params = {
        "league_id": DEFAULT_LEAGUE_ID,
        "available": available,
        "available_sort": DEFAULT_AVAILABLE_SORT,
    }
    transport = RecordingTransport(inner)
    async with httpx.AsyncClient(transport=transport, timeout=DEFAULT_TIMEOUT) as http:
        client = YahooClient(http, creds, store, clock=clock)
        source = HttpYahooSource(client, available=available)
        started = time.perf_counter()
        try:
            snapshot = await source.fetch_snapshot()
        except Exception as error:
            try:  # every record, retries included: this is for diagnosis
                write_records(transport.records, probe_dir / "failed", params)
            except SanitizeError as e:  # keep the refresh's error, the one that matters
                error.add_note(f"What it recorded was not kept: {e}")
            raise
        seconds = time.perf_counter() - started
        main_calls = list(transport.records)
        write_records(latest_per_path(main_calls), fixture_dir, params)
        notes = await run_probes(client, snapshot)
        write_records(transport.records[len(main_calls) :], probe_dir, params)
    return Summary(len(main_calls), seconds, snapshot, notes)


async def _main() -> int:
    try:
        creds = credentials_from_env(os.environ)
        store = JsonFileTokenStore()
        if await store.load() is None:
            raise SetupError("no token yet: run python -m scripts.yahoo_auth first")
        summary = await record(creds, store, httpx.AsyncHTTPTransport())
    except (
        SetupError,
        SanitizeError,
        YahooError,
        YahooAuthError,
        YahooParseError,
        httpx.HTTPError,  # e.g. no network: a line, not a traceback
    ) as e:
        print(f"Failed: {e}", *getattr(e, "__notes__", ()), sep="\n", file=sys.stderr)
        return 1
    print("\n".join(summary.lines()))
    print(f"\nFixtures: {FIXTURE_DIR.relative_to(REPO)}; probes: {PROBE_DIR.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
