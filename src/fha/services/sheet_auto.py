"""Run a guarded tab update for an unchecked, saved Yahoo generation."""

from __future__ import annotations

import logging
import secrets
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from fha.domain.sheet_update import Change, TabRow
from fha.services.clock import Clock
from fha.services.free_agents import row_key
from fha.services.league_sheet import load_league_sheet, read_league_sheet
from fha.services.refresh import RefreshService
from fha.services.settings import COLLECTION as CONFIG
from fha.services.sheet_update import (
    WRITE_ATTEMPTS,
    AttemptSource,
    SheetUpdate,
    WriteJournal,
    WriteResult,
    prepare_update,
    write_update,
)
from fha.sources.league_sheet.models import LeagueSheetError
from fha.sources.league_sheet.source import LeagueSheetSource
from fha.sources.league_sheet.writer import LeagueSheetWriter
from fha.storage.repository import Document, Repository, RepositoryError

AUTO_LOG = "sheet_auto_log"
AUTO_STATE = "sheet_auto_state"
AUTO_SWITCH = "sheet_auto"
LOG_LIMIT = 50
SHEET_MAX_AGE = 24 * 3600
SEND_BUDGET = 35.0
ATTEMPT_PREFIX = "attempt-"
log = logging.getLogger(__name__)

Outcome = Literal["written", "skipped", "failed", "unknown"]

REASONS = {
    "earlier_unknown": "an earlier write's outcome isn't known yet",
    "expired_attempt": "an earlier write's outcome is unknown. Check the tab",
    "no_sheet": "no league sheet has been read yet",
    "problem_no_team": "your team isn't in the Yahoo data",
    "problem_unbound": "no tab is bound to your team yet",
    "problem_unreadable_tab": "your tab couldn't be read",
    "problem_old_sheet": "the stored sheet predates the update feature. Read the sheet again",
    "tab_changed": "the tab or your roster changed during the update",
    "protections_unknown": "the sheet's protections couldn't be checked",
    "others_unprotected": "other tabs are still editable by the app",
    "own_tab_locked": "your tab is protected against the app",
    "guard": "the proposed changes did not pass the write guard",
    "superseded": "the Yahoo snapshot is no longer eligible",
    "switched_off": "automatic updates were switched off during the update",
    "out_of_time": "the time allowed to send the update expired",
    "sheet_unreadable": "the sheet couldn't be read",
    "storage_failed": "the app's storage failed",
    "google_refused": "Google refused the write",
    "not_sent": "the write couldn't be sent",
    "lost_answer": "Google's answer was lost and the result couldn't be verified",
    "unverified": "the write went through but couldn't be verified",
}


@dataclass(frozen=True)
class AutoUpdate:
    at: float
    outcome: Outcome
    code: str | None = None
    reason: str = ""
    source: AttemptSource = "auto"
    changes: tuple[str, ...] = ()
    cells: int = 0
    payroll_after: int | None = None
    attempt_id: str | None = None


async def enabled(repo: Repository) -> bool:
    doc = await repo.get(CONFIG, AUTO_SWITCH)
    return doc is None or doc.get("enabled") is True


async def set_enabled(repo: Repository, value: bool) -> None:
    await repo.put(CONFIG, AUTO_SWITCH, {"enabled": value})


async def checked_generation(repo: Repository) -> float | None:
    doc = await repo.get(AUTO_STATE, "last")
    return None if doc is None else doc.get("fetched_at")


async def mark_checked(repo: Repository, fetched_at: float) -> None:
    await repo.put(AUTO_STATE, "last", {"fetched_at": fetched_at})


def has_unresolved_attempt(attempts: Mapping[str, Document], now: float) -> bool:
    return any(attempt["deadline"] > now for attempt in attempts.values())


async def _ordered_log(repo: Repository) -> list[tuple[str, Document]]:
    entries = await repo.all(AUTO_LOG)
    return sorted(entries.items(), key=lambda item: (item[1]["at"], item[0]), reverse=True)


