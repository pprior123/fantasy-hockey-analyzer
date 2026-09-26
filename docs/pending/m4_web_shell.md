# Pending for DECISIONS / CLAUDE.md (M4 stream W: web shell)

## 2026-09-26 — M4: the web shell (app factory, auth, chrome, PWA, demo mode)

- **Wiring.** `create_app(AppContext)`. The context holds `Settings` and a
  services factory. The factory runs in the app's lifespan, inside the
  running event loop, because the repository, refresh service and token
  providers own httpx clients and `asyncio.Lock`s ("For M4" in the round-1
  entry). Routes reach them through `request.app.state.context.services`
  (the `fha.web.app.services(request)` helper).
  - `AppContext.from_env` wires production: `repository_from_env`, the Yahoo
    client with `RepositoryTokenStore`, `RefreshService`, and the live sheet
    (`LEAGUE_SHEET_ID` with the service account's key) or a downloaded one
    (`LEAGUE_SHEET_XLSX`). Or it wires the demo league.
  - Missing configuration raises `ConfigError`, naming variables only. The
    app then shows it on an error page instead of crashing.
  - `fha.web.main:app` builds lazily on the first ASGI event. Importing it
    reads no environment and loads no heavy library (tested in a
    subprocess).
- **Auth (SPEC §7).**
  - `APP_PASSWORD` is compared with `hmac.compare_digest`.
  - The session is an itsdangerous `URLSafeTimedSerializer` token (salt
    `fha-session-v1`), signed with `SESSION_SECRET`, in the cookie
    `fha_session`: HttpOnly, SameSite=Lax, Secure unless
    `FHA_INSECURE_COOKIES=1` (local http only), for 400 days.
  - Every path except `/login`, `/manifest.webmanifest` and `/static/*`
    redirects to `/login?next=<path?query>`. htmx requests get a 401 with
    `HX-Redirect`. `next` is a relative path only (no open redirect).
  - Throttling is per process: after 5 failed logins in 60 s, logins are
    refused with 429 until the window passes. Serverless instances count
    separately; with one user and a strong password that stops casual
    guessing. The password is never logged (tested).
  - CSRF: SameSite=Lax keeps cross-site POSTs from carrying the cookie, which
    is enough for a single-user app with no GET side effects. Routes that
    change state must be POSTs.
  - API docs routes are off.
- **Chrome.**
  - `base.html` is mobile first (designed at 390 px), with a sticky nav and a
    header slot for the season label and last-refreshed time. The footer
    carries "Fantasy data provided by Yahoo Fantasy", linking to Yahoo
    Fantasy (required attribution).
  - htmx **2.0.4** is vendored at `src/fha/web/static/htmx.min.js`, from
    `https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js`, sha256
    `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`. There
    is no CDN at runtime and no build step.
  - Unexpected errors show a page without details, and log only the
    exception type and path. The `httpx` and `httpcore` loggers are set to
    WARNING, since at INFO they log URLs carrying the sheet ID and project.
- **PWA.**
  - `manifest.webmanifest`: standalone, `start_url /players`, theme
    `#0b3d91`, icons 192 and 512, and an apple-touch-icon at 180.
  - The icons are drawn by `scripts/make_icons.py` (stdlib zlib+struct PNG
    writer; a puck on the brand blue).
  - **No service worker:** installable to the home screen without one, and
    nothing to cache-bust. Offline use isn't a Phase 1 goal.
- **Demo mode (`FHA_DEMO=1`).**
  - `fha.sources.yahoo.demo.demo_snapshot(seed)` is a deterministic
    synthetic league: 8 teams of 27, every slot kind, 300 free agents, both
    seasons under Yahoo's stat IDs, and this and next week's scoreboards.
    The week is 3, so the baseline view applies. Names are made up from
    syllables.
  - Storage is `FHA_LOCAL_REPOSITORY` (inside `private/`) or in memory. The
    league sheet comes from `LEAGUE_SHEET_XLSX` if set.
  - This lets the owner see every screen before Yahoo access exists. Real
    Yahoo data replaces it with no code change.
- **Test client.** Starlette 1.7's `TestClient` asks for `httpx2` and warns
  on httpx (an error under `filterwarnings=error`), so `httpx2` is a dev
  dependency. The app itself still uses httpx.

Alternatives:
- Starlette's `SessionMiddleware` (rejected: it signs a whole session dict,
  where a single fixed claim is simpler to check);
- JWTs (rejected: no need for claims or a key-rotation story);
- a CDN for htmx (rejected: the phone may be offline, and it's one more
  third-party request).

## CLAUDE.md "Commands" addition

```
FHA_DEMO=1 FHA_INSECURE_COOKIES=1 APP_PASSWORD=dev SESSION_SECRET=dev \
  uv run uvicorn fha.web.main:app --reload
                                # the app on http://127.0.0.1:8000 with the demo league
uv run python -m scripts.make_icons   # redraw the PWA icons
```
