"""The refresh service (SPEC §2): serve the cached snapshot, refresh when stale.

No background jobs: on request, if the cached snapshot is older than the
TTL (default 30 min), read Yahoo again, store it, serve it. ``force`` is the
Refresh button. The cache holds the raw snapshot; ratings are computed from
it on every view, so a settings change needs no refresh.

If Yahoo fails and a cached snapshot exists, the stale snapshot is served
with the error attached (the app stays usable during a Yahoo outage);
without a cache the error propagates.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fha.services import snapshot_codec
from fha.services.clock import Clock
from fha.sources.yahoo.models import LeagueSnapshot
from fha.sources.yahoo.source import YahooSource
from fha.storage.chunks import ChunkError, load_chunked, save_chunked
from fha.storage.repository import Repository

CACHE = "stats_cache"
DEFAULT_TTL_SECONDS = 30 * 60


@dataclass(frozen=True)
class Cached:
    snapshot: LeagueSnapshot
    fetched_at: float  # epoch seconds
    refresh_error: str | None = None  # set when a refresh failed and this is stale


async def load_cached(repo: Repository) -> Cached | None:
    """The stored snapshot, or None if there is none or it can't be read back
    (an old format, a damaged record): then it is simply refetched. A backend
    failure is not "no cache" and propagates."""
    try:
        stored = await load_chunked(repo, CACHE)
        if stored is None:
            return None
        meta, lists = stored
        fetched_at = meta.pop("fetched_at", None)
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, int | float):
            return None
        return Cached(snapshot_codec.decode(meta, lists), float(fetched_at))
    except (ChunkError, snapshot_codec.SnapshotCodecError):
        return None


async def save_cached(repo: Repository, cached: Cached) -> None:
    meta, lists = snapshot_codec.encode(cached.snapshot)
    await save_chunked(repo, CACHE, {**meta, "fetched_at": cached.fetched_at}, lists)


class RefreshService:
    def __init__(
        self,
        source: YahooSource,
        repo: Repository,
        clock: Clock,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError(f"ttl_seconds must be >= 0, got {ttl_seconds}")
        self._source = source
        self._repo = repo
        self._clock = clock
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()  # one refresh at a time in this process

    def is_fresh(self, cached: Cached) -> bool:
        age = self._clock.now() - cached.fetched_at
        return 0 <= age < self._ttl  # a timestamp from the future is not trusted

    async def current(self, *, force: bool = False) -> Cached:
        """The snapshot to serve: cached if fresh (and not forced), else refreshed."""
        cached = await load_cached(self._repo)
        if cached is not None and not force and self.is_fresh(cached):
            return cached
        async with self._lock:
            # Another request may have refreshed while this one waited.
            latest = await load_cached(self._repo)
            seen = None if cached is None else cached.fetched_at
            if latest is not None and latest.fetched_at != seen and self.is_fresh(latest):
                return latest
            try:
                snapshot = await self._source.fetch_snapshot()
            except Exception as error:
                if latest is None:
                    raise
                return Cached(
                    latest.snapshot, latest.fetched_at, f"{type(error).__name__}: {error}"
                )
            fresh = Cached(snapshot, self._clock.now())
            await save_cached(self._repo, fresh)
            return fresh