async def log_entries(repo: Repository) -> list[AutoUpdate]:
    return [
        AutoUpdate(
            at=doc["at"],
            outcome=doc["outcome"],
            code=doc["code"],
            reason=doc["reason"],
            source=doc["source"],
            changes=tuple(doc.get("changes", ())),
            cells=doc["cells"],
            payroll_after=doc.get("payroll_after"),
            attempt_id=doc_id.removeprefix(ATTEMPT_PREFIX)
            if doc_id.startswith(ATTEMPT_PREFIX)
            else None,
        )
        for doc_id, doc in await _ordered_log(repo)
    ]


async def _record_log(repo: Repository, result: AutoUpdate) -> None:
    if result.outcome == "skipped":
        entries = await _ordered_log(repo)
        if entries:
            _, latest = entries[0]
            if (latest["outcome"], latest["code"], latest["reason"]) == (
                "skipped",
                result.code,
                result.reason,
            ):
                return
    doc_id = (
        f"{ATTEMPT_PREFIX}{result.attempt_id}"
        if result.attempt_id is not None
        else f"{int(result.at * 1000):013d}-{secrets.token_hex(4)}"
    )
    doc: Document = {
        "at": result.at,
        "outcome": result.outcome,
        "code": result.code,
        "source": result.source,
        "cells": result.cells,
        "reason": result.reason,
    }
    if result.outcome == "written":
        doc.update(changes=list(result.changes), payroll_after=result.payroll_after)
    await repo.put(AUTO_LOG, doc_id, doc)
    for older_id, _ in (await _ordered_log(repo))[LOG_LIMIT:]:
        await repo.delete(AUTO_LOG, older_id)


def _unidentified_drops(
    changes: Sequence[Change],
    planned_rows: Sequence[TabRow],
    bindings: Mapping[str, str],
    roster: set[str],
) -> tuple[int, ...]:
    drops = {change.from_row for change in changes if change.action == "drop"}
    rows = [(row.row, row_key(row.name, row.position)) for row in planned_rows if row.row in drops]
    keys = Counter(key for _, key in rows)
    players = Counter(bindings.get(key) for _, key in rows)
    return tuple(
        sorted(
            number
            for number, key in rows
            if (player := bindings.get(key)) is None
            or player in roster
            or keys[key] > 1
            or players[player] > 1
        )[:5]
    )


def _reason(code: str | None, refusal: str | None, unidentified: tuple[int, ...]) -> str:
    if code == "refusal" and refusal is not None:
        return refusal
    if code == "unidentified_drop" and unidentified:
        if len(unidentified) == 1:
            subject = f"row {unidentified[0]} isn't matched to a player who left your roster"
        else:
            numbers = ", ".join(str(number) for number in unidentified)
            subject = f"rows {numbers} aren't matched to distinct players who left your roster"
        return f"{subject}. Check on Update my tab"
    if code is None:
        return ""
    return REASONS.get(code, "the tab couldn't be prepared for writing")


async def auto_update(
    repo: Repository,
    clock: Clock,
    refresh: RefreshService,
    source: LeagueSheetSource,
    writer: LeagueSheetWriter,
    *,
    fetched_at: float,
    started: float,
) -> AutoUpdate | None:
    """Check one saved generation and report its automatic update, if any."""
    try:
        result = await _run_update(repo, clock, refresh, source, writer, fetched_at, started)
    except RepositoryError as error:
        log.warning("Automatic sheet update storage failed (%s)", type(error).__name__)
        result = AutoUpdate(clock.now(), "failed", "storage_failed", REASONS["storage_failed"])
    await _save_result(repo, result)
    return result


async def record_manual_attempt_outcome(
    repo: Repository, clock: Clock, result: WriteResult
) -> None:
    """Keep a definite manual result when its journal record had to stay behind."""
    if result.attempt_cleanup_failed and result.attempt_id is not None:
        await _save_result(repo, _write_outcome(clock, result, source="manual"))


async def _save_result(repo: Repository, result: AutoUpdate | None) -> None:
    if result is not None:
        try:
            await _record_log(repo, result)
        except RepositoryError as error:
            log.warning("Automatic sheet update log failed (%s)", type(error).__name__)


