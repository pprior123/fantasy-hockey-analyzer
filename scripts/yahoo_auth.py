"""One-time Yahoo OAuth consent (SPEC §4). The owner runs this; agents must not.

    uv run --env-file .env python -m scripts.yahoo_auth

1. Prints the Yahoo authorization URL.
2. You open it, sign in, click Allow. The browser goes to
   https://localhost:8000/?code=... and the page fails to load: that's fine.
3. Paste that full URL back here.
4. The script trades the code for a token, saves it to
   private/yahoo_token.json (gitignored, mode 0600) and checks it works by
   reading the league's settings.

Secrets are never printed: not the client secret, not the tokens.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Callable, Mapping

import httpx

from fha.sources.yahoo import oauth, parse
from fha.sources.yahoo.client import YahooClient, YahooError, make_http_client
from fha.sources.yahoo.oauth import YahooAuthError
from fha.sources.yahoo.source import DEFAULT_LEAGUE_ID
from scripts.yahoo_common import JsonFileTokenStore, SetupError, credentials_from_env


async def run(
    environ: Mapping[str, str],
    ask: Callable[[str], str],
    http: httpx.AsyncClient,
    store: JsonFileTokenStore,
    clock: Callable[[], float] = time.time,
    league_id: int = DEFAULT_LEAGUE_ID,
) -> int:
    try:
        creds = credentials_from_env(environ)
        state = oauth.new_state()
        print("1. Open this URL, sign in to Yahoo and click Allow:\n")
        print(f"   {oauth.authorization_url(creds, state)}\n")
        print(f"2. Your browser then goes to {creds.redirect_uri}/?code=...")
        print("   The page won't load. That's expected.\n")
        redirected = ask("3. Paste the full URL from the address bar here: ")
        code = oauth.code_from_redirect(redirected, state)
        token = await oauth.exchange_code(http, creds, code, clock())
        await store.save(token)
        print(f"\nSaved the token to {store.path} (never commit or share this file).")

        client = YahooClient(http, creds, store, clock=clock)
        game = parse.parse_games(await client.get("games;game_codes=nhl"))
        league_key = f"{game.game_key}.l.{league_id}"
        settings = parse.parse_league_settings(await client.get(f"league/{league_key}/settings"))
    except (SetupError, YahooAuthError, YahooError, parse.YahooParseError) as e:
        print(f"\nFailed: {e}", file=sys.stderr)
        return 1
    print(
        f"Checked: league {settings.league_key}, season {settings.season}, "
        f"{settings.num_teams} teams, week {settings.current_week}. Consent is done."
    )
    return 0


async def _main() -> int:
    async with make_http_client() as http:
        return await run(os.environ, input, http, JsonFileTokenStore())


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
