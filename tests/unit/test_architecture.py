"""Structural guards for the layering in SPEC §3."""

import ast
import importlib
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
    "google",
    "httpx",
    "io",
    "openpyxl",
    "os",
    "pathlib",
    "random",
    "requests",
    "shutil",
    "socket",
    "subprocess",
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


def imported_modules(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


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
    src = "import os.path\nfrom httpx import AsyncClient\nfrom . import sibling\n"
    assert imported_modules(src) == {"os.path", "httpx"}


def test_domain_has_no_io_imports() -> None:
    offenders = {
        f"{path.relative_to(DOMAIN_DIR)}: {mod}"
        for path in DOMAIN_DIR.rglob("*.py")
        for mod in imported_modules(path.read_text())
        if is_forbidden(mod)
    }
    assert not offenders, f"domain/ must stay pure; found {sorted(offenders)}"
