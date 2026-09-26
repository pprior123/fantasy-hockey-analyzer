"""Free-agent salaries from the PuckPedia CSV (SPEC §4, §6; DECISIONS, M3).

Each imported row is bound to a Yahoo player **once** and the binding is
persisted: a row is never re-matched by name on later requests. Rows the
cascade can't bind wait for the owner (``pending_reviews`` / ``confirm``).

Re-importing is idempotent: rows are keyed by normalized name and position
group, stored in a canonical order, and nothing is written when neither the
rows nor the bindings change.

Storage: the rows as one chunked record, and the bindings and the
single-player AAV overrides as one document each (a few hundred entries, far
under the document limit), so an import is a handful of writes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fha.domain.matcher import Aliases, Candidate, MatchResult, Query, Status, match
from fha.domain.names import canonical_team, normalize_name, position_group
from fha.sources.puckpedia import SalaryRow
from fha.sources.yahoo.models import Player
from fha.storage.chunks import load_chunked, save_chunked
from fha.storage.repository import Repository

ROWS = "fa_salaries"  # chunked record: the imported rows
STATE = "fa_state"  # documents: BINDINGS, OVERRIDES
BINDINGS = "bindings"
OVERRIDES = "aav_overrides"


class FreeAgentError(ValueError):
    """An owner action that can't be applied (unknown row, bad cap hit)."""


def row_key(name: str, position: str | None) -> str:
    """A row's identity across imports: normalized name + position group."""
    group = position_group(position)
    return f"{normalize_name(name)}|{group.value if group else '?'}"


def auto_how(result: MatchResult) -> str:
    """How an automatic binding was made, e.g. ``auto:surname`` (stable across label edits)."""
    step = result.step.name.lower() if result.step is not None else "unknown"
    return f"auto:{step}"


def candidates(players: Iterable[Player]) -> list[Candidate]:
    return [Candidate(p.player_id, p.name, p.nhl_team or None, p.display_position) for p in players]


@dataclass(frozen=True)
class Review:
    """A row the cascade couldn't bind, with its best candidates."""

    key: str
    row: SalaryRow
    result: MatchResult


@dataclass(frozen=True)
class ImportReport:
    rows: int  # distinct rows stored
    changed: bool  # False: the same file again, nothing written
    bound: int  # rows bound automatically by this import
    already_bound: int  # rows bound before (kept, not re-matched)
    reviews: tuple[Review, ...]  # REVIEW / AMBIGUOUS / UNMATCHED rows
    conflicts: tuple[str, ...]  # keys given twice with different cap hits: not imported
    unknown_teams: tuple[str, ...] = ()  # team strings no NHL code matches (SPEC §6)


async def import_free_agent_salaries(
    repo: Repository, rows: Sequence[SalaryRow], pool: Sequence[Player], aliases: Aliases
) -> ImportReport:
    by_key: dict[str, SalaryRow] = {}
    conflicting: set[str] = set()
    for row in rows:
        key = row_key(row.name, row.position)
        seen = by_key.get(key)
        if seen is not None and (seen.aav, seen.cap_hit_with_bonuses) != (
            row.aav,
            row.cap_hit_with_bonuses,
        ):
            conflicting.add(key)
        by_key.setdefault(key, row)
    for key in conflicting:
        del by_key[key]
    stored = [_encode_row(key, row) for key, row in sorted(by_key.items())]

    previous = await load_chunked(repo, ROWS)
    rows_changed = previous is None or previous[1].get("rows") != stored
    if rows_changed:
        await save_chunked(repo, ROWS, {}, {"rows": stored})

    bindings = await _load(repo, BINDINGS)
    pool_candidates = candidates(pool)
    bound, already, reviews = 0, 0, []
    for key, row in sorted(by_key.items()):
        if key in bindings:
            already += 1
            continue
        result = match(Query(row.name, row.team, row.position), pool_candidates, aliases=aliases)
        if result.status is Status.MATCHED and result.player is not None:
            bindings[key] = {"player_id": result.player.player_id, "how": auto_how(result)}
            bound += 1
        else:
            reviews.append(Review(key, row, result))
    if bound:
        await repo.put(STATE, BINDINGS, {"entries": bindings})
    return ImportReport(
        rows=len(stored),
        changed=rows_changed or bound > 0,
        bound=bound,
        already_bound=already,
        reviews=tuple(reviews),
        conflicts=tuple(sorted(conflicting)),
        unknown_teams=tuple(
            sorted({r.team for r in rows if r.team and canonical_team(r.team) is None})
        ),
    )


