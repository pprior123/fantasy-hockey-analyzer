"""The test suite must not reach the network (CLAUDE.md hard rule 2)."""

import socket

import pytest
from pytest_socket import SocketBlockedError


# pytest-socket warns before raising; with filterwarnings=error the warning
# would otherwise pre-empt the SocketBlockedError this test asserts on.
@pytest.mark.filterwarnings("ignore:A test tried to use socket.socket")
def test_inet_sockets_are_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_unix_sockets_are_allowed_for_asyncio() -> None:
    a, b = socket.socketpair(socket.AF_UNIX)
    with a, b:
        a.sendall(b"ok")
        assert b.recv(2) == b"ok"
