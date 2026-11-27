"""Automatic tab updates over a saved roster and synthetic Sheets responses."""

import time
from dataclasses import dataclass, replace
from typing import Any

import httpx
import pytest
import respx

from fha.services.free_agents import row_key, set_aav_override
from fha.services.league_sheet import COLLECTION, ROW_BINDINGS, bind_tab, read_league_sheet
from fha.services.refresh import CACHE, Cached, RefreshService, save_cached
from fha.services.sheet_auto import (
    AUTO_LOG,
    AutoUpdate,
    _record_log,
    auto_update,
    checked_generation,
    log_entries,
    mark_checked,
    set_enabled,
)
from fha.services.sheet_update import WRITE_ATTEMPTS, WriteJournal, prepare_update, write_update
from fha.sources.league_sheet.models import LeagueSheetError, ParsedSheet
from fha.sources.league_sheet.writer import FakeLeagueSheetWriter, SheetsApiLeagueSheetWriter
from fha.sources.yahoo.fake import FakeYahooSource
from fha.sources.yahoo.models import LeagueSnapshot
from fha.storage.memory import InMemoryRepository
from fha.storage.repository import RepositoryError
from tests.unit.league_sheet import sheets as s
from tests.unit.services.test_league_sheet_service import ONE, TAB_ONE, TAB_TWO, TWO
from tests.unit.services.test_refresh import T0, FakeClock
from tests.unit.services.test_sheet_update_service import BEFORE, LOST, Reads, live, snapshot

OLD_TAB = replace(TAB_ONE, players=[*TAB_ONE.players[:2], None, *TAB_ONE.players[3:]])
OLD = live(OLD_TAB, TAB_TWO)
AFTER = live(
    replace(
        OLD_TAB,
        players=[
            *OLD_TAB.players[:3],
            s.Player("Oettinger, Jake", "G", "Dal", 8_000_000),
            *OLD_TAB.players[4:],
        ],
    ),
    TAB_TWO,
)


class Sheet(Reads):
    calls = 0

    async def fetch(self) -> ParsedSheet:
        self.calls += 1
        return await super().fetch()  # type: ignore[no-any-return]


@dataclass
class Setup:
    repo: InMemoryRepository
    clock: FakeClock
    snap: LeagueSnapshot
    refresh: RefreshService
    yahoo: FakeYahooSource

    async def save_generation(self) -> None:
        await save_cached(self.repo, Cached(self.snap, self.clock.now()))

    async def run(
        self, sheet: Sheet, writer: Any, *, started: float | None = None
    ) -> AutoUpdate | None:
        result = await auto_update(
            self.repo,
            self.clock,
            self.refresh,
            sheet,
            writer,
            fetched_at=self.clock.now(),
            started=time.monotonic() if started is None else started,
        )
        assert self.yahoo.calls == []
        return result


async def setup(
    stored: ParsedSheet = OLD,
    *,
    repo: InMemoryRepository | None = None,
    priced: bool = True,
    bindings: dict[str, str] | None = None,
) -> Setup:
    repo = repo or InMemoryRepository()
    clock = FakeClock()
    snap = await snapshot()
    yahoo = FakeYahooSource(snap)
    await read_league_sheet(repo, Sheet(stored), clock)
    await bind_tab(repo, "Pinecone", ONE.team_key)
    await bind_tab(repo, "Maple", TWO.team_key)
    if priced:
        await set_aav_override(repo, "3", 8_000_000)
    if bindings is None:
        bindings = {row_key("Zed Nobody", "C"): "99"}
    await repo.put(
        COLLECTION,
        ROW_BINDINGS,
        {
            "entries": {
                "Pinecone": {
                    key: {"player_id": player_id, "how": "confirmed"}
                    for key, player_id in bindings.items()
                }
            }
        },
    )
    out = Setup(repo, clock, snap, RefreshService(yahoo, repo, clock), yahoo)
    await out.save_generation()
    return out


