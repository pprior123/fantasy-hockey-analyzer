"""The refresh service (SPEC §2): serve the cached snapshot, refresh when stale.

No background jobs: on request, if the cached snapshot is older than the
TTL (default 30 min), read Yahoo again, store it, serve it. ``force`` is the
Refresh button. The cache holds the raw snapshot; ratings are computed from
it on every view, so a settings change needs no refresh.

If Yahoo fails and a cached snapshot exists, the stale snapshot is served
with the error attached (the app stays usable during a Yahoo outage);
without a cache a ``RefreshError`` is raised, whatever the source raised
(a Yahoo HTTP error, a transport timeout, a parse error).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace

from fha.services import snapshot_codec
from fha.services.clock import Clock
from fha.sources.yahoo.models import LeagueSnapshot
from fha.sources.yahoo.source import YahooSource
from fha.storage.chunks import META, ChunkError, load_chunked, save_chunked
from fha.storage.repository import Repository, RepositoryError

CACHE = "stats_cache"
DEFAULT_TTL_SECONDS = 30 * 60
RETRY_AFTER_SECONDS = 60.0  # after a failed refresh, serve stale this long before retrying

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Cached:
    snapshot: LeagueSnapshot
    fetched_at: float  # epoch seconds
    # A note for the UI, or None: a refresh failed and this snapshot is stale, or
    # ("not saved: ...") it is fresh from Yahoo but the store refused it.
    refresh_error: str | None = None


async def load_cached(repo: Repository) -> Cached | None:
    """The stored snapshot, or None if there is none or it can't be read back
    (an old format, a damaged record): then it is simply refetched, and a
    content-free warning is logged, since each refetch costs ~130 Yahoo calls.
    A backend failure is not "no cache" and propagates."""
    try:
        stored = await load_chunked(repo, CACHE)
        if stored is None:
            return None
        meta, lists = stored
        fetched_at = _timestamp(meta.pop("fetched_at", None))
        if fetched_at is None:
            log.warning("stats cache has no usable timestamp; refetching")
            return None
        return Cached(snapshot_codec.decode(meta, lists), fetched_at)
    except (ChunkError, snapshot_codec.SnapshotCodecError) as error:
        log.warning("stats cache unreadable (%s); refetching", type(error).__name__)
        return None


async def save_cached(repo: Repository, cached: Cached) -> None:
    meta, lists = snapshot_codec.encode(cached.snapshot)
    await save_chunked(repo, CACHE, {**meta, "fetched_at": cached.fetched_at}, lists)


def _describe(error: Exception) -> str:
    """A failure as the pages show it. A store error is named by its type only: its
    text can quote Firestore's reason, which can name the project (M4R4A-2)."""
    if isinstance(error, RepositoryError):
        return type(error).__name__
    return f"{type(error).__name__}: {error}"


class RefreshError(Exception):
    """A refresh failed (now, or recently) and there is no cached snapshot to serve."""


