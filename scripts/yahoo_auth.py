"""One-time Yahoo OAuth consent (SPEC §4). The owner runs this; agents must not.

    uv run --env-file .env python -m scripts.yahoo_auth
    uv run --env-file .env python -m scripts.yahoo_auth --firestore private/<key>.json

The first saves the token for dev; the second for production, in Firestore.

1. Checks the token store can be read (for Firestore: the key works and the
   service account has access), so a problem shows before consent.
2. Prints the Yahoo authorization URL.
3. You open it, sign in, click Allow. The browser goes to
   https://localhost:8000/?code=... and the page fails to load: that's fine.
   Production uses the same redirect URI: the Yahoo app needs no other.
4. Paste that full URL back here.
5. The script trades the code for a token and saves it:
   - dev: to private/yahoo_token.json (gitignored, mode 0600);
   - ``--firestore KEY``: to the Firestore project named in the service-account
     key (``--project`` overrides it), document secrets/yahoo_token, where
     the app on Vercel reads it. The key file is the one Vercel's
     FIRESTORE_SERVICE_ACCOUNT_JSON holds; keep it under private/.
   It reads the token back, then checks it works by reading the league's
   settings.

Secrets are never printed: not the client secret, not the tokens, not the key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from fha.sources.yahoo import oauth, parse
from fha.sources.yahoo.client import YahooClient, YahooError, YahooHTTPError, make_http_client
from fha.sources.yahoo.oauth import TokenStore, YahooAuthError
from fha.sources.yahoo.source import DEFAULT_LEAGUE_ID
from fha.storage.factory import KEY, PROJECT, repository_from_env
from fha.storage.repository import Repository, RepositoryError
from fha.storage.tokens import COLLECTION, YAHOO_TOKEN, RepositoryTokenStore
from scripts.yahoo_common import JsonFileTokenStore, SetupError, credentials_from_env

FORBIDDEN_HINT = (
    "The token works but Yahoo won't serve fantasy data with it: the app's API\n"
    "access isn't approved yet (docs/DECISIONS.md, \"Yahoo API access: pending\n"
    'approval"). The token is {saved}; check again later with\n'
    "uv run --env-file .env python -m scripts.yahoo_diagnose"
)


@dataclass(frozen=True)
class Target:
    """Where consent saves the token, and how to describe it to the owner."""

    store: TokenStore
    where: str  # "private/yahoo_token.json", or the Firestore project and document
    note: str  # printed after saving
    saved: str  # completes "The token is ..." in the 403 hint


def file_target(store: JsonFileTokenStore) -> Target:
    """Dev: the token file under ``private/``."""
    return Target(
        store, str(store.path), "Never commit or share this file.", f"saved in {store.path}"
    )


def repository_target(repo: Repository, project: str) -> Target:
    """Production: the Repository the app reads (``secrets/yahoo_token``)."""
    return Target(
        RepositoryTokenStore(repo),
        f"Firestore project {project}, {COLLECTION}/{YAHOO_TOKEN}",
        f"Vercel's FIRESTORE_PROJECT_ID must be {project}.",
        f"saved in Firestore project {project}, and the app will use it once access is on",
    )


def firestore_target(key_path: Path, http: httpx.AsyncClient, project: str | None = None) -> Target:
    """The Firestore project of the service-account key at ``key_path`` (or ``project``).

    Only the key and the project reach ``repository_from_env``: a dev
    ``FHA_LOCAL_REPOSITORY`` in ``.env`` can't pick the store."""
    try:
        key_json = key_path.read_text()
    except OSError as e:
        raise SetupError(f"can't read the service-account key {key_path}: {e.strerror}") from None
    try:
        info = json.loads(key_json)
    except ValueError:
        raise SetupError(f"{key_path} is not JSON (a service-account key file is)") from None
    if project is None:
        found = info.get("project_id") if isinstance(info, dict) else None
        if not isinstance(found, str) or not found:
            raise SetupError(f"{key_path} has no project_id: pass --project")
        project = found
    try:
        repo = repository_from_env({PROJECT: project, KEY: key_json}, http)
    except RepositoryError as e:
        raise SetupError(str(e)) from None
    return repository_target(repo, project)


def target_from_args(argv: Sequence[str], http: httpx.AsyncClient) -> Target:
    parser = argparse.ArgumentParser(prog="python -m scripts.yahoo_auth", description=__doc__)
    parser.add_argument(
        "--firestore",
        type=Path,
        metavar="KEY",
        help="save to production Firestore, with this service-account key file",
    )
    parser.add_argument("--project", help="the Firestore project (default: the key's project_id)")
    args = parser.parse_args(argv)
    if args.firestore is None:
        if args.project is not None:
            parser.error("--project needs --firestore")
        return file_target(JsonFileTokenStore())
    return firestore_target(args.firestore, http, args.project)


async def run(
    environ: Mapping[str, str],
    ask: Callable[[str], str],
    http: httpx.AsyncClient,
    target: Target,
    clock: Callable[[], float] = time.time,
    league_id: int = DEFAULT_LEAGUE_ID,
) -> int:
    store = target.store
    try:
        creds = credentials_from_env(environ)
        try:  # before consent: for Firestore, this proves the key and its access
            replacing = await store.load() is not None
        except YahooAuthError:
            replacing = True  # an unreadable token, which consent replaces
        state = oauth.new_state()
        print("1. Open this URL, sign in to Yahoo and click Allow:\n")
        print(f"   {oauth.authorization_url(creds, state)}\n")
        print(f"2. Your browser then goes to {creds.redirect_uri}/?code=...")
        print("   The page won't load. That's expected.\n")
        redirected = ask("3. Paste the full URL from the address bar here: ")
        code = oauth.code_from_redirect(redirected, state)
        token = await oauth.exchange_code(http, creds, code, clock())
        await store.save(token)
        if await store.load() != token:
            raise SetupError(f"the token saved to {target.where} didn't read back the same")
        verb = "Replaced the token in" if replacing else "Saved the token to"
        print(f"\n{verb} {target.where}. {target.note}")

        client = YahooClient(http, creds, store, clock=clock)
        game = parse.parse_games(await client.get("games;game_codes=nhl"))
        league_key = f"{game.game_key}.l.{league_id}"
        settings = parse.parse_league_settings(await client.get(f"league/{league_key}/settings"))
    except (SetupError, YahooAuthError, YahooError, parse.YahooParseError, RepositoryError) as e:
        print(f"\nFailed: {e}", file=sys.stderr)
        if isinstance(e, YahooHTTPError) and e.status == httpx.codes.FORBIDDEN:
            print(FORBIDDEN_HINT.format(saved=target.saved), file=sys.stderr)
        return 1
    print(
        f"Checked: league {settings.league_key}, season {settings.season}, "
        f"{settings.num_teams} teams, week {settings.current_week}. Consent is done."
    )
    return 0


async def _main(argv: Sequence[str]) -> int:
    async with make_http_client() as http:
        try:
            target = target_from_args(argv, http)
        except SetupError as e:
            print(f"Failed: {e}", file=sys.stderr)
            return 1
        return await run(os.environ, input, http, target)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
