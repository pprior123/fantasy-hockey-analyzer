"""Cold starts stay light (SPEC §2): the entry point loads no heavy optional library."""

import subprocess
import sys

HEAVY = (
    "openpyxl",
    "google.auth",
    "cryptography",
    "rapidfuzz",
    "fha.sources.yahoo.demo",
    "fha.sources.demo_salaries",
    "fha.services.demo",
)


def test_importing_the_entry_point_loads_nothing_heavy() -> None:
    code = f"import sys, fha.web.main\nprint(','.join(m for m in {HEAVY!r} if m in sys.modules))\n"
    out = subprocess.run(  # noqa: S603 - our own interpreter, fixed code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""