class RefreshService:
    """One per process. It keeps the last decoded snapshot in memory, so a request
    reads only the cache's small meta document to learn whether it's current."""

    def __init__(
        self,
        source: YahooSource,
        repo: Repository,
        clock: Clock,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        retry_after_seconds: float = RETRY_AFTER_SECONDS,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError(f"ttl_seconds must be >= 0, got {ttl_seconds}")
        if retry_after_seconds < 0:
            raise ValueError(f"retry_after_seconds must be >= 0, got {retry_after_seconds}")
        self._source = source
        self._repo = repo
        self._clock = clock
        self._ttl = ttl_seconds
        self._retry_after = retry_after_seconds
        self._lock = asyncio.Lock()  # one refresh at a time in this process
        self._load_lock = asyncio.Lock()  # one full cache download at a time
        # The newest snapshot this process has: decoded from the store, or fetched
        # here (then possibly not saved: its refresh_error says so).
        self._memory: Cached | None = None
        self._unreadable_at: float | None = None  # a stored fetch that failed to decode
        self._failure: tuple[float, str] | None = None  # (when, what) of the last failure

    def is_fresh(self, cached: Cached) -> bool:
        age = self._clock.now() - cached.fetched_at
        return 0 <= age < self._ttl  # a timestamp from the future is not trusted

    async def current(self, *, force: bool = False) -> Cached:
        """The snapshot to serve: cached if fresh (and not forced), else refreshed.

        After a failed refresh, requests within ``retry_after_seconds`` get the
        stale snapshot and that error without calling Yahoo again, so an outage
        doesn't cost every request (or every waiter) a full Yahoo attempt.
        """
        cached = await self._stored()
        if cached is not None and not force and self.is_fresh(cached):
            return self._with_recent_failure(cached)
        async with self._lock:
            # Another request may have refreshed while this one waited.
            latest = await self._stored()
            seen = None if cached is None else cached.fetched_at
            if latest is not None and latest.fetched_at != seen and self.is_fresh(latest):
                return latest
            failure = self._recent_failure()
            if failure is not None:
                _, note = failure
                if latest is None:
                    raise RefreshError(f"refresh failed: {note}")
                return replace(latest, refresh_error=note)
            try:
                snapshot = await self._source.fetch_snapshot()
            except Exception as error:
                self._failure = (self._clock.now(), _describe(error))
                if latest is None:
                    raise RefreshError(f"refresh failed: {self._failure[1]}") from error
                return replace(latest, refresh_error=self._failure[1])
            self._failure = None
            fresh = Cached(snapshot, self._clock.now())
            self._memory = fresh
            try:
                await save_cached(self._repo, fresh)
            except RepositoryError as error:
                # Keep serving what Yahoo gave (from memory) until the TTL, rather
                # than calling Yahoo again on every request; the next refresh
                # tries the save again.
                self._memory = replace(fresh, refresh_error=f"not saved: {_describe(error)}")
            return self._memory

    def _recent_failure(self) -> tuple[float, str] | None:
        """(when, note) of the last failure while within ``retry_after_seconds`` of it."""
        if self._failure is None:
            return None
        failed_at, reason = self._failure
        ago = self._clock.now() - failed_at
        if not 0 <= ago < self._retry_after:
            return None
        return failed_at, f"{reason} ({ago:.0f} s ago; retrying after {self._retry_after:.0f} s)"

    def _with_recent_failure(self, cached: Cached) -> Cached:
        """A fresh snapshot, with the note of a refresh that failed after it was fetched
        (the Refresh button's forced attempt): so the page after a failed Refresh says
        so, instead of looking like a success. It replaces a "not saved" note: the
        snapshot is no longer the fresh fetch that note describes."""
        failure = self._recent_failure()
        if failure is None:
            return cached
        failed_at, note = failure
        if failed_at < cached.fetched_at:
            return cached  # a newer fetch (another instance's) succeeded since
        return replace(cached, refresh_error=note)

    async def _stored(self) -> Cached | None:
        """The newest snapshot: from memory unless the store holds a newer fetch, so a
        warm request reads one small document instead of the whole cache."""
        meta = await self._repo.get(CACHE, META)
        fetched_at = None if meta is None else _timestamp(meta.get("fetched_at"))
        memory = self._memory
        if memory is not None and self._keep(memory, fetched_at):
            if memory.refresh_error and memory.fetched_at == fetched_at:
                # The store holds this very fetch (e.g. a commit whose reply timed out).
                self._memory = memory = replace(memory, refresh_error=None)
            return memory
        if fetched_at is None:
            return None
        async with self._load_lock:  # concurrent cold requests share one download
            memory = self._memory
            if memory is not None and self._keep(memory, fetched_at):
                return memory
            loaded = await load_cached(self._repo)
            if loaded is None:
                self._unreadable_at = fetched_at  # don't download it again and again
            latest = self._memory  # a local refresh may have finished during the download
            if loaded is not None and (latest is None or loaded.fetched_at > latest.fetched_at):
                self._memory = loaded
            return self._memory

    def _keep(self, memory: Cached, stored_at: float | None) -> bool:
        """Serve ``memory`` rather than downloading the stored cache: nothing stored, the
        same fetch or an older one, one that failed to decode, or one stamped in this
        clock's future (another instance's skewed clock; it would never be fresh)."""
        return (
            stored_at is None
            or memory.fetched_at >= stored_at
            or stored_at == self._unreadable_at
            or stored_at > self._clock.now()
        )


def _timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