@pytest.mark.parametrize("state", ["checked", "off", "expired", "undecodable"])
async def test_an_ineligible_snapshot_writes_and_logs_nothing(state: str) -> None:
    app = await setup()
    if state == "checked":
        await mark_checked(app.repo, T0)
    elif state == "off":
        await set_enabled(app.repo, False)
    elif state == "expired":
        app.clock.t += 1800
    else:
        meta = await app.repo.get(CACHE, "meta")
        assert meta is not None
        await app.repo.put(CACHE, "meta", {**meta, "format": 999})

    sheet, writer = Sheet(), FakeLeagueSheetWriter()
    assert await app.run(sheet, writer) is None
    assert sheet.calls == 0
    assert writer.writes == []
    assert await checked_generation(app.repo) == (T0 if state == "checked" else None)
    assert await log_entries(app.repo) == []


async def test_the_matching_stored_sheet_avoids_google_until_the_24_hour_threshold() -> None:
    app = await setup(AFTER)
    writer = FakeLeagueSheetWriter()
    app.clock.t += 24 * 3600
    await app.save_generation()
    fresh = Sheet()
    assert await app.run(fresh, writer) is None
    assert fresh.calls == 0
    app.clock.t += 1
    await app.save_generation()
    old = Sheet(AFTER)
    assert await app.run(old, writer) is None
    assert old.calls == 1
    assert writer.writes == []
    assert await log_entries(app.repo) == []


async def test_a_clean_identified_write_is_verified_and_logged() -> None:
    app = await setup()
    sheet, writer = Sheet(OLD, OLD, AFTER), FakeLeagueSheetWriter()
    preview = await prepare_update(app.repo, app.snap, OLD)
    assert preview.plan is not None
    assert preview.plan.ok
    result = await app.run(sheet, writer)
    assert result is not None
    assert result.outcome == "written"
    assert result.changes == ("drop Zed Nobody", "add Oettinger, Jake")
    assert result.payroll_after == 16_400_000
    assert writer.writes == [("Pinecone", preview.plan.edits)]
    assert result.cells == len(preview.plan.edits)
    assert sheet.calls == 3
    assert await checked_generation(app.repo) == T0
    assert await app.repo.all(WRITE_ATTEMPTS) == {}
    assert await log_entries(app.repo) == [result]


@pytest.mark.parametrize("reason", ["over-cap", "review", "cap-hit", "unprotected", "locked"])
async def test_a_blocked_plan_is_logged_without_writing(reason: str) -> None:
    sheets = {
        "over-cap": replace(live(replace(OLD_TAB, cap_value=12_000_000), TAB_TWO), cap=12_000_000),
        "review": BEFORE,
        "cap-hit": OLD,
        "unprotected": live(OLD_TAB, TAB_TWO, editable=("Pinecone", "Maple")),
        "locked": live(OLD_TAB, TAB_TWO, editable=()),
    }
    before = sheets[reason]
    app = await setup(before, priced=reason != "cap-hit")
    writer = FakeLeagueSheetWriter()
    result = await app.run(Sheet(before, before), writer)
    assert result is not None
    assert result.outcome == "skipped"
    assert result.code == {"unprotected": "others_unprotected", "locked": "own_tab_locked"}.get(
        reason, "refusal"
    )
    if reason == "over-cap":
        assert "over the" in result.reason
    elif reason == "review":
        assert "awaits match review" in result.reason
    elif reason == "cap-hit":
        assert "cap hit is unknown" in result.reason
    assert await log_entries(app.repo) == [result]
    assert writer.writes == []
    assert await app.repo.all(WRITE_ATTEMPTS) == {}


async def test_a_refusal_without_changes_is_silent() -> None:
    matching = AFTER.tab("Pinecone")
    assert matching is not None
    sheet = replace(
        AFTER, tabs=tuple(replace(t, cap=1) if t == matching else t for t in AFTER.tabs)
    )
    app = await setup(sheet)
    preview = await prepare_update(app.repo, app.snap, sheet)
    assert preview.plan is not None
    assert preview.plan.refusals
    assert preview.plan.changes == ()
    source, writer = Sheet(), FakeLeagueSheetWriter()
    assert await app.run(source, writer) is None
    assert source.calls == 0
    assert writer.writes == []
    assert await log_entries(app.repo) == []


async def test_a_stored_problem_skips_without_google_and_sanitizes_the_reason() -> None:
    hidden = "FORMULA_SENTINEL"
    broken = live(replace(OLD_TAB, payroll_formula=f"=SUM({hidden})"), TAB_TWO)
    app = await setup(broken)
    sheet, writer = Sheet(), FakeLeagueSheetWriter()
    result = await app.run(sheet, writer)
    assert result is not None
    assert result.outcome == "skipped"
    assert result.code == "problem_unreadable_tab"
    assert hidden not in result.reason
    assert hidden not in repr(await app.repo.all(AUTO_LOG))
    assert sheet.calls == 0
    assert writer.writes == []


