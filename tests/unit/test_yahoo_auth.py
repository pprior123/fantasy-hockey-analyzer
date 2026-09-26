"""scripts/yahoo_auth.py and scripts/yahoo_common.py, with Yahoo mocked."""

import json
import os
import stat
from pathlib import Path
from typing import IO
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from fha.sources.yahoo import oauth
from fha.sources.yahoo.oauth import Token, YahooAuthError
from scripts import yahoo_auth
from scripts.yahoo_common import JsonFileTokenStore, SetupError, credentials_from_env
from tests.unit.yahoo.fake_league import League

ENV = {"YAHOO_CLIENT_ID": "cid-visible", "YAHOO_CLIENT_SECRET": "csecret-hidden"}
NOW = 1_000_000.0


# ---------------------------------------------------------------- common


def test_credentials_from_env() -> None:
    creds = credentials_from_env(ENV | {"YAHOO_REDIRECT_URI": "https://example.test:9"})
    assert (creds.client_id, creds.client_secret) == ("cid-visible", "csecret-hidden")
    assert creds.redirect_uri == "https://example.test:9"
    assert credentials_from_env(ENV).redirect_uri == "https://localhost:8000"


@pytest.mark.parametrize(
    ("env", "named"),
    [
        ({}, "YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET"),
        ({"YAHOO_CLIENT_ID": "x"}, "YAHOO_CLIENT_SECRET not set"),
        ({"YAHOO_CLIENT_ID": "", "YAHOO_CLIENT_SECRET": "s3cr3t"}, "YAHOO_CLIENT_ID not set"),
    ],
)
def test_missing_credentials_are_named_not_shown(env: dict[str, str], named: str) -> None:
    with pytest.raises(SetupError, match=named) as excinfo:
        credentials_from_env(env)
    assert "s3cr3t" not in str(excinfo.value)
    assert "--env-file .env" in str(excinfo.value)


async def test_token_store_round_trip_is_owner_only(tmp_path: Path) -> None:
    store = JsonFileTokenStore(tmp_path / "private" / "yahoo_token.json")
    assert await store.load() is None
    token = Token("a", "r", NOW)
    await store.save(token)
    assert await store.load() == token
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert list(store.path.parent.iterdir()) == [store.path]  # no temp file left


async def test_token_store_is_owner_only_before_the_token_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileTokenStore(tmp_path / "t.json")
    tmp = tmp_path / "t.json.tmp"
    tmp.write_text("stale")  # a leftover from an interrupted save, readable by others
    tmp.chmod(0o644)
    modes: list[int] = []
    real_dump = json.dump

    def dump(obj: object, f: IO[str]) -> None:
        modes.append(stat.S_IMODE(os.fstat(f.fileno()).st_mode))
        real_dump(obj, f)

    monkeypatch.setattr(json, "dump", dump)
    await store.save(Token("a", "r", NOW))
    assert modes == [0o600]
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert not tmp.exists()


async def test_token_store_rejects_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text('{"access_token": "a"}')
    with pytest.raises(YahooAuthError, match="missing"):
        await JsonFileTokenStore(path).load()


# ---------------------------------------------------------------- consent


class Yahoo:
    """The token endpoint plus the synthetic league's API."""

    def __init__(self, token_status: int = 200) -> None:
        self.league = League()
        self.token_status = token_status
        self.token_requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == oauth.TOKEN_URL:
            self.token_requests.append(request)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            body = {"access_token": "acc-hidden", "refresh_token": "ref-hidden", "expires_in": 3600}
            return httpx.Response(200, json=body)
        return await self.league(request)


def redirect_to(printed: str, code: str = "the-code", state: str | None = None) -> str:
    url = next(w for w in printed.split() if w.startswith(oauth.AUTH_URL))
    sent_state = parse_qs(urlsplit(url).query)["state"][0]
    return f"https://localhost:8000/?code={code}&state={state or sent_state}"


async def run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], yahoo: Yahoo, **kw: object
) -> tuple[int, str, str, JsonFileTokenStore]:
    store = JsonFileTokenStore(tmp_path / "yahoo_token.json")

    def ask(prompt: str) -> str:
        return redirect_to(capsys.readouterr().out, **kw)  # type: ignore[arg-type]

    async with httpx.AsyncClient(transport=httpx.MockTransport(yahoo)) as http:
        code = await yahoo_auth.run(ENV, ask, http, store, clock=lambda: NOW)
    out, err = capsys.readouterr()
    return code, out, err, store


async def test_consent_saves_the_token_and_checks_the_league(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    yahoo = Yahoo()
    code, out, err, store = await run(tmp_path, capsys, yahoo)
    assert (code, err) == (0, "")
    assert await store.load() == Token("acc-hidden", "ref-hidden", NOW + 3600)
    assert "code=the-code" in yahoo.token_requests[0].content.decode()
    assert "Checked: league 465.l.8076, season 2026, 8 teams, week 1" in out
    assert yahoo.league.paths == ["games;game_codes=nhl", "league/465.l.8076/settings"]


async def test_consent_never_prints_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = JsonFileTokenStore(tmp_path / "yahoo_token.json")
    printed: list[str] = []

    def ask(prompt: str) -> str:
        out = capsys.readouterr().out
        printed.append(out + prompt)
        return redirect_to(out)

    async with httpx.AsyncClient(transport=httpx.MockTransport(Yahoo())) as http:
        assert await yahoo_auth.run(ENV, ask, http, store, clock=lambda: NOW) == 0
    out, err = capsys.readouterr()
    everything = "".join(printed) + out + err
    assert "cid-visible" in everything  # the consent URL carries the (public) client ID
    for secret in ("csecret-hidden", "acc-hidden", "ref-hidden"):
        assert secret not in everything


async def test_a_mismatched_state_fails_without_saving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    yahoo = Yahoo()
    code, _, err, store = await run(tmp_path, capsys, yahoo, state="forged")
    assert code == 1
    assert "state doesn't match" in err
    assert await store.load() is None
    assert yahoo.token_requests == []


async def test_a_rejected_code_fails_without_saving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err, store = await run(tmp_path, capsys, Yahoo(token_status=400))
    assert code == 1
    assert "invalid_grant" in err
    assert await store.load() is None


async def test_missing_env_fails_before_anything_is_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def ask(prompt: str) -> str:
        raise AssertionError("should not ask")

    store = JsonFileTokenStore(tmp_path / "t.json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(Yahoo())) as http:
        assert await yahoo_auth.run({}, ask, http, store) == 1
    assert "YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET not set" in capsys.readouterr().err


async def test_a_failing_league_check_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    yahoo = Yahoo()
    yahoo.league.strict = False
    yahoo.league.overrides["games;game_codes=nhl"] = {"games": []}
    code, _, err, store = await run(tmp_path, capsys, yahoo)
    assert code == 1
    assert "no NHL game listed" in err
    assert await store.load() is not None  # consent itself succeeded


async def test_a_403_points_at_the_pending_api_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Forbidden(Yahoo):
        async def __call__(self, request: httpx.Request) -> httpx.Response:
            if str(request.url) == oauth.TOKEN_URL:
                return await super().__call__(request)
            body = {"error": {"description": "This application is not authorized"}}
            return httpx.Response(403, json=body)

    code, _, err, store = await run(tmp_path, capsys, Forbidden())
    assert code == 1
    assert "HTTP 403" in err
    assert "isn't approved yet" in err
    assert "scripts.yahoo_diagnose" in err
    assert await store.load() is not None


async def test_other_failures_carry_no_permissions_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, err, _ = await run(tmp_path, capsys, Yahoo(token_status=400))
    assert "approved" not in err
