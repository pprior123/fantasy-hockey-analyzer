"""Which Yahoo Fantasy endpoints does the saved token reach? (owner-run)

    uv run --env-file .env python -m scripts.yahoo_diagnose

Calls a handful of endpoints, from public game data to the league itself,
and prints each one's HTTP status and Yahoo's error text. Never prints a
token, a secret or a response body (bodies can hold account details).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Callable, Mapping

import httpx

from fha.sources.yahoo.client import YahooClient, YahooError, YahooHTTPError, make_http_client
from fha.sources.yahoo.oauth import TokenStore, YahooAuthError
from fha.sources.yahoo.source import DEFAULT_LEAGUE_ID
from scripts.yahoo_common import JsonFileTokenStore, SetupError, credentials_from_env

# From "any app can read this" to "this account's league".
PATHS = [
    "game/nhl",
    "games;game_codes=nhl",
    "games;game_keys=nhl",
    "game/nhl/stat_categories",
    "users;use_login=1",
    "users;use_login=1/games;game_codes=nhl",
    "users;use_login=1/games;game_codes=nhl/leagues",
    f"league/nhl.l.{DEFAULT_LEAGUE_ID}/metadata",
]


async def run(
    environ: Mapping[str, str],
    http: httpx.AsyncClient,
    store: TokenStore,
    clock: Callable[[], float] = time.time,
) -> int:
    try:
        creds = credentials_from_env(environ)
        token = await store.load()
    except (SetupError, YahooAuthError) as e:
        print(f"Failed: {e}", file=sys.stderr)
        return 1
    if token is None:
        print("Failed: no token saved; run python -m scripts.yahoo_auth first", file=sys.stderr)
        return 1
    minutes = (token.expires_at - clock()) / 60
    print(f"Token: {'expired' if minutes <= 0 else f'valid for {minutes:.0f} more min'}.")
    client = YahooClient(http, creds, store, clock=clock, max_concurrency=1)
    for path in PATHS:
        try:
            content = await client.get(path)
        except YahooHTTPError as e:
            print(f"{e.status}  {path}  {e}")
        except (YahooError, YahooAuthError) as e:
            print(f"ERR  {path}  {e}")
        else:
            print(f"200  {path}  ok ({', '.join(sorted(content)[:4])})")
    return 0


async def _main() -> int:
    async with make_http_client() as http:
        return await run(os.environ, http, JsonFileTokenStore())


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