@pytest.mark.parametrize("failure", ["fresh-read", "final-read", "write", "lost-read"])
async def test_external_failure_text_stays_out_of_the_log_and_banner(failure: str) -> None:
    hidden = "EXTERNAL_FAILURE_SENTINEL"
    unavailable = LeagueSheetError(hidden)
    fallback, writer = Sheet(), FakeLeagueSheetWriter()
    if failure == "fresh-read":
        fallback = Sheet(unavailable)
    elif failure == "final-read":
        fallback = Sheet(OLD, unavailable)
    elif failure == "write":
        fallback, writer = Sheet(OLD, OLD), FakeLeagueSheetWriter(unavailable)
    elif failure == "lost-read":
        fallback = Sheet(OLD, OLD, unavailable)
        writer = FakeLeagueSheetWriter(type(LOST)(hidden))
    app = await setup()
    result = await app.run(fallback, writer)
    assert result is not None
    assert (
        result.code
        == {
            "fresh-read": "sheet_unreadable",
            "final-read": "sheet_unreadable",
            "write": "not_sent",
            "lost-read": "lost_answer",
        }[failure]
    )
    assert hidden not in result.reason
    assert hidden not in repr(await app.repo.all(AUTO_LOG))


async def test_out_of_block_ir_content_stays_out_of_the_log_and_banner() -> None:
    hidden = "OUT_OF_BLOCK_SENTINEL"
    tab = replace(
        OLD_TAB,
        below=[*OLD_TAB.below, s.IRRow("notes"), s.IRRow("IR+", s.Player(hidden, "D", "EDM", 1))],
    )
    sheet = live(tab, TAB_TWO)
    app = await setup(sheet)
    result = await app.run(Sheet(sheet), FakeLeagueSheetWriter())
    assert result is not None
    assert result.code == "refusal"
    assert hidden not in result.reason
    assert hidden not in repr(await app.repo.all(AUTO_LOG))


async def test_a_binding_removed_during_the_final_read_blocks_the_drop() -> None:
    app = await setup()

    class UnbindOnFinalRead(Sheet):
        async def fetch(self) -> ParsedSheet:
            if self.calls == 1:
                await app.repo.delete(COLLECTION, ROW_BINDINGS)
            return await super().fetch()

    writer = FakeLeagueSheetWriter()
    result = await app.run(UnbindOnFinalRead(OLD, OLD), writer)
    assert result is not None
    assert result.code == "unidentified_drop"
    assert "row 10" in result.reason
    assert "Zed Nobody" not in result.reason
    assert "Zed Nobody" not in repr(await app.repo.all(AUTO_LOG))
    assert writer.writes == []


@pytest.mark.parametrize("same_name", [False, True], ids=["same-player", "same-key"])
async def test_two_drops_cannot_share_a_binding(same_name: bool) -> None:
    name = "Zed Nobody" if same_name else "Row Name Sentinel"
    sheet = live(
        replace(OLD_TAB, players=[*OLD_TAB.players, s.Player(name, "C", "TBL", 100)]), TAB_TWO
    )
    bindings = {row_key("Zed Nobody", "C"): "99", row_key(name, "C"): "99"}
    app = await setup(sheet, bindings=bindings)
    writer = FakeLeagueSheetWriter()
    result = await app.run(Sheet(sheet, sheet), writer)
    assert result is not None
    assert result.code == "unidentified_drop"
    assert "rows 10, 12" in result.reason
    assert name not in repr(await app.repo.all(AUTO_LOG))
    assert writer.writes == []


@pytest.mark.parametrize("code", ["superseded", "switched_off", "earlier_unknown"])
async def test_final_hook_rechecks_the_saved_generation_switch_and_attempts(code: str) -> None:
    app = await setup()

    class ChangeDuringFinalRead(Sheet):
        async def fetch(self) -> ParsedSheet:
            if self.calls == 1:
                if code == "superseded":
                    await save_cached(app.repo, Cached(app.snap, T0 + 1))
                elif code == "switched_off":
                    await set_enabled(app.repo, False)
                else:
                    await WriteJournal(app.repo, "manual").record("other", T0, 5)
            return await super().fetch()

    writer = FakeLeagueSheetWriter()
    result = await app.run(ChangeDuringFinalRead(OLD, OLD), writer)
    assert result is not None
    assert result.outcome == "skipped"
    assert result.code == code
    assert writer.writes == []
    assert set(await app.repo.all(WRITE_ATTEMPTS)) == (
        {"other"} if code == "earlier_unknown" else set()
    )


