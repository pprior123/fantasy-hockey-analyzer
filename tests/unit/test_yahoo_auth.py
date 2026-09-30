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
from fha.storage.memory import InMemoryRepository
from fha.storage.repository import Document, RepositoryError
from fha.storage.tokens import COLLECTION, YAHOO_TOKEN, RepositoryTokenStore
from scripts import yahoo_auth
from scripts.yahoo_common import TOKEN_PATH, JsonFileTokenStore, SetupError, credentials_from_env
from tests.unit.test_google_auth import key_json
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


async def test_token_store_names_a_non_json_file_without_echoing_it(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text('{"access_token": "acc-old-hidden", "refr')
    with pytest.raises(YahooAuthError, match="not JSON") as excinfo:
        await JsonFileTokenStore(path).load()
    assert "acc-old-hidden" not in str(excinfo.value)


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
        code = await yahoo_auth.run(
            ENV, ask, http, yahoo_auth.file_target(store), clock=lambda: NOW
        )
    out, err = capsys.readouterr()
    return code, out, err, store


async def test_consent_saves_the_token_and_checks_the_league(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    yahoo = Yahoo()
    code, out, err, store = await run(tmp_path, capsys, yahoo)
    assert (code, err) == (0, "")
    assert await store.load() == Token("acc-hidden", "ref-hidden", NOW + 3600)
    assert f"Saved the token to {store.path}" in out
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
        target = yahoo_auth.file_target(store)
        assert await yahoo_auth.run(ENV, ask, http, target, clock=lambda: NOW) == 0
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
        assert await yahoo_auth.run({}, ask, http, yahoo_auth.file_target(store)) == 1
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


async def test_consent_replacing_a_file_token_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await JsonFileTokenStore(tmp_path / "yahoo_token.json").save(Token("old", "old-r", NOW))
    code, out, _, store = await run(tmp_path, capsys, Yahoo())
    assert code == 0
    assert f"Replaced the token in {store.path}" in out
    assert (await store.load()) == Token("acc-hidden", "ref-hidden", NOW + 3600)


@pytest.mark.parametrize(
    "stored",
    ['{"access_token": "a"}', '{"access_token": "acc-old-hidden", "refr'],
    ids=["missing-fields", "truncated-json"],
)
async def test_an_unreadable_file_token_is_replaced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], stored: str
) -> None:
    (tmp_path / "yahoo_token.json").write_text(stored)
    code, out, _, store = await run(tmp_path, capsys, Yahoo())
    assert code == 0
    assert "Replaced the token" in out
    assert (await store.load()) == Token("acc-hidden", "ref-hidden", NOW + 3600)


# ---------------------------------------------------------------- production (Firestore)


async def consent_to(
    repo: InMemoryRepository, capsys: pytest.CaptureFixture[str], yahoo: Yahoo
) -> tuple[int, str, str]:
    def ask(prompt: str) -> str:
        return redirect_to(capsys.readouterr().out)

    target = yahoo_auth.repository_target(repo, "fha-prod")
    async with httpx.AsyncClient(transport=httpx.MockTransport(yahoo)) as http:
        code = await yahoo_auth.run(ENV, ask, http, target, clock=lambda: NOW)
    out, err = capsys.readouterr()
    return code, out, err


