"""scripts/record_yahoo.py against the synthetic league (no network)."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from fha.sources.yahoo import oauth
from fha.sources.yahoo.oauth import Credentials, Token
from fha.sources.yahoo.parse import YahooParseError
from scripts import record_yahoo as rec
from scripts.sanitize_yahoo import problems
from scripts.yahoo_common import JsonFileTokenStore
from tests.unit.yahoo import builders as b
from tests.unit.yahoo.fake_league import FREE, LK, League

CREDS = Credentials("cid", "csecret-hidden")
NOW = 1_000_000.0


class Yahoo:
    def __init__(self) -> None:
        self.league = League(strict=False)
        self.refreshes = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == oauth.TOKEN_URL:
            self.refreshes += 1
            return httpx.Response(200, json={"access_token": "acc-hidden", "expires_in": 3600})
        return await self.league(request)


async def store(tmp_path: Path, expires_at: float = NOW + 3600) -> JsonFileTokenStore:
    s = JsonFileTokenStore(tmp_path / "token.json")
    await s.save(Token("acc-old", "ref-hidden", expires_at))
    return s


async def record(tmp_path: Path, yahoo: Yahoo, **kw: Any) -> rec.Summary:
    return await rec.record(
        CREDS,
        kw.pop("token_store", None) or await store(tmp_path),
        httpx.MockTransport(yahoo),
        fixture_dir=tmp_path / "fixtures",
        probe_dir=tmp_path / "probes",
        clock=lambda: NOW,
        available=kw.pop("available", 60),
    )


def written(directory: Path) -> dict[str, Any]:
    return {p.name: json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))}


def test_api_path_and_file_name() -> None:
    url = httpx.URL("https://fantasysports.yahooapis.com/fantasy/v2/league/1;x=2/y?format=json")
    assert rec.api_path(url) == "league/1;x=2/y"
    assert rec.file_name(7, "players;player_keys=465.p.1,465.p.2/stats") == (
        "007-players-player-keys-465-p-1-465-p-2-stats.json"
    )
    assert len(rec.file_name(0, "x" * 200)) == len("000-") + 60 + len(".json")


async def test_refresh_is_recorded_as_sanitized_fixtures_with_a_manifest(tmp_path: Path) -> None:
    yahoo = Yahoo()
    summary = await record(tmp_path, yahoo)
    files = written(tmp_path / "fixtures")
    manifest = files.pop(rec.MANIFEST)
    assert manifest["params"] == {"league_id": 8076, "available": 60, "available_sort": "AR"}
    calls = manifest["calls"]
    assert summary.calls == len(calls) == 15
    assert [c["path"] for c in calls] == yahoo.league.paths[:15]
    assert {c["status"] for c in calls} == {200}
    assert sorted(c["file"] for c in calls) == sorted(files)
    text = json.dumps(files)
    assert "managers" not in text
    assert "@" not in text
    assert all(problems(body) == [] for body in files.values())
    rosters = files[next(c["file"] for c in calls if c["path"].endswith("/teams/roster"))]
    assert {"name": "Team 1"} in rosters["fantasy_content"]["league"][1]["teams"]["0"]["team"][0]


async def test_probes_go_to_their_own_directory_and_failures_are_notes(tmp_path: Path) -> None:
    yahoo = Yahoo()
    yahoo.league.overrides["games;game_codes=nhl;seasons=2025"] = b.games(("453", 2025))
    summary = await record(tmp_path, yahoo)
    probes = written(tmp_path / "probes")
    paths = [c["path"] for c in probes.pop(rec.MANIFEST)["calls"]]
    assert paths[0] == "game/nhl"
    assert "games;game_codes=nhl;seasons=2025" in paths
    assert any(p.startswith("players;player_keys=453.p.1,453.p.2,") for p in paths)
    assert f"league/{LK}/players;status=A;sort=OR;start=0;count=25" in paths
    assert len(probes) == len(paths)
    assert any("HTTP 400 for game/nhl" in n for n in summary.notes)
    assert any("453.p.1" in n for n in summary.notes)  # the unknown old keys failed too
    fixture_paths = [c["path"] for c in written(tmp_path / "fixtures")[rec.MANIFEST]["calls"]]
    assert not set(paths) & set(fixture_paths)


async def test_probe_sample_has_skaters_and_a_goalie(tmp_path: Path) -> None:
    summary = await record(tmp_path, Yahoo())
    paths = rec.probe_paths(summary.snapshot)
    # The first four skaters in the pool (1, 2, 4, 5), then its first goalie (3).
    keys = "465.p.1,465.p.2,465.p.4,465.p.5,465.p.3"
    assert paths[2] == f"league/{LK}/players;player_keys={keys}/stats;type=season"
    assert paths[3] == f"league/{LK}/players;player_keys={keys}/stats;type=season;season=2025"


async def test_the_token_endpoint_is_never_recorded(tmp_path: Path) -> None:
    yahoo = Yahoo()
    await record(tmp_path, yahoo, token_store=await store(tmp_path, expires_at=NOW))
    assert yahoo.refreshes == 1
    everything = json.dumps(written(tmp_path / "fixtures")) + json.dumps(
        written(tmp_path / "probes")
    )
    for secret in ("acc-hidden", "acc-old", "ref-hidden", "csecret-hidden"):
        assert secret not in everything
    assert "get_token" not in everything


async def test_a_rerun_replaces_old_fixtures(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "999-stale.json").write_text("{}")
    (fixtures / "README.md").write_text("kept")
    await record(tmp_path, Yahoo())
    assert not (fixtures / "999-stale.json").exists()
    assert (fixtures / "README.md").read_text() == "kept"


async def test_a_failed_refresh_keeps_what_it_recorded_in_private(tmp_path: Path) -> None:
    yahoo = Yahoo()
    yahoo.league.settings["league_key"] = "465.l.9999"
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "000-previous.json").write_text("{}")
    with pytest.raises(YahooParseError, match="asked for league"):
        await record(tmp_path, yahoo)
    failed = written(tmp_path / "probes" / "failed")
    assert [c["path"] for c in failed.pop(rec.MANIFEST)["calls"]] == yahoo.league.paths
    assert (tmp_path / "fixtures" / "000-previous.json").exists()  # untouched


def test_nothing_is_written_if_sanitizing_leaves_an_email(tmp_path: Path) -> None:
    leaky = b.P("777", "Mail me at x.y@example.com")
    records = [
        rec.Record("fine", 200, b.envelope(b.league_players(FREE[:2]))),
        rec.Record("leaky", 200, b.envelope(b.league_players([leaky]))),
    ]
    expected = r"2 problems.*\n  leaky: \$\..*name\.full: email address"
    with pytest.raises(rec.SanitizeError, match=expected):
        rec.write_records(records, tmp_path / "fixtures", {})
    assert not (tmp_path / "fixtures").exists()


async def test_non_json_responses_are_recorded_as_null(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>down</html>")

    transport = rec.RecordingTransport(httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=transport) as http:
        await http.get("https://fantasysports.yahooapis.com/fantasy/v2/game/nhl?format=json")
        await http.get("https://api.login.yahoo.com/other")
    assert transport.records == [rec.Record("game/nhl", 500, None)]


async def test_summary_reports_what_the_owner_should_check(tmp_path: Path) -> None:
    summary = await record(tmp_path, Yahoo())
    text = "\n".join(summary.lines())
    assert "Recorded 15 API calls" in text
    assert "(game 465, season 2026)" in text
    assert "Teams: 2; rostered players: 6; available: 60; pool: 66." in text
    assert f"My team: {LK}.t.1." in text
    assert "Week 1 of 1-25; matchups this week: 1; next week: 1." in text
    assert "PPP: direct" in text
    assert "GP 0 (goalies 29)" in text
    assert "Players with GP > 0 last season: 2 of 66." in text


async def test_summary_explains_a_stat_map_failure(tmp_path: Path) -> None:
    yahoo = Yahoo()
    no_gp = [c for c in b.GAME_CATEGORIES if c[2] != "GP"]
    yahoo.league.overrides["game/465/stat_categories"] = b.game_stat_categories(no_gp)
    text = "\n".join((await record(tmp_path, yahoo)).lines())
    assert "STAT MAP FAILED: no Yahoo stat 'GP'" in text
    assert "Yahoo's stat names: A, BLK, G," in text


async def test_main_needs_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("YAHOO_CLIENT_ID", raising=False)
    monkeypatch.delenv("YAHOO_CLIENT_SECRET", raising=False)
    assert await rec._main() == 1
    assert "not set" in capsys.readouterr().err


async def test_main_needs_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("YAHOO_CLIENT_ID", "c")
    monkeypatch.setenv("YAHOO_CLIENT_SECRET", "s")
    monkeypatch.setattr(rec, "JsonFileTokenStore", lambda: JsonFileTokenStore(tmp_path / "none"))
    assert await rec._main() == 1
    assert "scripts.yahoo_auth first" in capsys.readouterr().err
