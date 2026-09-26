"""Enforce per-package coverage gates (SPEC §9).

pytest-cov only supports a single global threshold, so this reads the JSON
report written by ``coverage json`` and checks line *and* branch coverage for
each top-level subpackage of ``fha`` against ``[tool.fha.coverage-gates]`` in
pyproject.toml.

Modules directly under ``fha/`` are gated as the pseudo-package ``_root``.
A package with no measurable code yet (e.g. only a docstring ``__init__``) is
skipped. Code can never silently escape the gates:

- a package with code but no configured gate fails;
- a source file on disk with code that is missing from the report fails
  (coverage.py omits unimported files in directories without ``__init__.py``);
- a report with no ``fha`` files at all fails;
- a missing package directory fails (rather than silently checking nothing);
- in packages listed in ``[tool.fha] coverage-no-exclusions`` (``domain``), any
  line excluded from measurement (``# pragma: no cover``) fails.

Usage: python scripts/check_coverage.py [coverage.json] [pyproject.toml] [src/fha]
"""

from __future__ import annotations

import ast
import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

ROOT_PACKAGE = "fha"
ROOT_MODULES = "_root"


@dataclass(frozen=True)
class Totals:
    statements: int = 0
    covered_lines: int = 0
    branches: int = 0
    covered_branches: int = 0

    def __add__(self, other: Totals) -> Totals:
        return Totals(
            self.statements + other.statements,
            self.covered_lines + other.covered_lines,
            self.branches + other.branches,
            self.covered_branches + other.covered_branches,
        )

    @property
    def line_pct(self) -> float:
        return 100.0 if self.statements == 0 else 100.0 * self.covered_lines / self.statements

    @property
    def branch_pct(self) -> float:
        return 100.0 if self.branches == 0 else 100.0 * self.covered_branches / self.branches


@dataclass(frozen=True)
class Result:
    package: str
    totals: Totals
    gate: float | None

    @property
    def skipped(self) -> bool:
        return self.totals.statements == 0 and self.totals.branches == 0

    @property
    def passed(self) -> bool:
        if self.skipped:
            return True
        if self.gate is None:
            return False
        return self.totals.line_pct >= self.gate and self.totals.branch_pct >= self.gate


def relative_to_package(path: str) -> tuple[str, ...]:
    """Path components below the ``fha`` package directory."""
    parts = PurePath(path.replace("\\", "/")).parts
    if ROOT_PACKAGE not in parts:
        raise ValueError(f"not inside the {ROOT_PACKAGE!r} package: {path}")
    return parts[parts.index(ROOT_PACKAGE) + 1 :]


def package_of(path: str) -> str:
    """Return the ``fha`` subpackage a file belongs to (``_root`` for top-level modules)."""
    rest = relative_to_package(path)
    return rest[0] if len(rest) > 1 else ROOT_MODULES


def has_code(source: str) -> bool:
    """True if a module has statements other than a leading docstring."""
    body = ast.parse(source).body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return bool(body)


def unmeasured(report: dict[str, Any], package_dir: Path) -> list[str]:
    """Source files with code that the coverage report does not mention."""
    reported = {"/".join(relative_to_package(p)) for p in report["files"]}
    return sorted(
        rel
        for path in package_dir.rglob("*.py")
        if (rel := path.relative_to(package_dir).as_posix()) not in reported
        and has_code(path.read_text())
    )


def excluded_in(report: dict[str, Any], packages: list[str]) -> list[str]:
    """Files in ``packages`` with lines excluded from measurement."""
    return sorted(
        "/".join(relative_to_package(path))
        for path, data in report["files"].items()
        if package_of(path) in packages and data.get("excluded_lines")
    )


def totals_by_package(report: dict[str, Any]) -> dict[str, Totals]:
    out: dict[str, Totals] = {}
    for path, data in report["files"].items():
        pkg = package_of(path)
        s = data["summary"]
        t = Totals(
            s["num_statements"],
            s["covered_lines"],
            s.get("num_branches", 0),
            s.get("covered_branches", 0),
        )
        out[pkg] = out.get(pkg, Totals()) + t
    return out


def evaluate(report: dict[str, Any], gates: dict[str, float]) -> list[Result]:
    totals = totals_by_package(report)
    names = sorted(set(totals) | set(gates))
    return [Result(n, totals.get(n, Totals()), gates.get(n)) for n in names]


def format_result(r: Result) -> str:
    if r.skipped:
        return f"SKIP {r.package:<10} no measurable code yet"
    if r.gate is None:
        return f"FAIL {r.package:<10} has code but no gate in [tool.fha.coverage-gates]"
    status = "PASS" if r.passed else "FAIL"
    return (
        f"{status} {r.package:<10} line {r.totals.line_pct:6.2f}%  "
        f"branch {r.totals.branch_pct:6.2f}%  (gate {r.gate:g}%)"
    )


def main(argv: list[str]) -> int:
    report_path = Path(argv[1] if len(argv) > 1 else "coverage.json")
    pyproject_path = Path(argv[2] if len(argv) > 2 else "pyproject.toml")
    package_dir = Path(argv[3] if len(argv) > 3 else f"src/{ROOT_PACKAGE}")
    if not package_dir.is_dir():
        print(f"FAIL package directory {package_dir} not found; run from the repo root")
        return 1
    report = json.loads(report_path.read_text())
    if not report["files"]:
        print(f"FAIL report contains no {ROOT_PACKAGE} files; was coverage measured?")
        return 1
    config = tomllib.loads(pyproject_path.read_text())["tool"]["fha"]
    gates = {k: float(v) for k, v in config["coverage-gates"].items()}
    results = evaluate(report, gates)
    for r in results:
        print(format_result(r))
    missing = unmeasured(report, package_dir)
    for rel in missing:
        print(f"FAIL {rel} has code but is missing from the coverage report")
    excluded = excluded_in(report, list(config.get("coverage-no-exclusions", [])))
    for rel in excluded:
        print(f"FAIL {rel} excludes lines from coverage (pragma: no cover not allowed here)")
    ok = all(r.passed for r in results) and not missing and not excluded
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
