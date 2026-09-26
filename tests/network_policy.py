"""Test-suite network policy (CLAUDE.md hard rule 2), enforced from conftest.

pytest-socket's ``--disable-socket`` only takes effect from test setup, and
any test could opt out with ``@pytest.mark.enable_socket``. These helpers
close both gaps: sockets are blocked from ``pytest_configure`` (so imports at
collection time are covered too), and opting out is rejected. The one
permitted exception is ``@pytest.mark.allow_hosts`` restricted to loopback,
for emulator-backed integration tests (SPEC §10, M3).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


class _Marker(Protocol):
    args: tuple[object, ...]
    kwargs: dict[str, object]


class _Item(Protocol):
    nodeid: str

    def get_closest_marker(self, name: str) -> _Marker | None: ...


def _hosts(marker: _Marker) -> list[str]:
    raw = marker.args[0] if marker.args else marker.kwargs.get("allowed", [])
    if isinstance(raw, str):
        return [h.strip() for h in raw.split(",")]
    return [str(h) for h in raw] if isinstance(raw, list | tuple) else [str(raw)]


def violations(items: Iterable[_Item]) -> list[str]:
    """Describe every test that tries to widen network access beyond loopback."""
    out: list[str] = []
    for item in items:
        if item.get_closest_marker("enable_socket") is not None:
            out.append(f"{item.nodeid}: enable_socket is not allowed")
        marker = item.get_closest_marker("allow_hosts")
        if marker is not None:
            bad = sorted(set(_hosts(marker)) - LOOPBACK)
            if bad:
                out.append(f"{item.nodeid}: allow_hosts beyond loopback: {bad}")
    return out
