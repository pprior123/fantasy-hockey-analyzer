"""Automatic tab updates over a saved roster and synthetic Sheets responses."""

import asyncio
import time
from dataclasses import dataclass, replace
from typing import Any

import httpx
import pytest
import respx

from fha.services import sheet_auto
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
from fha.sources.yahoo import oauth
from fha.sources.yahoo.client import YahooClient
from fha.sources.yahoo.fake import FakeYahooSource
from fha.sources.yahoo.models import LeagueSnapshot, RosterEntry, Rosters, Team
from fha.sources.yahoo.oauth import Credentials, Token
from fha.sources.yahoo.source import HttpYahooSource
from fha.storage.memory import InMemoryRepository
from fha.storage.repository import RepositoryError
from tests.unit.league_sheet import sheets as s
from tests.unit.services.test_league_sheet_service import ONE, TAB_ONE, TAB_TWO, TWO, yplayer
from tests.unit.services.test_refresh import T0, FakeClock
from tests.unit.services.test_sheet_update_service import BEFORE, LOST, Reads, live, snapshot

OLD_TAB = replace(TAB_ONE, players=[*TAB_ONE.players[:2], None, *TAB_ONE.players[3:]])
OLD = live(OLD_TAB, TAB_TWO)
OETTINGER = s.Player("Oettinger, Jake", "G", "Dal", 8_000_000)
BO_STONE = s.Player("Bo Stone", "D", "EDM", 2_000_000)
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

    def yahoo_now(self, *teams: Team, date: str | None = None) -> None:
        """What a fresh roster read returns from now on."""
        self.yahoo.rosters = Rosters(teams, date)

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


def moved(team: Team, player_id: str, slot: str) -> Team:
    """``team`` with one player moved to another roster slot."""
    return replace(
        team,
        roster=tuple(
            replace(entry, selected_position=slot) if entry.player.player_id == player_id else entry
            for entry in team.roster
        ),
    )


# The owner's tab after he moved Bo Stone off IR by hand: a counted row, an empty IR slot.
OFF_IR = live(
    replace(
        OLD_TAB,
        players=[*OLD_TAB.players[:2], BO_STONE, OETTINGER, *OLD_TAB.players[4:]],
        below=[s.IRRow("IR")],
    ),
    TAB_TWO,
)


@pytest.mark.parametrize("stored", ["current", "stale"])
async def test_a_hand_fix_off_ir_is_not_written_back_from_the_cached_roster(stored: str) -> None:
    """The owner's report (2026-11-28): the cached snapshot still has Bo Stone in IR,
    Yahoo and the tab now have him in a counted row. Nothing is written, whether the
    stored sheet decides it or (past 24 hours) a live read does."""
    app = await setup(OFF_IR)
    if stored == "stale":
        app.clock.t += 24 * 3600 + 1
        await app.save_generation()
    in_ir = [e.selected_position for e in app.snap.teams[0].roster if e.player.player_id == "4"]
    assert in_ir == ["IR"]
    app.yahoo_now(moved(ONE, "4", "BN"), TWO)
    sheet, writer = Sheet(OFF_IR, OFF_IR, OFF_IR), FakeLeagueSheetWriter()
    result = await app.run(sheet, writer)
    assert writer.writes == []
    assert result is None
    assert sheet.calls == (0 if stored == "current" else 1)
    assert await log_entries(app.repo) == []
    assert app.yahoo.roster_calls == [app.snap.settings.league_key]


def pinecone(players: list[s.Player | None], *below: s.IRRow) -> ParsedSheet:
    """The owner's tab with these counted rows and IR rows, beside Maple."""
    return live(replace(OLD_TAB, players=players, below=list(below)), TAB_TWO)


ANDERSEN, CY = OLD_TAB.players[1], OLD_TAB.players[4]
ADA = OLD_TAB.players[0]
# AFTER, with a free IR+ row under Bo Stone's.
ROOM = pinecone([ADA, ANDERSEN, None, OETTINGER, CY], s.IRRow("IR", BO_STONE), s.IRRow("IR+"))


async def test_a_move_to_ir_since_the_fetch_is_planned_and_written_from_the_fresh_roster() -> None:
    app = await setup(ROOM)
    app.yahoo_now(moved(ONE, "1", "IR+"), TWO, date="2026-11-28")
    written = pinecone(
        [None, ANDERSEN, None, OETTINGER, CY], s.IRRow("IR", BO_STONE), s.IRRow("IR+", ADA)
    )
    sheet, writer = Sheet(ROOM, ROOM, written), FakeLeagueSheetWriter()
    result = await app.run(sheet, writer)
    assert result is not None
    assert result.outcome == "written"  # a cached re-plan would end as tab_changed
    assert result.changes == ("to_ir Ada Knight",)
    assert result.roster_date == "2026-11-28"
    assert [edit.row for edit in writer.writes[0][1]] == [7] * 4 + [13] * 4
    assert await log_entries(app.repo) == [result]
    assert (await app.repo.all(AUTO_LOG)).popitem()[1]["roster_date"] == "2026-11-28"
    assert app.yahoo.settles == []  # a successful read waits for nothing


