"""Test-suite network policy (CLAUDE.md hard rule 2), enforced from conftest.

The pytest-socket *plugin* is disabled (``-p no:socket``) because its hooks
leave gaps: sockets open until test setup (so collection-time imports could
connect), opt-outs via the ``enable_socket`` marker, the ``socket_enabled``
fixture or CLI flags, and restrictions removed before fixture teardown. We
use its functions instead and own the lifecycle:

- sockets are blocked from ``pytest_configure`` and never re-enabled, except
- during setup, call and teardown of a test marked ``allow_hosts(...)`` with
  loopback-only hosts (Firestore-emulator tests, SPEC §10 M3). In that window
  ``connect``, ``sendto`` and name resolution are restricted to loopback.
"""

from __future__ import annotations

import socket
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from pytest_socket import (
    SocketBlockedError,
    _remove_restrictions,  # private, but the only way to lift the guard
    disable_socket,
    socket_allow_hosts,
)

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


class _Marker(Protocol):
    args: tuple[object, ...]
    kwargs: dict[str, object]


class _Item(Protocol):
    nodeid: str

    def get_closest_marker(self, name: str) -> _Marker | None: ...


def block() -> None:
    disable_socket(allow_unix_socket=True)


def marker_hosts(marker: _Marker) -> list[str]:
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
            bad = sorted(set(marker_hosts(marker)) - LOOPBACK)
            if bad:
                out.append(f"{item.nodeid}: allow_hosts beyond loopback: {bad}")
    return out


def _host(address: Any) -> str | None:
    return str(address[0]) if isinstance(address, tuple) and address else None


@contextmanager
def loopback_only() -> Iterator[None]:
    """Allow sockets, but only to loopback addresses; re-block on exit."""
    _remove_restrictions()
    real_getaddrinfo = socket.getaddrinfo
    real_sendto = socket.socket.sendto

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and str(host) not in LOOPBACK:
            raise SocketBlockedError(f"A test tried to use socket.getaddrinfo for {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_sendto(inst: socket.socket, data: Any, *args: Any) -> int:
        host = _host(args[-1]) if args else None
        if host is not None and host not in LOOPBACK:
            raise SocketBlockedError(f"A test tried to use socket.sendto to {host!r}")
        return real_sendto(inst, data, *args)

    socket_allow_hosts(sorted(LOOPBACK), allow_unix_socket=True)
    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.sendto = guarded_sendto  # type: ignore[method-assign,assignment]
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo
        socket.socket.sendto = real_sendto  # type: ignore[method-assign]
        _remove_restrictions()
        block()
