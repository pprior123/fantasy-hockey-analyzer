"""The Vercel deploy configuration (SPEC §2, §10 M5): entry, limits, what gets uploaded."""

import json
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
# Vercel's zero-config Python entrypoints at the project root (vercel/vercel
# packages/python, PYTHON_ENTRYPOINT_DIRS and the FastAPI preset).
VERCEL_ROOT_ENTRIES = {"app.py", "index.py", "server.py", "main.py", "wsgi.py", "asgi.py"}
HOBBY_MAX_DURATION = 300  # seconds, Fluid compute on the Hobby plan
REFRESH_TARGET = 8  # seconds, SPEC §2


def vercel() -> dict[str, object]:
    config = json.loads((REPO / "vercel.json").read_text())
    assert isinstance(config, dict)
    return config


def test_vercel_runs_one_function_the_lazy_app() -> None:
    import app
    from fha.web import main

    functions = vercel()["functions"]
    assert isinstance(functions, dict)
    [entry] = functions
    assert entry in VERCEL_ROOT_ENTRIES  # a file Vercel detects, so the key applies
    assert entry == "app.py"
    assert (REPO / entry).is_file()
    assert app.app is main.app


def test_the_function_time_limit_fits_hobby_and_the_refresh() -> None:
    functions = vercel()["functions"]
    assert isinstance(functions, dict)
    limit = functions["app.py"]["maxDuration"]
    assert isinstance(limit, int)
    assert 5 * REFRESH_TARGET <= limit <= HOBBY_MAX_DURATION


def test_one_region() -> None:
    regions = vercel()["regions"]
    assert isinstance(regions, list)
    assert len(regions) == 1  # Hobby deploys functions to one region


def test_static_files_stay_behind_the_app() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert pyproject["tool"]["vercel"]["fastapi"]["static"]["cdn"] is False


# ---------------------------------------------------------------- .vercelignore


def allowlist() -> list[str]:
    lines = [ln.strip() for ln in (REPO / ".vercelignore").read_text().splitlines()]
    rules = [ln for ln in lines if ln and not ln.startswith("#")]
    assert rules[0] == "/*", "the file must ignore everything first, then allow"
    allowed = []
    for rule in rules[1:]:
        assert rule.startswith("!/"), f"only root-level allow rules: {rule}"
        assert "*" not in rule, f"no globs in allow rules: {rule}"
        allowed.append(rule.removeprefix("!/").rstrip("/"))
    return allowed


def uploaded(path: str) -> bool:
    """Whether a CLI deploy uploads ``path`` (relative to the root) under the allowlist."""
    return path.split("/")[0] in allowlist()


def test_the_upload_allowlist_names_what_the_build_needs() -> None:
    allowed = allowlist()
    build = ["app.py", "src", "pyproject.toml", "uv.lock", ".python-version", "README.md"]
    assert sorted(allowed) == sorted([*build, "vercel.json"])
    for name in allowed:
        assert (REPO / name).exists(), name
    readme = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["readme"]
    assert readme in allowed  # uv_build reads it


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.example",
        "private/yahoo_token.json",
        "private/fha-prod-1234abcd.json",
        "fha-prod-1234abcd.json",
        "league.xlsx",
        "player-salaries.csv",
        "tests/fixtures/yahoo/league.json",
        "scripts/yahoo_auth.py",
        "docs/SPEC.md",
        "mutants/src/fha/domain/engine.py",
        "coverage.json",
        ".git/config",
        ".vercel/project.json",
        "firebase.json",
    ],
)
def test_a_cli_deploy_never_uploads(path: str) -> None:
    assert not uploaded(path)


def test_the_uploaded_source_holds_no_data_or_keys() -> None:
    """``src/`` is uploaded whole: no sheet, CSV, env file or key may sit in it."""
    for path in (REPO / "src").rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        assert path.suffix not in {".xlsx", ".xls", ".csv", ".env", ".pem"}, path
        assert not path.name.startswith(".env"), path
        if path.suffix == ".json":
            assert "private_key" not in path.read_text(), path