async def test_a_move_off_ir_since_the_fetch_is_written_though_the_stored_tab_has_him_in_ir() -> (
    None
):
    app = await setup(AFTER)
    app.yahoo_now(moved(ONE, "4", "BN"), TWO)
    sheet, writer = Sheet(AFTER, AFTER, OFF_IR), FakeLeagueSheetWriter()
    result = await app.run(sheet, writer)
    assert result is not None
    assert result.outcome == "written"
    assert result.changes == ("back Bo Stone",)
    assert result.roster_date is None
    assert "roster_date" not in (await app.repo.all(AUTO_LOG)).popitem()[1]


async def test_a_drop_since_the_fetch_passes_the_unidentified_drop_guard() -> None:
    app = await setup(AFTER, bindings={row_key("Ada Knight", "C"): "1"})
    assert "1" in {e.player.player_id for e in ONE.roster}  # the cached roster still has her
    app.yahoo_now(replace(ONE, roster=ONE.roster[1:]), TWO)
    dropped = pinecone([None, ANDERSEN, None, OETTINGER, CY], s.IRRow("IR", BO_STONE))
    sheet, writer = Sheet(AFTER, AFTER, dropped), FakeLeagueSheetWriter()
    result = await app.run(sheet, writer)
    assert result is not None
    assert result.outcome == "written"
    assert result.changes == ("drop Ada Knight",)


async def test_every_teams_rows_are_matched_against_the_fresh_rosters() -> None:
    gus = s.Player("Gus Lake", "C", "NYR", 1_000_000)
    after_tab = replace(OLD_TAB, players=[ADA, ANDERSEN, None, OETTINGER, CY])
    sheet = live(after_tab, replace(TAB_TWO, players=[*TAB_TWO.players, gus]))
    app = await setup(sheet)
    newcomer = RosterEntry(yplayer("13", "Gus Lake", "NYR"), "C")
    app.yahoo_now(ONE, replace(TWO, roster=(*TWO.roster, newcomer)))
    assert await app.run(Sheet(), FakeLeagueSheetWriter()) is None
    bindings = await app.repo.get(COLLECTION, ROW_BINDINGS)
    assert bindings is not None
    assert bindings["entries"]["Maple"][row_key("Gus Lake", "C")]["player_id"] == "13"


async def test_my_team_missing_from_the_fresh_rosters_skips() -> None:
    app = await setup()
    app.yahoo_now(TWO, date="2026-11-28")
    sheet, writer = Sheet(), FakeLeagueSheetWriter()
    result = await app.run(sheet, writer)
    assert result is not None
    assert (result.outcome, result.code) == ("skipped", "problem_no_team")
    assert result.roster_date == "2026-11-28"
    assert sheet.calls == 0
    assert writer.writes == []


