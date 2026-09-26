import json
from pathlib import Path
from typing import Any

import pytest

from scripts.check_coverage import (
    Totals,
    evaluate,
    excluded_in,
    has_code,
    main,
    package_of,
    pragmas_in,
    unmeasured,
)

GATES = {"domain": 95.0, "web": 80.0}


def file_entry(stmts: int, covered: int, branches: int = 0, covered_br: int = 0) -> dict[str, Any]:
    return {
        "summary": {
            "num_statements": stmts,
            "covered_lines": covered,
            "num_branches": branches,
            "covered_branches": covered_br,
        }
    }


def report(files: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"files": files}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/fha/domain/engine.py", "domain"),
        ("src/fha/domain/sub/deep.py", "domain"),
        ("/abs/site-packages/fha/web/app.py", "web"),
        ("src\\fha\\web\\app.py", "web"),
        ("src/fha/__init__.py", "_root"),
        ("src/fha/config.py", "_root"),
    ],
)
def test_package_of(path: str, expected: str) -> None:
    assert package_of(path) == expected


def test_package_of_rejects_paths_outside_package() -> None:
    with pytest.raises(ValueError, match="not inside"):
        package_of("src/other/mod.py")


def test_totals_percentages() -> None:
    t = Totals(statements=200, covered_lines=190, branches=40, covered_branches=30)
    assert t.line_pct == 95.0
    assert t.branch_pct == 75.0


def test_empty_totals_count_as_fully_covered() -> None:
    assert Totals().line_pct == 100.0
    assert Totals().branch_pct == 100.0


def test_files_in_a_package_are_aggregated() -> None:
    rep = report(
        {
            "src/fha/domain/a.py": file_entry(100, 100, 10, 10),
            "src/fha/domain/b.py": file_entry(100, 80, 10, 5),
        }
    )
    [domain, web] = evaluate(rep, GATES)
    assert domain.totals == Totals(200, 180, 20, 15)
    assert domain.totals.line_pct == 90.0
    assert not domain.passed
    assert web.skipped


def test_passes_when_line_and_branch_meet_gate() -> None:
    rep = report({"src/fha/domain/a.py": file_entry(100, 95, 20, 19)})
    [domain, _] = evaluate(rep, GATES)
    assert domain.passed


def test_fails_when_only_branch_coverage_is_below_gate() -> None:
    rep = report({"src/fha/domain/a.py": file_entry(100, 100, 20, 18)})
    [domain, _] = evaluate(rep, GATES)
    assert domain.totals.branch_pct == 90.0
    assert not domain.passed


def test_package_with_code_but_no_gate_fails() -> None:
    rep = report({"src/fha/newpkg/a.py": file_entry(10, 10)})
    results = {r.package: r for r in evaluate(rep, GATES)}
    assert results["newpkg"].gate is None
    assert not results["newpkg"].passed


def test_package_with_no_code_is_skipped_even_without_gate() -> None:
    rep = report({"src/fha/newpkg/__init__.py": file_entry(0, 0)})
    results = {r.package: r for r in evaluate(rep, GATES)}
    assert results["newpkg"].skipped
    assert results["newpkg"].passed


def test_root_level_modules_are_gated_as_root() -> None:
    rep = report({"src/fha/config.py": file_entry(10, 0, 2, 0)})
    results = {r.package: r for r in evaluate(rep, {**GATES, "_root": 90.0})}
    assert results["_root"].totals == Totals(10, 0, 2, 0)
    assert not results["_root"].passed


def test_root_level_code_without_a_gate_fails() -> None:
    rep = report({"src/fha/config.py": file_entry(10, 10)})
    results = {r.package: r for r in evaluate(rep, GATES)}
    assert results["_root"].gate is None
    assert not results["_root"].passed


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", False),
        ('"""Only a docstring."""\n', False),
        ('"""Doc."""\nX = 1\n', True),
        ("import os\n", True),
        ("X = 1\n", True),
    ],
)
def test_has_code(source: str, expected: bool) -> None:
    assert has_code(source) is expected