async def pending_reviews(
    repo: Repository, pool: Sequence[Player], aliases: Aliases
) -> list[Review]:
    """Imported rows with no binding yet, each with its best candidates (Admin)."""
    bindings = await _load(repo, BINDINGS)
    pool_candidates = candidates(pool)
    out = []
    for key, row in (await _rows(repo)).items():
        if key not in bindings:
            query = Query(row.name, row.team, row.position)
            out.append(Review(key, row, match(query, pool_candidates, aliases=aliases)))
    return out


async def confirm(repo: Repository, key: str, player_id: str) -> None:
    """The owner binds a row to a player (from the review screen)."""
    if key not in await _rows(repo):
        raise FreeAgentError(f"no imported row {key!r}")
    bindings = await _load(repo, BINDINGS)
    bindings[key] = {"player_id": player_id, "how": "confirmed"}
    await repo.put(STATE, BINDINGS, {"entries": bindings})


async def unbind(repo: Repository, key: str) -> None:
    bindings = await _load(repo, BINDINGS)
    if bindings.pop(key, None) is not None:
        await repo.put(STATE, BINDINGS, {"entries": bindings})


async def set_aav_override(repo: Repository, player_id: str, aav: int | None) -> None:
    """The single-player AAV edit (SPEC §6), for a contract that changed mid-season.

    ``None`` removes the override, so the imported cap hit applies again.
    """
    if aav is not None and (isinstance(aav, bool) or not isinstance(aav, int) or aav < 0):
        raise FreeAgentError(f"a cap hit must be a whole number of dollars >= 0, got {aav!r}")
    overrides = await _load(repo, OVERRIDES)
    if aav is None:
        overrides.pop(player_id, None)
    else:
        overrides[player_id] = aav
    await repo.put(STATE, OVERRIDES, {"entries": overrides})


@dataclass(frozen=True)
class FreeAgentSalaries:
    aav: Mapping[str, int]  # player_id -> cap hit
    problems: tuple[str, ...]  # e.g. two rows bound to one player (neither is used)


async def load_free_agent_salaries(repo: Repository) -> FreeAgentSalaries:
    """Cap hits by player: an override, else the bound row's. Unbound rows give none."""
    rows = await _rows(repo)
    bindings = await _load(repo, BINDINGS)
    by_player: dict[str, list[str]] = {}
    for key, binding in sorted(bindings.items()):
        if key in rows:
            by_player.setdefault(binding["player_id"], []).append(key)
    aav: dict[str, int] = {}
    problems = []
    for player_id, keys in sorted(by_player.items()):
        if len(keys) > 1:
            problems.append(f"player {player_id} is bound to {len(keys)} rows: {', '.join(keys)}")
        else:
            aav[player_id] = rows[keys[0]].aav
    for player_id, value in (await _load(repo, OVERRIDES)).items():
        aav[player_id] = value
    return FreeAgentSalaries(aav, tuple(problems))


async def _rows(repo: Repository) -> dict[str, SalaryRow]:
    stored = await load_chunked(repo, ROWS)
    if stored is None:
        return {}
    return {r["key"]: _decode_row(r) for r in stored[1]["rows"]}


async def _load(repo: Repository, doc_id: str) -> dict[str, Any]:
    doc = await repo.get(STATE, doc_id)
    return dict(doc["entries"]) if doc is not None else {}


def _encode_row(key: str, row: SalaryRow) -> dict[str, Any]:
    return {
        "key": key,
        "name": row.name,
        "position": row.position,
        "team": row.team,
        "aav": row.aav,
        "line": row.line,
        "cap_hit_with_bonuses": row.cap_hit_with_bonuses,
    }


def _decode_row(r: Mapping[str, Any]) -> SalaryRow:
    return SalaryRow(
        r["name"], r["position"], r["team"], r["aav"], r["line"], r.get("cap_hit_with_bonuses")
    )
