import json
from pathlib import Path
from typing import Any

import pytest

from scripts.check_coverage import Totals, evaluate, main, package_of

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
        ("src/fha/__init__.py", None),
    ],
)
def test_package_of(path: str, expected: str | None) -> None:
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


def test_root_level_modules_are_not_gated() -> None:
    rep = report({"src/fha/__init__.py": file_entry(10, 0)})
    assert all(r.package != "__init__.py" for r in evaluate(rep, GATES))
    assert all(r.passed for r in evaluate(rep, GATES))


def test_main_exit_code_and_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.fha.coverage-gates]\ndomain = 95\nweb = 80\n")
    cov = tmp_path / "coverage.json"

    cov.write_text(json.dumps(report({"src/fha/domain/a.py": file_entry(100, 96, 4, 4)})))
    assert main(["prog", str(cov), str(pyproject)]) == 0
    out = capsys.readouterr().out
    assert "PASS domain" in out
    assert "SKIP web" in out

    cov.write_text(json.dumps(report({"src/fha/domain/a.py": file_entry(100, 50)})))
    assert main(["prog", str(cov), str(pyproject)]) == 1
    assert "FAIL domain" in capsys.readouterr().out
