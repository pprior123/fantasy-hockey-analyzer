"""Structural guards for the layering in SPEC §3."""

import ast
import importlib
import importlib.util
from pathlib import Path

import pytest

import fha

SUBPACKAGES = ["domain", "sources", "storage", "services", "web"]
DOMAIN_DIR = Path(fha.__file__).parent / "domain"

# domain/ is pure: no I/O, no network, no Firestore, no clock (CLAUDE.md).
# Allowlist, not denylist: any import not listed here fails, so a new I/O
# library can't slip in. Extend deliberately, with a reason.
ALLOWED_IN_DOMAIN = {
    "__future__",
    "abc",
    "collections",
    "dataclasses",
    "datetime",  # types only; reading the clock is banned via CLOCK_CALLS
    "decimal",
    "enum",
    "fractions",
    "functools",
    "itertools",
    "math",
    "operator",
    "rapidfuzz",  # fuzzy name matching (SPEC §6)
    "re",
    "statistics",
    "typing",
    "unicodedata",
    "fha.domain",
}


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_exists(name: str) -> None:
    module = importlib.import_module(f"fha.{name}")
    assert module.__doc__, f"fha.{name} should document its role"


def test_package_is_typed() -> None:
    assert (Path(fha.__file__).parent / "py.typed").is_file()


# Builtins that do I/O or dynamic imports.
FORBIDDEN_CALLS = {"open", "input", "print", "__import__", "exec", "eval", "breakpoint"}
# Method names that read the clock (datetime.now(), date.today(), ...).
CLOCK_CALLS = {"now", "today", "utcnow", "fromtimestamp", "utcfromtimestamp"}


def package_name(path: Path) -> str:
    """Dotted package containing the module at ``path`` (for resolving relative imports)."""
    rel = path.relative_to(DOMAIN_DIR.parent).with_suffix("").parts
    return ".".join(("fha", *rel[:-1]))


def imported_modules(source: str, package: str) -> set[str]:
    """Absolute names of every module a source file imports, relative imports resolved.

    ``from X import y`` records both ``X`` and ``X.y`` since ``y`` may be a submodule.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def forbidden_calls(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            found.add(node.func.id)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in CLOCK_CALLS:
            found.add(f".{node.func.attr}")
    return found


def is_allowed(module: str) -> bool:
    return any(module == a or module.startswith(a + ".") for a in ALLOWED_IN_DOMAIN)


def is_forbidden(module: str) -> bool:
    return not is_allowed(module)


@pytest.mark.parametrize(
    "module",
    [
        "os",
        "os.path",
        "google.cloud.firestore",
        "fha.storage.memory",
        "fha",
        "yfpy",
        "gspread",
        "dotenv",
        "secrets",
        "pickle",
        "importlib",
        "builtins",
        "scripts.check_coverage",
        "osmosis",
        "mathx",
    ],
)
def test_unlisted_modules_are_forbidden(module: str) -> None:
    assert is_forbidden(module)


@pytest.mark.parametrize(
    "module",
    ["math", "fha.domain", "fha.domain.engine", "collections.abc", "datetime", "rapidfuzz.fuzz"],
)
def test_listed_modules_are_allowed(module: str) -> None:
    assert is_allowed(module)


def test_imported_modules_sees_both_import_forms() -> None:
    src = "import os.path\nfrom httpx import AsyncClient\n"
    assert imported_modules(src, "fha.domain") == {"os.path", "httpx", "httpx.AsyncClient"}


def test_imported_modules_resolves_relative_imports() -> None:
    src = "from . import models\nfrom .. import storage\nfrom ..web.app import make\n"
    assert imported_modules(src, "fha.domain") == {
        "fha.domain",
        "fha.domain.models",
        "fha",
        "fha.storage",
        "fha.web.app",
        "fha.web.app.make",
    }


def test_relative_escape_from_domain_is_forbidden() -> None:
    mods = imported_modules("from ..storage import repo\n", "fha.domain.sub")
    assert not any(is_forbidden(m) for m in mods)  # fha.domain.storage: still domain
    mods = imported_modules("from ... import storage\n", "fha.domain.sub")
    assert any(is_forbidden(m) for m in mods)


def test_builtins_alias_of_open_is_forbidden() -> None:
    mods = imported_modules("from builtins import open as o\n", "fha.domain")
    assert any(is_forbidden(m) for m in mods)


def test_package_name_for_modules_and_packages() -> None:
    assert package_name(DOMAIN_DIR / "engine.py") == "fha.domain"
    assert package_name(DOMAIN_DIR / "__init__.py") == "fha.domain"
    assert package_name(DOMAIN_DIR / "sub" / "calc.py") == "fha.domain.sub"


def test_forbidden_calls_detects_builtin_io() -> None:
    src = "def f(p):\n    print(open(p).read())\n    return len(p)\n"
    assert forbidden_calls(src) == {"open", "print"}


def test_forbidden_calls_detects_clock_reads_but_not_date_types() -> None:
    src = (
        "from datetime import date, datetime\n"
        "def f(d: date) -> date:\n"
        "    return max(d, date.today(), datetime.now().date())\n"
    )
    assert forbidden_calls(src) == {".today", ".now"}
    assert forbidden_calls("from datetime import date\nX = date(2026, 10, 7)\n") == set()


def test_domain_has_no_io_imports_or_calls() -> None:
    offenders: set[str] = set()
    for path in DOMAIN_DIR.rglob("*.py"):
        source = path.read_text()
        rel = path.relative_to(DOMAIN_DIR)
        offenders |= {
            f"{rel}: import {m}"
            for m in imported_modules(source, package_name(path))
            if is_forbidden(m)
        }
        offenders |= {f"{rel}: call {c}()" for c in forbidden_calls(source)}
    assert not offenders, f"domain/ must stay pure; found {sorted(offenders)}"