async def test_a_player_added_since_the_fetch_without_a_cap_hit_is_refused() -> None:
    app = await setup(AFTER)
    newcomer = RosterEntry(yplayer("21", "Nils Newman", "DET"), "BN")
    app.yahoo_now(replace(ONE, roster=(*ONE.roster, newcomer)), TWO)
    sheet, writer = Sheet(AFTER), FakeLeagueSheetWriter()
    result = await app.run(sheet, writer)
    assert result is not None
    assert (result.outcome, result.code) == ("skipped", "refusal")
    assert "Nils Newman" in result.reason
    assert "cap hit is unknown" in result.reason
    assert writer.writes == []


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_an_unreadable_roster_skips_without_reading_or_writing_the_sheet(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hidden = "ROSTER_FAILURE_SENTINEL"
    app = await setup()

    class Unreadable(FakeYahooSource):
        async def fetch_rosters(self, league_key: str) -> Rosters:
            if failure == "timeout":
                await asyncio.sleep(60)
            raise httpx.ConnectError(hidden)

    monkeypatch.setattr(sheet_auto, "ROSTER_TIMEOUT", 0.01)
    unreadable = Unreadable(app.snap)
    app.refresh = RefreshService(unreadable, app.repo, app.clock)
    sheet, writer = Sheet(), FakeLeagueSheetWriter()
    result = await app.run(sheet, writer)
    assert result is not None
    assert (result.outcome, result.code) == ("skipped", "roster_unreadable")
    assert result.reason == "your Yahoo roster couldn't be checked just now"
    assert result.roster_date is None
    assert hidden not in repr(await app.repo.all(AUTO_LOG))
    assert await log_entries(app.repo) == [result]
    assert sheet.calls == 0
    assert writer.writes == []
    assert await checked_generation(app.repo) == T0
    assert unreadable.settles == [sheet_auto.SETTLE_TIMEOUT]  # a pending token save may land


class RotatingYahoo:
    """Yahoo's token endpoint rotating the refresh token (the old one retired), and a
    roster call that never answers in time."""

    def __init__(self) -> None:
        self.refresh_tokens = {"refresh-1"}
        self.access_tokens: set[str] = set()
        self.renewals = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == oauth.TOKEN_URL:
            form = dict(item.split("=", 1) for item in request.content.decode().split("&"))
            if form["refresh_token"] not in self.refresh_tokens:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.renewals += 1
            access, refresh = f"access-{self.renewals + 1}", f"refresh-{self.renewals + 1}"
            self.refresh_tokens, self.access_tokens = {refresh}, {access}
            return httpx.Response(
                200, json={"access_token": access, "refresh_token": refresh, "expires_in": 3600}
            )
        if request.headers["Authorization"].removeprefix("Bearer ") not in self.access_tokens:
            return httpx.Response(401, json={"error": {"description": "expired"}})
        if "/teams/roster" in request.url.path:
            await asyncio.sleep(60)
        return httpx.Response(200, json={"fantasy_content": {"ok": 1}})


class StalledStore:
    """A token store whose saves wait for ``release``."""

    def __init__(self, token: Token) -> None:
        self.token = token
        self.release = asyncio.Event()

    async def load(self) -> Token | None:
        return self.token

    async def save(self, token: Token) -> None:
        await self.release.wait()
        self.token = token


def yahoo_client(yahoo: RotatingYahoo, store: StalledStore, clock: FakeClock) -> YahooClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(yahoo))
    return YahooClient(http, Credentials("id", "secret"), store, clock=clock.now)


