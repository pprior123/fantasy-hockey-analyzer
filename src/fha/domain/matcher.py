"""Salary rows to Yahoo players: the SPEC §6 cascade. Pure.

A salary row (PuckPedia CSV, or a league-sheet row) is a ``Query``; the
players it may be are ``Candidate`` values. The cascade, first hit wins:

1. ``EXACT``: the same name (up to whitespace) and the same NHL team;
2. ``NORMALIZED``: the same normalized name, nicknames allowed, and team;
3. ``NAME``: the same normalized name, any team;
4. (roster scope only) ``SURNAME``: the row's name is a candidate's surname
   ("Andersen"): a sheet row matched within its tab's bound Yahoo roster;
5. fuzzy (rapidfuzz ``token_sort_ratio``): **never a match**, only
   candidates for the owner to confirm.

Steps 1-2 need the row's team; an unknown team skips them. Steps 3-4 skip a
player whose team *and* position group both contradict the row's (he is then
only a fuzzy candidate): a silent, permanent binding must not rest on a name
alone against that evidence. When a step finds
several players, the row's position group (F / D / G) breaks the tie, the
owner-approved extension of SPEC §6 (the two Sebastian Ahos). A tie it can't
break is ``AMBIGUOUS``. Aliases apply first: a row whose name has an alias
is looked up by the alias's stats name(s) instead.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache

from rapidfuzz import fuzz

from fha.domain import names
from fha.domain.names import PositionGroup

# Memoized views of the name functions: a pool import compares every row with
# every player (~1,700), so without these the same few thousand names are
# normalized millions of times (8 s for 800 rows; DECISIONS, M3 matcher).
CACHE_SIZE = 1 << 14


def _frozen_keys(raw: str) -> frozenset[str]:
    return frozenset(names.name_keys(raw))


name_keys = lru_cache(maxsize=CACHE_SIZE)(_frozen_keys)
normalize_name = lru_cache(maxsize=CACHE_SIZE)(names.normalize_name)
canonical_team = lru_cache(maxsize=CACHE_SIZE)(names.canonical_team)
position_group = lru_cache(maxsize=CACHE_SIZE)(names.position_group)
surname = lru_cache(maxsize=CACHE_SIZE)(names.surname)
_CACHED = (name_keys, normalize_name, canonical_team, position_group, surname)


def clear_caches() -> None:
    """Forget memoized names (tests clear them, so no test sees another's results)."""
    for cached in _CACHED:
        cached.cache_clear()


POOL_FUZZY = 90.0  # SPEC §6: a fuzzy candidate needs at least this score
ROSTER_FUZZY = 75.0  # among ~27 roster players a lower bar still suggests the right one
TOP = 3  # candidates offered for review (SPEC §7, Admin match review)


class Scope(StrEnum):
    POOL = "pool"  # PuckPedia rows: every player in the pool
    ROSTER = "roster"  # league-sheet rows: the tab's bound Yahoo roster only


class Step(StrEnum):
    EXACT = "exact name + team"
    NORMALIZED = "normalized name + team"
    NAME = "normalized name"
    SURNAME = "surname in roster"


class Status(StrEnum):
    MATCHED = "matched"  # bind without asking
    REVIEW = "review"  # fuzzy candidates only: the owner confirms
    AMBIGUOUS = "ambiguous"  # several players fit equally well
    UNMATCHED = "unmatched"  # nothing close; the best guesses are still offered


@dataclass(frozen=True)
class Candidate:
    """A player a row may be (from Yahoo): its ID, name, NHL team and position, as given."""

    player_id: str
    name: str
    team: str | None = None
    position: str | None = None


@dataclass(frozen=True)
class Query:
    """A salary row's name, NHL team and position, as the source wrote them."""

    name: str
    team: str | None = None
    position: str | None = None


@dataclass(frozen=True)
class Scored:
    candidate: Candidate
    score: float  # 0-100, rapidfuzz token_sort_ratio on normalized names


@dataclass(frozen=True)
class MatchResult:
    status: Status
    player: Candidate | None = None  # MATCHED only
    step: Step | None = None  # MATCHED only
    by_alias: bool = False  # the row was looked up by an alias
    by_position: bool = False  # position group broke a tie
    candidates: tuple[Scored, ...] = ()  # REVIEW / UNMATCHED: best first; AMBIGUOUS: the tie


@dataclass(frozen=True)
class Aliases:
    """Salary-source names -> the stats (Yahoo) names they stand for (SPEC §6).

    Keyed by normalized salary name; one salary name may stand for several
    stats names (the workbook spelled Voronkov two ways).
    """

    table: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_seed(cls, entries: Iterable[object]) -> Aliases:
        """From ``alias_seed.json``'s ``[{"stats_name": ..., "salary_name": ...}]``."""
        aliases = cls()
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError(f"alias entry must be an object, got {entry!r}")
            stats, salary = entry.get("stats_name"), entry.get("salary_name")
            if not isinstance(stats, str) or not isinstance(salary, str):
                raise ValueError(f"alias entry needs stats_name and salary_name: {entry!r}")
            aliases = aliases.with_alias(stats, salary)
        return aliases

    def with_alias(self, stats_name: str, salary_name: str) -> Aliases:
        key = normalize_name(salary_name)
        if not key or not normalize_name(stats_name):
            raise ValueError(f"alias names must not be blank: {stats_name!r}, {salary_name!r}")
        targets = tuple(sorted({*self.table.get(key, ()), stats_name}))
        return Aliases({**self.table, key: targets})

    def targets(self, salary_name: str) -> tuple[str, ...]:
        return self.table.get(normalize_name(salary_name), ())


NO_ALIASES = Aliases()


def _exact(name: str) -> str:
    return " ".join(name.split())


def match(
    query: Query,
    candidates: Sequence[Candidate],
    *,
    aliases: Aliases = NO_ALIASES,
    scope: Scope = Scope.POOL,
) -> MatchResult:
    """Run the cascade for one row over ``candidates``."""
    targets = aliases.targets(query.name)
    names = targets or (query.name,)
    by_alias = bool(targets)
    team = canonical_team(query.team)
    group = position_group(query.position)
    exact = {_exact(n) for n in names}
    keys = set().union(*(name_keys(n) for n in names))
    whole = {normalize_name(n) for n in names}

    steps: list[tuple[Step, Callable[[Candidate], bool]]] = []
    if team is not None:
        steps.append(
            (Step.EXACT, lambda p: _exact(p.name) in exact and canonical_team(p.team) == team)
        )
        steps.append(
            (
                Step.NORMALIZED,
                lambda p: bool(keys & name_keys(p.name)) and canonical_team(p.team) == team,
            )
        )

    def plausible(p: Candidate) -> bool:
        """Not contradicted by both team and position group (steps 3-4 ignore the team,
        so a same-name player on another team at another position is only a candidate)."""
        other_team = team is not None and canonical_team(p.team) not in (None, team)
        other_group = group is not None and position_group(p.position) not in (None, group)
        return not (other_team and other_group)

    steps.append((Step.NAME, lambda p: bool(keys & name_keys(p.name)) and plausible(p)))
    if scope is Scope.ROSTER:
        steps.append((Step.SURNAME, lambda p: surname(p.name) in whole and plausible(p)))

    for step, fits in steps:
        hits = sorted((p for p in candidates if fits(p)), key=_order)
        if len(hits) == 1:
            return MatchResult(Status.MATCHED, hits[0], step, by_alias)
        if hits:
            same = [p for p in hits if group is not None and position_group(p.position) is group]
            if len(same) == 1:
                return MatchResult(Status.MATCHED, same[0], step, by_alias, by_position=True)
            tie = tuple(Scored(p, _score(names, p, scope)) for p in hits)
            return MatchResult(Status.AMBIGUOUS, by_alias=by_alias, candidates=tie)

    scored = sorted(
        (Scored(p, _score(names, p, scope)) for p in candidates),
        key=lambda s: (-s.score, _other_group(s.candidate, group), *_order(s.candidate)),
    )[:TOP]
    bar = ROSTER_FUZZY if scope is Scope.ROSTER else POOL_FUZZY
    status = Status.REVIEW if scored and scored[0].score >= bar else Status.UNMATCHED
    return MatchResult(status, by_alias=by_alias, candidates=tuple(scored))


def _order(p: Candidate) -> tuple[str, str]:
    """A stable order, so the result doesn't depend on the order candidates came in."""
    return (p.name, p.player_id)


def _other_group(p: Candidate, group: PositionGroup | None) -> bool:
    return group is None or position_group(p.position) is not group


def _score(names: Iterable[str], p: Candidate, scope: Scope) -> float:
    """The best token_sort_ratio between any key of the row and of the candidate.

    In roster scope, a one-name row ("Oetterger") is also scored against the
    candidate's surname.
    """
    theirs = name_keys(p.name)
    if scope is Scope.ROSTER:
        theirs = theirs | {surname(p.name)}
    return max(
        (
            fuzz.token_sort_ratio(mine, other)
            for n in names
            for mine in name_keys(n)
            for other in theirs
        ),
        default=0.0,
    )
