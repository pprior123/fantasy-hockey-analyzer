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
# Top-level module names that must never be imported from domain code.
FORBIDDEN_IN_DOMAIN = {
    "asyncio",
    "datetime",  # clock access goes through the Clock protocol
    "fastapi",
    "glob",
    "google",
    "http",
    "httpx",
    "io",
    "logging",
    "openpyxl",
    "os",
    "pathlib",
    "random",
    "requests",
    "shutil",
    "socket",
    "sqlite3",
    "ssl",
    "subprocess",
    "sys",
    "tempfile",
    "time",
    "urllib",
    "fha.sources",
    "fha.storage",
    "fha.services",
    "fha.web",
}


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_exists(name: str) -> None:
    module = importlib.import_module(f"fha.{name}")
    assert module.__doc__, f"fha.{name} should document its role"


def test_package_is_typed() -> None:
    assert (Path(fha.__file__).parent / "py.typed").is_file()


# Builtins that do I/O or dynamic imports.
FORBIDDEN_CALLS = {"open", "input", "print", "__import__", "exec", "eval"}


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
    return {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in FORBIDDEN_CALLS
    }


def is_forbidden(module: str) -> bool:
    return any(module == f or module.startswith(f + ".") for f in FORBIDDEN_IN_DOMAIN)


def test_is_forbidden_matches_submodules_but_not_prefixes() -> None:
    assert is_forbidden("os")
    assert is_forbidden("os.path")
    assert is_forbidden("google.cloud.firestore")
    assert is_forbidden("fha.storage.memory")
    assert not is_forbidden("osmosis")
    assert not is_forbidden("fha.domain.engine")
    assert not is_forbidden("math")


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
    assert any(is_forbidden(m) for m in mods) is False  # fha.domain.storage: still domain
    mods = imported_modules("from ... import storage\n", "fha.domain.sub")
    assert any(is_forbidden(m) for m in mods)


def test_package_name_for_modules_and_packages() -> None:
    assert package_name(DOMAIN_DIR / "engine.py") == "fha.domain"
    assert package_name(DOMAIN_DIR / "__init__.py") == "fha.domain"
    assert package_name(DOMAIN_DIR / "sub" / "calc.py") == "fha.domain.sub"


def test_forbidden_calls_detects_builtin_io() -> None:
    src = "def f(p):\n    print(open(p).read())\n    return len(p)\n"
    assert forbidden_calls(src) == {"open", "print"}


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