def make_package(root: Path, files: dict[str, str]) -> Path:
    pkg = root / "src" / "fha"
    for rel, text in files.items():
        (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
        (pkg / rel).write_text(text)
    return pkg


def test_unmeasured_finds_code_files_missing_from_report(tmp_path: Path) -> None:
    pkg = make_package(
        tmp_path,
        {
            "__init__.py": '"""Root."""\n',
            "domain/__init__.py": '"""Domain."""\n',
            "domain/engine.py": "X = 1\n",
            "newpkg/mod.py": "Y = 2\n",  # no __init__.py: coverage.py never lists it
            "domain/empty.py": '"""Docstring only."""\n',
        },
    )
    rep = report(
        {
            "src/fha/__init__.py": file_entry(0, 0),
            "src/fha/domain/__init__.py": file_entry(0, 0),
            "src/fha/domain/engine.py": file_entry(1, 1),
        }
    )
    assert unmeasured(rep, pkg) == ["newpkg/mod.py"]


def test_main_exit_code_and_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.fha.coverage-gates]\ndomain = 95\nweb = 80\n")
    cov = tmp_path / "coverage.json"
    pkg = make_package(tmp_path, {"domain/a.py": "X = 1\n"})

    cov.write_text(json.dumps(report({"src/fha/domain/a.py": file_entry(100, 96, 4, 4)})))
    assert main(["prog", str(cov), str(pyproject), str(pkg)]) == 0
    out = capsys.readouterr().out
    assert "PASS domain" in out
    assert "SKIP web" in out

    cov.write_text(json.dumps(report({"src/fha/domain/a.py": file_entry(100, 50)})))
    assert main(["prog", str(cov), str(pyproject), str(pkg)]) == 1
    assert "FAIL domain" in capsys.readouterr().out


def test_main_fails_on_unmeasured_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.fha.coverage-gates]\ndomain = 95\n")
    pkg = make_package(tmp_path, {"domain/a.py": "X = 1\n", "domain/sub/b.py": "Y = 2\n"})
    cov = tmp_path / "coverage.json"
    cov.write_text(json.dumps(report({"src/fha/domain/a.py": file_entry(1, 1)})))
    assert main(["prog", str(cov), str(pyproject), str(pkg)]) == 1
    assert "FAIL domain/sub/b.py has code but is missing" in capsys.readouterr().out


def test_main_fails_on_empty_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.fha.coverage-gates]\ndomain = 95\n")
    cov = tmp_path / "coverage.json"
    cov.write_text(json.dumps({"files": {}}))
    assert main(["prog", str(cov), str(pyproject), str(tmp_path)]) == 1
    assert "no fha files" in capsys.readouterr().out


def test_main_fails_when_package_dir_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.fha.coverage-gates]\ndomain = 95\n")
    cov = tmp_path / "coverage.json"
    cov.write_text(json.dumps(report({"src/fha/domain/a.py": file_entry(1, 1)})))
    assert main(["prog", str(cov), str(pyproject), str(tmp_path / "nope")]) == 1
    assert "not found" in capsys.readouterr().out


def test_excluded_in_flags_only_listed_packages() -> None:
    rep = report(
        {
            "src/fha/domain/a.py": {**file_entry(1, 1), "excluded_lines": [3, 4]},
            "src/fha/domain/b.py": {**file_entry(1, 1), "excluded_lines": []},
            "src/fha/web/c.py": {**file_entry(1, 1), "excluded_lines": [7]},
        }
    )
    assert excluded_in(rep, ["domain"]) == ["domain/a.py"]
    assert excluded_in(rep, []) == []


def test_main_fails_on_excluded_lines_in_domain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.fha]\ncoverage-no-exclusions = ["domain"]\n[tool.fha.coverage-gates]\ndomain = 95\n'
    )
    pkg = make_package(tmp_path, {"domain/a.py": "X = 1\n"})
    cov = tmp_path / "coverage.json"
    entry = {**file_entry(0, 0), "excluded_lines": [1, 2]}
    cov.write_text(json.dumps(report({"src/fha/domain/a.py": entry})))
    assert main(["prog", str(cov), str(pyproject), str(pkg)]) == 1
    assert "FAIL domain/a.py excludes lines" in capsys.readouterr().out


def test_pragmas_in_finds_any_pragma_in_listed_packages(tmp_path: Path) -> None:
    pkg = make_package(
        tmp_path,
        {
            "domain/a.py": "if x:  # pragma: no branch\n    y = 1\n",
            "domain/b.py": "z = 2  # PRAGMA: NO COVER\n",
            "domain/c.py": "w = 3  # a pragmatic comment, not a pragma directive\n",
            "web/d.py": "v = 4  # pragma: no cover\n",
        },
    )
    assert pragmas_in(pkg, ["domain"]) == ["domain/a.py:1", "domain/b.py:1"]
    assert pragmas_in(pkg, []) == []


def test_main_fails_on_no_branch_pragma_in_domain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.fha]\ncoverage-no-exclusions = ["domain"]\n[tool.fha.coverage-gates]\ndomain = 95\n'
    )
    pkg = make_package(tmp_path, {"domain/a.py": "if X:  # pragma: no branch\n    Y = 1\n"})
    cov = tmp_path / "coverage.json"
    cov.write_text(json.dumps(report({"src/fha/domain/a.py": file_entry(2, 2, 2, 2)})))
    assert main(["prog", str(cov), str(pyproject), str(pkg)]) == 1
    assert "FAIL domain/a.py:1 has a coverage pragma" in capsys.readouterr().out
