"""scripts/yahoo_diagnose.py with Yahoo mocked: statuses only, no secrets or bodies."""

import httpx
import pytest

from fha.sources.yahoo.oauth import Token
from scripts import yahoo_diagnose as diag

ENV = {"YAHOO_CLIENT_ID": "cid", "YAHOO_CLIENT_SECRET": "csecret-hidden"}
NOW = 1_000_000.0


class Store:
    def __init__(self, token: Token | None) -> None:
        self.token = token

    async def load(self) -> Token | None:
        return self.token

    async def save(self, token: Token) -> None:
        self.token = token


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.raw_path.decode()
    if "users;use_login=1" in path:
        return httpx.Response(403, json={"error": {"description": "not authorized"}})
    if "metadata" in path:
        return httpx.Response(200, text="<not json>")
    body = {"fantasy_content": {"game": [{"guid": "SECRET-GUID"}], "time": "1ms"}}
    return httpx.Response(200, json=body)


async def run(capsys: pytest.CaptureFixture[str], token: Token | None, env: dict[str, str] = ENV):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        code = await diag.run(env, http, Store(token), clock=lambda: NOW)
    return code, *capsys.readouterr()


async def test_reports_each_endpoint_without_bodies_or_secrets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = await run(capsys, Token("acc-hidden", "ref-hidden", NOW + 1800))
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "Token: valid for 30 more min."
    assert lines[1] == "200  game/nhl  ok (game, time)"
    assert lines[5].startswith("403  users;use_login=1  Yahoo returned HTTP 403")
    assert "not authorized" in lines[5]
    assert lines[8].startswith("ERR  league/nhl.l.8076/metadata  ")
    assert len(lines) == 1 + len(diag.PATHS)
    for secret in ("acc-hidden", "ref-hidden", "csecret-hidden", "SECRET-GUID"):
        assert secret not in out


async def test_expired_token_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    async def refresh_fails(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(refresh_fails)) as http:
        code = await diag.run(ENV, http, Store(Token("a", "r", NOW - 1)), clock=lambda: NOW)
    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[0] == "Token: expired."
    assert "ERR  game/nhl  Yahoo token request (refresh_token) failed: invalid_grant" in out


async def test_needs_a_token(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = await run(capsys, None)
    assert code == 1
    assert "scripts.yahoo_auth first" in err


async def test_needs_credentials(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = await run(capsys, Token("a", "r", NOW), env={})
    assert code == 1
    assert "not set" in err