async def test_production_consent_saves_the_token_in_the_repository(
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = InMemoryRepository()
    code, out, err = await consent_to(repo, capsys, Yahoo())
    assert (code, err) == (0, "")
    stored = await RepositoryTokenStore(repo).load()
    assert stored == Token("acc-hidden", "ref-hidden", NOW + 3600)
    assert f"Saved the token to Firestore project fha-prod, {COLLECTION}/{YAHOO_TOKEN}" in out
    assert "FIRESTORE_PROJECT_ID must be fha-prod" in out
    # production refreshes with Vercel's Yahoo credentials: they must be these
    assert "YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET" in out
    assert "Checked: league 465.l.8076" in out
    for secret in ("csecret-hidden", "acc-hidden", "ref-hidden"):
        assert secret not in out + err


async def test_production_consent_replacing_a_token_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = InMemoryRepository()
    await RepositoryTokenStore(repo).save(Token("old", "old-r", NOW))
    code, out, _ = await consent_to(repo, capsys, Yahoo())
    assert code == 0
    assert "Replaced the token in Firestore project fha-prod" in out
    assert (await RepositoryTokenStore(repo).load()) == Token(
        "acc-hidden", "ref-hidden", NOW + 3600
    )


async def test_an_unreachable_repository_fails_before_consent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Denied(InMemoryRepository):
        async def get(self, collection: str, doc_id: str) -> Document | None:
            raise RepositoryError("Firestore said 403 (PERMISSION_DENIED)")

    def ask(prompt: str) -> str:
        raise AssertionError("should not ask")

    yahoo = Yahoo()
    target = yahoo_auth.repository_target(Denied(), "fha-prod")
    async with httpx.AsyncClient(transport=httpx.MockTransport(yahoo)) as http:
        assert await yahoo_auth.run(ENV, ask, http, target, clock=lambda: NOW) == 1
    out, err = capsys.readouterr()
    assert "PERMISSION_DENIED" in err
    assert oauth.AUTH_URL not in out  # no consent URL: fix access first
    assert yahoo.token_requests == []


async def test_a_token_that_does_not_read_back_fails(capsys: pytest.CaptureFixture[str]) -> None:
    class Lossy(InMemoryRepository):
        async def put(self, collection: str, doc_id: str, doc: Document) -> None:
            await super().put(collection, doc_id, doc | {"refresh_token": "mangled"})

    code, _, err = await consent_to(Lossy(), capsys, Yahoo())
    assert code == 1
    assert "didn't read back" in err
    for secret in ("acc-hidden", "ref-hidden", "mangled"):
        assert secret not in err


async def test_a_production_403_says_the_token_is_in_firestore(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Forbidden(Yahoo):
        async def __call__(self, request: httpx.Request) -> httpx.Response:
            if str(request.url) == oauth.TOKEN_URL:
                return await super().__call__(request)
            return httpx.Response(403, json={"error": {"description": "not authorized"}})

    repo = InMemoryRepository()
    code, _, err = await consent_to(repo, capsys, Forbidden())
    assert code == 1
    assert "isn't approved yet" in err
    assert "saved in Firestore project fha-prod" in err
    assert "private/yahoo_token.json" not in err
    # diagnose reads the dev token: the hint says why that answers for this one too
    assert "Yahoo approves the app, not each token" in err
    assert await RepositoryTokenStore(repo).load() is not None


def write_key(tmp_path: Path, **fields: object) -> Path:
    key = {"client_email": "fha@fha-prod.iam.gserviceaccount.com", "private_key": "pk-hidden"}
    path = tmp_path / "key.json"
    path.write_text(json.dumps(key | fields))
    return path


async def test_the_firestore_target_takes_the_project_from_the_key(tmp_path: Path) -> None:
    async with httpx.AsyncClient() as http:
        target = yahoo_auth.firestore_target(write_key(tmp_path, project_id="fha-prod"), http)
        assert "Firestore project fha-prod" in target.where
        assert isinstance(target.store, RepositoryTokenStore)
        overridden = yahoo_auth.firestore_target(
            write_key(tmp_path, project_id="fha-prod"), http, project="other-proj"
        )
        assert "Firestore project other-proj" in overridden.where


async def test_the_firestore_target_ignores_the_dev_repository_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.env`` may set ``FHA_LOCAL_REPOSITORY``: only the key decides the store."""
    monkeypatch.setenv("FHA_LOCAL_REPOSITORY", str(tmp_path / "dev.json"))
    seen: list[dict[str, str]] = []
    real = yahoo_auth.repository_from_env

    def spy(environ: dict[str, str], http: httpx.AsyncClient) -> object:
        seen.append(dict(environ))
        return real(environ, http)

    monkeypatch.setattr(yahoo_auth, "repository_from_env", spy)
    async with httpx.AsyncClient() as http:
        yahoo_auth.firestore_target(write_key(tmp_path, project_id="fha-prod"), http)
    assert [sorted(env) for env in seen] == [
        ["FIRESTORE_PROJECT_ID", "FIRESTORE_SERVICE_ACCOUNT_JSON"]
    ]
    assert seen[0]["FIRESTORE_PROJECT_ID"] == "fha-prod"


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda p: p / "missing.json", "can't read the service-account key"),
        (lambda p: (p / "k.json").write_text("pk-hidden {") and p / "k.json", "is not JSON"),
        (lambda p: write_key(p), "has no project_id"),
        (lambda p: write_key(p, project_id=""), "has no project_id"),
        (lambda p: write_key(p, project_id="a/b"), "not allowed"),
        (
            lambda p: (
                (p / "k.json").write_text('{"project_id": "fha-prod", "x": "pk-hidden"}')
                and p / "k.json"
            ),
            "has no client_email or private_key",
        ),
    ],
)
async def test_an_unusable_key_is_named_not_shown(
    tmp_path: Path, setup: object, message: str
) -> None:
    path = setup(tmp_path)  # type: ignore[operator]
    async with httpx.AsyncClient() as http:
        with pytest.raises(SetupError, match=message) as excinfo:
            yahoo_auth.firestore_target(path, http)
    assert "pk-hidden" not in str(excinfo.value)


async def test_the_command_line_picks_the_target(tmp_path: Path) -> None:
    async with httpx.AsyncClient() as http:
        dev = yahoo_auth.target_from_args([], http)
        assert isinstance(dev.store, JsonFileTokenStore)
        assert dev.store.path == TOKEN_PATH
        key = str(write_key(tmp_path, project_id="fha-prod"))
        prod = yahoo_auth.target_from_args(["--firestore", key], http)
        assert isinstance(prod.store, RepositoryTokenStore)
        assert "fha-prod" in prod.where
        other = yahoo_auth.target_from_args(["--firestore", key, "--project", "p2"], http)
        assert "Firestore project p2" in other.where
        with pytest.raises(SystemExit):
            yahoo_auth.target_from_args(["--project", "p2"], http)  # --project needs --firestore


async def test_the_production_target_is_real_firestore(tmp_path: Path) -> None:
    """Not the emulator, not the dev file: the key's project on firestore.googleapis.com."""
    firestore: list[httpx.URL] = []

    def google(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.example.test":  # the key's token_uri
            return httpx.Response(200, json={"access_token": "ya29.t", "expires_in": 3599})
        firestore.append(request.url)
        return httpx.Response(200, json=[{"missing": "x", "readTime": "2026-09-30T00:00:00Z"}])

    path = tmp_path / "key.json"
    path.write_text(key_json(project_id="fha-prod"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
        target = yahoo_auth.firestore_target(path, http)
        assert await target.store.load() is None
    [url] = firestore
    assert (url.scheme, url.host) == ("https", "firestore.googleapis.com")
    assert url.path == "/v1/projects/fha-prod/databases/(default)/documents:batchGet"


async def test_an_unusable_key_fails_the_command_before_consent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("should not ask"))
    assert await yahoo_auth._main(["--firestore", str(tmp_path / "missing.json")]) == 1
    out, err = capsys.readouterr()
    assert "can't read the service-account key" in err
    assert oauth.AUTH_URL not in out