async def _run_update(
    repo: Repository,
    clock: Clock,
    refresh: RefreshService,
    source: LeagueSheetSource,
    writer: LeagueSheetWriter,
    fetched_at: float,
    started: float,
) -> AutoUpdate | None:
    def skipped(code: str, refusal: str | None = None) -> AutoUpdate:
        return AutoUpdate(clock.now(), "skipped", code, _reason(code, refusal, ()))

    if not await refresh.eligible(fetched_at) or not await enabled(repo):
        return None
    if await checked_generation(repo) == fetched_at:
        return None
    cached = await refresh.load_saved(fetched_at)
    if cached is None:
        return None
    await mark_checked(repo, fetched_at)

    attempts = await repo.all(WRITE_ATTEMPTS)
    if has_unresolved_attempt(attempts, clock.now()):
        return skipped("earlier_unknown")
    for attempt_id, attempt in attempts.items():
        if await repo.get(AUTO_LOG, ATTEMPT_PREFIX + attempt_id) is None:
            await _record_log(
                repo,
                AutoUpdate(
                    clock.now(),
                    "unknown",
                    "expired_attempt",
                    REASONS["expired_attempt"],
                    source=attempt["source"],
                    cells=attempt["cells"],
                    attempt_id=attempt_id,
                ),
            )

    if not attempts:
        stored = await load_league_sheet(repo)
        if stored is None:
            return skipped("no_sheet")
        sheet, read_at = stored
        if clock.now() - read_at <= SHEET_MAX_AGE:
            preview = await prepare_update(repo, cached.snapshot, sheet)
            if preview.problem_code is not None:
                return skipped(preview.problem_code)
            if preview.plan is None or not preview.plan.changes:
                return None
    try:
        sheet = await read_league_sheet(repo, source, clock)
    except LeagueSheetError:
        return AutoUpdate(clock.now(), "failed", "sheet_unreadable", REASONS["sheet_unreadable"])
    for attempt_id in attempts:
        await repo.delete(WRITE_ATTEMPTS, attempt_id)

    preview = await prepare_update(repo, cached.snapshot, sheet)
    if preview.problem_code is not None:
        return skipped(preview.problem_code)
    if preview.plan is None or not preview.plan.changes:
        return None
    if preview.plan.refusals:
        return skipped("refusal", preview.plan.refusals[0])
    if not preview.plan.edits:
        return None

    unidentified: tuple[int, ...] = ()
    mine = cached.snapshot.my_team
    if mine is None:
        return skipped("problem_no_team")
    roster = {entry.player.player_id for entry in mine.roster}

    async def eligibility_issue() -> str | None:
        if not await refresh.eligible(fetched_at):
            return "superseded"
        if not await enabled(repo):
            return "switched_off"
        return None

    async def before_post() -> str | None:
        try:
            return await eligibility_issue()
        except RepositoryError:
            return "storage_failed"

    async def before_write(update: SheetUpdate, bindings: Mapping[str, str]) -> str | None:
        nonlocal unidentified
        if update.plan is None:
            return "tab_changed"
        unidentified = _unidentified_drops(update.plan.changes, update.rows, bindings, roster)
        if unidentified:
            return "unidentified_drop"
        if (issue := await eligibility_issue()) is not None:
            return issue
        if has_unresolved_attempt(await repo.all(WRITE_ATTEMPTS), clock.now()):
            return "earlier_unknown"
        return None

    result = await write_update(
        repo,
        clock,
        source,
        writer,
        cached.snapshot,
        preview.digest,
        before_write=before_write,
        before_post=before_post,
        journal=WriteJournal(repo, "auto"),
        deadline=started + SEND_BUDGET,
    )
    return _write_outcome(clock, result, source="auto", unidentified=unidentified)


def _write_outcome(
    clock: Clock, result: WriteResult, *, source: AttemptSource, unidentified: tuple[int, ...] = ()
) -> AutoUpdate | None:
    match result.kind:
        case "up_to_date":
            return None
        case "written":
            outcome: Outcome = "written"
        case "unknown":
            outcome = "unknown"
        case "refused":
            outcome = "skipped"
        case "unreadable" | "rejected":
            outcome = "failed"
    return AutoUpdate(
        clock.now(),
        outcome,
        result.code,
        _reason(result.code, result.refusal, unidentified),
        source=source,
        changes=result.changes,
        cells=result.attempted,
        payroll_after=result.payroll,
        attempt_id=result.attempt_id,
    )
