"""Cold starts stay light (SPEC §2): the entry points load no heavy optional library."""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

HEAVY = (
    "openpyxl",
    "google.auth",
    "cryptography",
    "rapidfuzz",
    "pandas",
    "numpy",
    "uvicorn",  # a dev dependency: Vercel runs its own server
    "fha.sources.yahoo.demo",
    "fha.sources.demo_salaries",
    "fha.services.demo",
)
# Once the app and its services are built, name matching (rapidfuzz) is in: every
# page matches salaries. The rest still waits for the first request that needs it.
HEAVY_AFTER_BUILD = tuple(m for m in HEAVY if m != "rapidfuzz")


def loaded(code: str, env: dict[str, str] | None = None) -> list[str]:
    """Which ``HEAVY`` modules are in ``sys.modules`` after ``code``, run from the root."""
    script = f"import sys\n{code}\nprint(','.join(m for m in {HEAVY!r} if m in sys.modules))\n"
    out = subprocess.run(  # noqa: S603 - our own interpreter, fixed code
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
        env={"PATH": os.environ.get("PATH", "")} | (env or {}),
    )
    return [m for m in out.stdout.strip().split(",") if m]


def test_importing_the_entry_point_loads_nothing_heavy() -> None:
    assert loaded("import fha.web.main") == []


def test_the_vercel_entry_loads_nothing_heavy_and_builds_nothing() -> None:
    """``app.py`` (vercel.json) is ``fha.web.main.app``, unbuilt: importing it reads no
    environment, so a missing variable can't fail the import (the error page names it)."""
    code = (
        "import app, fha.web.main\n"
        "assert app.app is fha.web.main.app\n"
        "assert app.app._app is None\n"
    )
    assert loaded(code) == []


def test_building_the_production_app_loads_nothing_heavy_but_matching() -> None:
    key = json.dumps({"client_email": "fha@demo-fha.iam.gserviceaccount.com", "private_key": "x"})
    env = {
        "APP_PASSWORD": "pw",
        "SESSION_SECRET": "secret-" * 5,
        "YAHOO_CLIENT_ID": "id",
        "YAHOO_CLIENT_SECRET": "sec",
        "FIRESTORE_PROJECT_ID": "fha-prod",
        "FIRESTORE_SERVICE_ACCOUNT_JSON": key,
        "LEAGUE_SHEET_ID": "sheet-id",
        "VERCEL": "1",
    }
    code = (
        "import os, httpx\n"
        "from fha.web.context import AppContext\n"
        "from fha.web.app import create_app\n"
        "context = AppContext.from_env(os.environ)\n"
        "create_app(context)\n"
        # Building sends nothing; a request would fail here (the subprocess has no
        # network guard, so this transport is it).
        "def refuse(request): raise AssertionError(f'sent {request.url} while building')\n"
        "context.factory(httpx.AsyncClient(transport=httpx.MockTransport(refuse)))\n"
    )
    assert [m for m in loaded(code, env) if m in HEAVY_AFTER_BUILD] == []