@pytest.mark.parametrize("slow", ["record", "token"])
async def test_elapsed_time_after_the_marker_or_token_cannot_send_a_batch(slow: str) -> None:
    monotonic = 20.0

    class SlowMarker(InMemoryRepository):
        async def put(self, collection: str, doc_id: str, doc: Any) -> None:
            nonlocal monotonic
            await super().put(collection, doc_id, doc)
            if slow == "record" and collection == WRITE_ATTEMPTS:
                monotonic += 36

    async def token() -> str:
        nonlocal monotonic
        if slow == "token":
            monotonic += 36
        return "test-token"

    app = await setup(repo=SlowMarker())
    with respx.mock(assert_all_called=False) as router:
        batch = router.post(
            "https://sheets.googleapis.com/v4/spreadsheets/test:batchUpdate"
        ).respond(200)
        async with httpx.AsyncClient() as http:
            writer = SheetsApiLeagueSheetWriter(http, "test", token, monotonic=lambda: monotonic)
            result = await app.run(Sheet(OLD, OLD), writer, started=monotonic)
    assert result is not None
    assert result.code == "out_of_time"
    assert result.cells == 0
    assert not batch.called
    assert await app.repo.all(WRITE_ATTEMPTS) == {}


async def test_a_lost_manual_answer_blocks_auto_then_keeps_evidence_until_a_fresh_read() -> None:
    app = await setup()
    digest = (await prepare_update(app.repo, app.snap, OLD)).digest
    result = await write_update(
        app.repo,
        app.clock,
        Sheet(OLD, OLD),
        FakeLeagueSheetWriter(LOST),
        app.snap,
        digest,
        journal=WriteJournal(app.repo, "manual"),
    )
    assert result.kind == "unknown"
    assert result.attempt_id is not None
    attempt_data = await app.repo.all(WRITE_ATTEMPTS)
    assert set(attempt_data) == {result.attempt_id}
    await read_league_sheet(app.repo, Sheet(AFTER), app.clock)  # the stored tab now matches

    app.clock.t += 10
    await app.save_generation()
    writer = FakeLeagueSheetWriter()
    skip = await app.run(Sheet(), writer)
    assert skip is not None
    assert skip.code == "earlier_unknown"

    app.clock.t += 600
    await app.save_generation()
    failure = await app.run(Sheet(LeagueSheetError("READ_SENTINEL")), writer)
    assert failure is not None
    assert failure.code == "sheet_unreadable"
    assert await app.repo.all(WRITE_ATTEMPTS) == attempt_data
    unknown_key = f"attempt-{result.attempt_id}"
    first_unknown = await app.repo.get(AUTO_LOG, unknown_key)
    assert first_unknown is not None
    assert first_unknown["source"] == "manual"
    assert first_unknown["outcome"] == "unknown"

    app.clock.t += 1
    await app.save_generation()
    reread = Sheet(AFTER)
    assert await app.run(reread, writer) is None
    assert reread.calls == 1
    assert writer.writes == []
    assert await app.repo.all(WRITE_ATTEMPTS) == {}
    assert await app.repo.get(AUTO_LOG, unknown_key) == first_unknown
    assert "READ_SENTINEL" not in repr(await app.repo.all(AUTO_LOG))


async def test_an_expired_attempt_forces_a_read_and_an_unbound_late_add_is_not_dropped() -> None:
    hidden = "LATE_ADDITION_SENTINEL"
    late = live(
        replace(
            OLD_TAB,
            players=[*OLD_TAB.players[:3], s.Player(hidden, "C", "TBL", 1), *OLD_TAB.players[4:]],
        ),
        TAB_TWO,
    )
    app = await setup(AFTER, bindings={})
    await WriteJournal(app.repo, "auto").record("late", T0 - 601, 4)
    writer = FakeLeagueSheetWriter()
    sheet = Sheet(late, late)
    result = await app.run(sheet, writer)
    assert result is not None
    assert result.code == "unidentified_drop"
    assert "row 10" in result.reason
    assert hidden not in repr(await app.repo.all(AUTO_LOG))
    assert sheet.calls == 2
    assert writer.writes == []
    assert await app.repo.all(WRITE_ATTEMPTS) == {}
    assert (await app.repo.get(AUTO_LOG, "attempt-late") or {})["outcome"] == "unknown"


