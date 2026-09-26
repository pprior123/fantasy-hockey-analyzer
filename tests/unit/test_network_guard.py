"""The test suite must not reach the network (CLAUDE.md hard rule 2)."""

import socket
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from pytest_socket import SocketBlockedError, SocketConnectBlockedError

from tests.network_policy import loopback_only, violations

# Runs at import, i.e. during collection, before any test setup.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    try:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()
        BLOCKED_DURING_COLLECTION = False
    except SocketBlockedError:
        BLOCKED_DURING_COLLECTION = True


def test_inet_sockets_are_blocked_during_collection() -> None:
    assert BLOCKED_DURING_COLLECTION


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


@dataclass
class FakeMarker:
    args: tuple[object, ...] = ()
    kwargs: dict[str, object] = field(default_factory=dict)


@dataclass
class FakeItem:
    nodeid: str
    markers: dict[str, FakeMarker] = field(default_factory=dict)

    def get_closest_marker(self, name: str) -> FakeMarker | None:
        return self.markers.get(name)


def test_enable_socket_marker_is_rejected() -> None:
    item = FakeItem("t::a", {"enable_socket": FakeMarker()})
    assert violations([item]) == ["t::a: enable_socket is not allowed"]


@pytest.mark.parametrize(
    "marker",
    [
        FakeMarker(args=(["127.0.0.1"],)),
        FakeMarker(args=("127.0.0.1,::1",)),
        FakeMarker(kwargs={"allowed": ["localhost"]}),
    ],
)
def test_loopback_allow_hosts_is_permitted(marker: FakeMarker) -> None:
    assert violations([FakeItem("t::b", {"allow_hosts": marker})]) == []


@pytest.mark.parametrize(
    ("marker", "bad"),
    [
        (FakeMarker(args=(["127.0.0.1", "api.login.yahoo.com"],)), "api.login.yahoo.com"),
        (FakeMarker(args=("10.0.0.5",)), "10.0.0.5"),
    ],
)
def test_non_loopback_allow_hosts_is_rejected(marker: FakeMarker, bad: str) -> None:
    [problem] = violations([FakeItem("t::c", {"allow_hosts": marker})])
    assert bad in problem


def test_unmarked_items_pass() -> None:
    assert violations([FakeItem("t::d")]) == []


def _local_server() -> tuple[socket.socket, int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server, server.getsockname()[1]


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
def test_loopback_only_allows_loopback_and_blocks_everything_else() -> None:
    with loopback_only():
        server, port = _local_server()
        with server, socket.create_connection(("127.0.0.1", port)) as client:
            assert client.getpeername()[1] == port
        with pytest.raises(SocketConnectBlockedError):
            socket.socket(socket.AF_INET).connect(("10.255.255.1", 80))
        with pytest.raises(SocketBlockedError):
            socket.getaddrinfo("example.com", 443)
        with (
            pytest.raises(SocketBlockedError),
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp,
        ):
            udp.sendto(b"x", ("10.255.255.1", 53))
    # Re-blocked on exit.
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET)


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_allow_hosts_marker_permits_loopback_regardless_of_order() -> None:
    server, port = _local_server()
    with server, socket.create_connection(("127.0.0.1", port)) as client:
        assert client.getpeername()[1] == port


@pytest.fixture
def socket_state_in_teardown() -> Iterator[list[bool]]:
    seen: list[bool] = []
    yield seen
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            socket.socket(socket.AF_INET).close()
            seen.append(False)
        except SocketBlockedError:
            seen.append(True)
    assert seen == [True], "fixture teardown must not have network access"


def test_fixture_teardown_is_blocked(socket_state_in_teardown: list[bool]) -> None:
    assert socket_state_in_teardown == []
