"""Test-suite network policy (CLAUDE.md hard rule 2), enforced from conftest.

The pytest-socket *plugin* is disabled (``-p no:socket``) because its hooks
leave gaps: sockets open until test setup (so collection-time imports could
connect), opt-outs via the ``enable_socket`` marker, the ``socket_enabled``
fixture or CLI flags, and restrictions removed before fixture teardown. We
use its functions instead and own the lifecycle:

- sockets are blocked from ``pytest_configure`` and never re-enabled, except
- during setup, call and teardown of a test marked ``allow_hosts(...)`` with
  loopback-only hosts (Firestore-emulator tests, SPEC §10 M3). In that window
  ``connect``, ``connect_ex``, ``sendto``, ``sendmsg`` and name resolution
  (``getaddrinfo``, ``gethostbyname(_ex)``, ``gethostbyaddr`` (so ``getfqdn``),
  ``getnameinfo``) are restricted to loopback.

Every blocked attempt is recorded in ``BLOCKED``, and conftest fails the test
that made it even if the code under test caught the exception (a broad
``except`` must not hide a missing mock), or skipped; one made during
collection fails the session. Tests of the guard itself claim their attempts
with ``expect_blocked()``.
"""

from __future__ import annotations

import socket
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from pytest_socket import (
    SocketBlockedError,
    SocketConnectBlockedError,
    _remove_restrictions,  # private, but the only way to lift the guard
    disable_socket,
    socket_allow_hosts,
)

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
BLOCKED: list[str] = []  # every blocked attempt, in order

# The real functions, taken before anything is patched.
_REAL = {
    name: getattr(socket, name)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo")
}
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_SENDTO = socket.socket.sendto
_REAL_SENDMSG = socket.socket.sendmsg


def _record_blocked(cls: type[RuntimeError]) -> None:
    original = cls.__init__

    def init(self: RuntimeError, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        BLOCKED.append(f"{type(self).__name__}: {self}")

    cls.__init__ = init  # type: ignore[method-assign]


_record_blocked(SocketBlockedError)
_record_blocked(SocketConnectBlockedError)


@contextmanager
def expect_blocked() -> Iterator[list[str]]:
    """Claim the blocked attempts made inside: they don't fail the test."""
    start = len(BLOCKED)
    seen: list[str] = []
    try:
        yield seen
    finally:
        seen.extend(BLOCKED[start:])
        del BLOCKED[start:]


def blocked_since(start: int) -> list[str]:
    return BLOCKED[start:]


def emulator_host_ok(value: str) -> bool:
    """``FIRESTORE_EMULATOR_HOST`` (``host:port``) must point at loopback."""
    host, sep, port = value.rpartition(":")
    host = host.removeprefix("[").removesuffix("]")
    return bool(sep) and port.isascii() and port.isdigit() and host in LOOPBACK


class _Marker(Protocol):
    args: tuple[object, ...]
    kwargs: dict[str, object]


class _Item(Protocol):
    nodeid: str

    def get_closest_marker(self, name: str) -> _Marker | None: ...


def _is_loopback(host: Any) -> bool:
    return host is None or str(host) in LOOPBACK


def _guard_resolution() -> None:
    """Name resolution only for loopback (pytest-socket covers two functions of four)."""

    def guard(name: str) -> Any:
        real = _REAL[name]

        def guarded(target: Any, *args: Any, **kwargs: Any) -> Any:
            host = target[0] if name == "getnameinfo" and isinstance(target, tuple) else target
            if not _is_loopback(host):
                raise SocketBlockedError(f"A test tried to use socket.{name} for {host!r}")
            return real(target, *args, **kwargs)

        return guarded

    for name in _REAL:
        setattr(socket, name, guard(name))


def _restore_resolution() -> None:
    for name, real in _REAL.items():
        setattr(socket, name, real)


def block() -> None:
    disable_socket(allow_unix_socket=True)
    _guard_resolution()


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
    _restore_resolution()

    def guarded_connect_ex(inst: socket.socket, address: Any) -> int:
        host = _host(address)
        if host is not None and host not in LOOPBACK:
            raise SocketBlockedError(f"A test tried to use socket.connect_ex to {host!r}")
        return _REAL_CONNECT_EX(inst, address)

    def guarded_sendto(inst: socket.socket, data: Any, *args: Any) -> int:
        host = _host(args[-1]) if args else None
        if host is not None and host not in LOOPBACK:
            raise SocketBlockedError(f"A test tried to use socket.sendto to {host!r}")
        return _REAL_SENDTO(inst, data, *args)

    def guarded_sendmsg(inst: socket.socket, buffers: Any, *args: Any) -> int:
        host = _host(args[2]) if len(args) > 2 else None  # (ancdata, flags, address)
        if host is not None and host not in LOOPBACK:
            raise SocketBlockedError(f"A test tried to use socket.sendmsg to {host!r}")
        return _REAL_SENDMSG(inst, buffers, *args)

    socket_allow_hosts(sorted(LOOPBACK), allow_unix_socket=True)
    _guard_resolution()
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = guarded_sendto  # type: ignore[method-assign,assignment]
    socket.socket.sendmsg = guarded_sendmsg  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect_ex = _REAL_CONNECT_EX  # type: ignore[method-assign]
        socket.socket.sendto = _REAL_SENDTO  # type: ignore[method-assign]
        socket.socket.sendmsg = _REAL_SENDMSG  # type: ignore[method-assign]
        _remove_restrictions()
        _restore_resolution()
        block()