@pytest.mark.parametrize("failure", ["logging", "deleting"])
async def test_a_verified_result_survives_post_outcome_storage_failures(failure: str) -> None:
    class FlakyRepo(InMemoryRepository):
        blocked = True

        async def put(self, collection: str, doc_id: str, doc: Any) -> None:
            if collection == AUTO_LOG and self.blocked and failure == "logging":
                raise RepositoryError("STORE_LOG_SENTINEL")
            await super().put(collection, doc_id, doc)

        async def delete(self, collection: str, doc_id: str) -> None:
            if collection == WRITE_ATTEMPTS and self.blocked and failure == "deleting":
                raise RepositoryError("STORE_DELETE_SENTINEL")
            await super().delete(collection, doc_id)

    repo = FlakyRepo()
    app = await setup(repo=repo)
    writer = FakeLeagueSheetWriter()
    result = await app.run(Sheet(OLD, OLD, AFTER), writer)
    assert result is not None
    assert result.outcome == "written"
    assert result.payroll_after == 16_400_000
    assert len(writer.writes) == 1
    if failure == "logging":
        assert await log_entries(repo) == []
    else:
        [attempt] = await repo.all(WRITE_ATTEMPTS)
        saved = await repo.get(AUTO_LOG, f"attempt-{attempt}")
        assert saved is not None
        assert saved["outcome"] == "written"
        repo.blocked = False
        app.clock.t += 601
        await app.save_generation()
        reread = Sheet(AFTER)
        assert await app.run(reread, writer) is None
        assert reread.calls == 1
        assert len(writer.writes) == 1
        assert await repo.all(WRITE_ATTEMPTS) == {}
        assert await log_entries(repo) == [result]


async def test_a_storage_failure_before_the_outcome_has_a_fixed_failure_reason() -> None:
    class FailingStore(InMemoryRepository):
        async def get(self, collection: str, doc_id: str) -> Any:
            if collection == CACHE:
                raise RepositoryError("STORE_SENTINEL")
            return await super().get(collection, doc_id)

    app = await setup(repo=FailingStore())
    source, writer = Sheet(), FakeLeagueSheetWriter()
    result = await app.run(source, writer)
    assert result is not None
    assert result.outcome == "failed"
    assert result.reason == "the app's storage failed"
    assert "STORE_SENTINEL" not in repr(await app.repo.all(AUTO_LOG))
    assert source.calls == 0
    assert writer.writes == []


async def test_log_dedupes_only_consecutive_skips_with_the_same_code_and_reason() -> None:
    repo = InMemoryRepository()
    first = AutoUpdate(T0, "skipped", "refusal", "first refusal")
    await _record_log(repo, first)
    await _record_log(repo, replace(first, at=T0 + 1))
    assert await log_entries(repo) == [first]
    different = replace(first, at=T0 + 2, reason="another refusal")
    await _record_log(repo, different)
    fail = replace(first, at=T0 + 3, outcome="failed")
    await _record_log(repo, fail)
    last = replace(first, at=T0 + 4)
    await _record_log(repo, last)
    assert await log_entries(repo) == [last, fail, different, first]


async def test_log_keeps_equal_times_and_prunes_by_time_across_id_formats() -> None:
    repo = InMemoryRepository()
    old = AutoUpdate(T0, "unknown", "lost_answer", attempt_id="oldest")
    await _record_log(repo, old)
    for seconds in range(1, 49):
        await _record_log(repo, AutoUpdate(T0 + seconds, "failed", "not_sent"))
    at = T0 + 49
    same_time = AutoUpdate(at, "failed", "storage_failed")
    await _record_log(repo, same_time)
    await _record_log(repo, same_time)
    entries = await log_entries(repo)
    assert len(entries) == 50
    assert entries[:2] == [same_time, same_time]
    assert entries[-1].at == T0 + 1
    assert await repo.get(AUTO_LOG, "attempt-oldest") is None
