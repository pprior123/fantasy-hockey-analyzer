"""Enforce per-package coverage gates (SPEC §9).

pytest-cov only supports a single global threshold, so this reads the JSON
report written by ``coverage json`` and checks line *and* branch coverage for
each top-level subpackage of ``fha`` against ``[tool.fha.coverage-gates]`` in
pyproject.toml.

A package with no measurable code yet (e.g. only a docstring ``__init__``) is
skipped. A subpackage with code but no configured gate is a failure, so new
packages cannot silently escape the gates.

Usage: python scripts/check_coverage.py [coverage.json] [pyproject.toml]
"""

from __future__ import annotations

import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

ROOT_PACKAGE = "fha"


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


def package_of(path: str) -> str | None:
    """Return the ``fha`` subpackage a file belongs to, or None for root-level modules."""
    parts = PurePath(path.replace("\\", "/")).parts
    if ROOT_PACKAGE not in parts:
        raise ValueError(f"not inside the {ROOT_PACKAGE!r} package: {path}")
    rest = parts[parts.index(ROOT_PACKAGE) + 1 :]
    return rest[0] if len(rest) > 1 else None


def totals_by_package(report: dict[str, Any]) -> dict[str, Totals]:
    out: dict[str, Totals] = {}
    for path, data in report["files"].items():
        pkg = package_of(path)
        if pkg is None:
            continue
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
    report = json.loads(report_path.read_text())
    config = tomllib.loads(pyproject_path.read_text())
    gates = {k: float(v) for k, v in config["tool"]["fha"]["coverage-gates"].items()}
    results = evaluate(report, gates)
    for r in results:
        print(format_result(r))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