@pytest.mark.parametrize("store_answers", ["within-bound", "never"])
async def test_a_roster_timeout_mid_renewal_waits_for_the_token_save_before_skipping(
    store_answers: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """FR-3: a timed-out roster read can leave a rotated token's save running. The skip
    waits (bounded) for it, so a cold instance finds the rotated token in the store."""
    app = await setup()
    yahoo = RotatingYahoo()
    store = StalledStore(Token("access-1", "refresh-1", app.clock.now() - 1))
    source = HttpYahooSource(yahoo_client(yahoo, store, app.clock))
    app.refresh = RefreshService(source, app.repo, app.clock)
    monkeypatch.setattr(sheet_auto, "ROSTER_TIMEOUT", 0.05)
    settle = 0.3 if store_answers == "never" else 5.0
    monkeypatch.setattr(sheet_auto, "SETTLE_TIMEOUT", settle)
    if store_answers == "within-bound":
        asyncio.get_running_loop().call_later(0.15, store.release.set)
    sheet, writer = Sheet(), FakeLeagueSheetWriter()
    result = await app.run(sheet, writer)
    assert result is not None
    assert (result.outcome, result.code) == ("skipped", "roster_unreadable")
    assert yahoo.renewals == 1
    assert sheet.calls == 0
    assert writer.writes == []
    if store_answers == "never":
        assert store.token.refresh_token not in yahoo.refresh_tokens  # bounded: didn't wait on
        assert "Yahoo token save still pending" in caplog.text
        store.release.set()
        await asyncio.sleep(0)
        return
    assert store.token.refresh_token in yahoo.refresh_tokens  # the rotated one, saved before
    cold = yahoo_client(yahoo, store, app.clock)  # a new instance, on the same store
    assert await cold.get("league/x/settings") == {"ok": 1}
    assert yahoo.renewals == 1  # it used the rotated token as stored


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
    assert app.yahoo.roster_calls == []


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


@pytest.mark.parametrize("change", ["switch", "generation"])
async def test_changes_during_token_acquisition_cancel_the_post_and_clear_its_record(
    change: str,
) -> None:
    app = await setup()

    async def token() -> str:
        assert len(await app.repo.all(WRITE_ATTEMPTS)) == 1
        if change == "switch":
            await set_enabled(app.repo, False)
        else:
            app.clock.t += 1
            mine = replace(ONE, roster=tuple(e for e in ONE.roster if e.player.player_id != "3"))
            newer = replace(app.snap, teams=(mine, TWO))
            await save_cached(app.repo, Cached(newer, app.clock.now()))
        return "token-for-test"

    with respx.mock(assert_all_called=False) as router:
        post = router.post(
            "https://sheets.googleapis.com/v4/spreadsheets/test:batchUpdate"
        ).respond(200)
        async with httpx.AsyncClient() as http:
            writer = SheetsApiLeagueSheetWriter(http, "test", token)
            result = await app.run(Sheet(OLD, OLD, AFTER), writer)
    assert not post.called
    assert result is not None
    assert result.outcome == "skipped"
    assert result.code == ("switched_off" if change == "switch" else "superseded")
    assert result.cells == 0
    assert await app.repo.all(WRITE_ATTEMPTS) == {}


async def test_repeated_post_token_refusals_preserve_each_attempts_outcome_in_recovery() -> None:
    class FailingDelete(InMemoryRepository):
        blocked = False

        async def delete(self, collection: str, doc_id: str) -> None:
            if self.blocked and collection == WRITE_ATTEMPTS:
                raise RepositoryError("test attempt delete failure")
            await super().delete(collection, doc_id)

    repo = FailingDelete()
    app = await setup(repo=repo)

    async def token() -> str:
        await set_enabled(repo, False)
        return "token-for-test"

    outcomes = []
    with respx.mock(assert_all_called=False) as router:
        post = router.post(
            "https://sheets.googleapis.com/v4/spreadsheets/test:batchUpdate"
        ).respond(200)
        async with httpx.AsyncClient() as http:
            writer = SheetsApiLeagueSheetWriter(http, "test", token)
            for blocked in (False, True):
                repo.blocked = blocked
                app.clock.t += 1
                await set_enabled(repo, True)
                await app.save_generation()
                result = await app.run(Sheet(OLD, OLD), writer)
                assert result is not None
                assert (result.outcome, result.code, result.cells) == ("skipped", "switched_off", 0)
                outcomes.append(result)

            [attempt_id] = await repo.all(WRITE_ATTEMPTS)
            assert attempt_id == outcomes[1].attempt_id
            log_before = await repo.all(AUTO_LOG)
            repo.blocked = False
            app.clock.t += 601
            await set_enabled(repo, True)
            await app.save_generation()
            fresh = Sheet(AFTER)
            assert await app.run(fresh, writer) is None
            assert fresh.calls == 1

    assert not post.called
    assert await repo.all(WRITE_ATTEMPTS) == {}
    assert await repo.all(AUTO_LOG) == log_before
    saved = await repo.get(AUTO_LOG, f"attempt-{attempt_id}")
    assert saved is not None
    assert (saved["outcome"], saved["code"], saved["cells"]) == ("skipped", "switched_off", 0)
    assert await log_entries(repo) == list(reversed(outcomes))


@pytest.mark.parametrize("failure", ["hook", "attempt-put", "post-hook"])
async def test_storage_failures_before_the_send_leave_no_batch_or_attempt(failure: str) -> None:
    class FailingStore(InMemoryRepository):
        fail_check = False

        async def get(self, collection: str, doc_id: str) -> Any:
            if self.fail_check and collection == CACHE:
                raise RepositoryError("private stored identifier")
            return await super().get(collection, doc_id)

        async def put(self, collection: str, doc_id: str, doc: Any) -> None:
            if failure == "attempt-put" and collection == WRITE_ATTEMPTS:
                raise RepositoryError("private stored identifier")
            await super().put(collection, doc_id, doc)

    repo = FailingStore()
    app = await setup(repo=repo)

    class FailOnFinalRead(Sheet):
        async def fetch(self) -> ParsedSheet:
            if failure == "hook" and self.calls == 1:
                repo.fail_check = True
            return await super().fetch()

    async def token() -> str:
        if failure == "post-hook":
            repo.fail_check = True
        return "token-for-test"

    with respx.mock(assert_all_called=False) as router:
        post = router.post(
            "https://sheets.googleapis.com/v4/spreadsheets/test:batchUpdate"
        ).respond(200)
        async with httpx.AsyncClient() as http:
            writer = SheetsApiLeagueSheetWriter(http, "test", token)
            result = await app.run(FailOnFinalRead(OLD, OLD, AFTER), writer)
    assert not post.called
    assert result is not None
    assert result.outcome == "failed"
    assert result.code == "storage_failed"
    assert "private stored identifier" not in result.reason
    assert await repo.all(WRITE_ATTEMPTS) == {}


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
    assert app.yahoo.roster_calls == []  # no Yahoo read while an outcome is unknown

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
