"""The test suite must not reach the network (CLAUDE.md hard rule 2)."""

import socket
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from pytest_socket import SocketBlockedError, SocketConnectBlockedError

from tests.network_policy import (
    BLOCKED,
    emulator_host_ok,
    expect_blocked,
    loopback_only,
    violations,
)

pytest_plugins = ["pytester"]

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
    with expect_blocked() as seen, pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    assert seen == ["SocketBlockedError: A test tried to use socket.socket."]


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
@pytest.mark.parametrize(
    ("call", "name"),
    [
        (lambda: socket.getaddrinfo("example.com", 443), "getaddrinfo"),
        (lambda: socket.gethostbyname("example.com"), "gethostbyname"),
        (lambda: socket.gethostbyname_ex("example.com"), "gethostbyname_ex"),
        (lambda: socket.getnameinfo(("93.184.215.14", 443), 0), "getnameinfo"),
    ],
)
def test_name_resolution_is_blocked(call: Any, name: str) -> None:
    with expect_blocked() as seen, pytest.raises(SocketBlockedError):
        call()
    assert len(seen) == 1
    assert name in seen[0]


def test_loopback_names_still_resolve_while_blocked() -> None:
    assert socket.gethostbyname("localhost") in {"127.0.0.1", "::1"}


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
    far = ("10.255.255.1", 80)
    with expect_blocked() as seen, loopback_only():
        server, port = _local_server()
        with server, socket.create_connection(("127.0.0.1", port)) as client:
            assert client.getpeername()[1] == port
        with server_and_port() as (_, other_port), socket.socket(socket.AF_INET) as probe:
            assert probe.connect_ex(("127.0.0.1", other_port)) == 0
        with pytest.raises(SocketConnectBlockedError):
            socket.socket(socket.AF_INET).connect(far)
        with pytest.raises(SocketBlockedError), socket.socket(socket.AF_INET) as tcp:
            tcp.connect_ex(far)
        for resolve in (
            lambda: socket.getaddrinfo("example.com", 443),
            lambda: socket.gethostbyname("example.com"),
            lambda: socket.gethostbyname_ex("example.com"),
            lambda: socket.getnameinfo(("93.184.215.14", 443), 0),
        ):
            with pytest.raises(SocketBlockedError):
                resolve()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            with pytest.raises(SocketBlockedError):
                udp.sendto(b"x", ("10.255.255.1", 53))
            with pytest.raises(SocketBlockedError):
                udp.sendmsg([b"x"], [], 0, ("10.255.255.1", 53))
    assert len(seen) == 8
    # Re-blocked on exit, resolution included.
    with expect_blocked() as after:
        with pytest.raises(SocketBlockedError):
            socket.socket(socket.AF_INET)
        with pytest.raises(SocketBlockedError):
            socket.gethostbyname_ex("example.com")
    assert len(after) == 2


@contextmanager
def server_and_port() -> Iterator[tuple[socket.socket, int]]:
    server, port = _local_server()
    with server:
        yield server, port


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_allow_hosts_marker_permits_loopback_regardless_of_order() -> None:
    server, port = _local_server()
    with server, socket.create_connection(("127.0.0.1", port)) as client:
        assert client.getpeername()[1] == port


@pytest.fixture
def socket_state_in_teardown() -> Iterator[list[bool]]:
    seen: list[bool] = []
    yield seen
    with warnings.catch_warnings(), expect_blocked():
        warnings.simplefilter("ignore")
        try:
            socket.socket(socket.AF_INET).close()
            seen.append(False)
        except SocketBlockedError:
            seen.append(True)
    assert seen == [True], "fixture teardown must not have network access"


def test_fixture_teardown_is_blocked(socket_state_in_teardown: list[bool]) -> None:
    assert socket_state_in_teardown == []


# ---------------------------------------------------------------- caught attempts fail the test

SWALLOWING_TEST = """
import socket

def test_swallows_the_error():
    try:
        socket.socket(socket.AF_INET)
    except Exception:
        pass  # a broad except, as code under test might have
"""


def inner_session(pytester: pytest.Pytester, test_source: str) -> None:
    """A test session using this suite's conftest (its hooks and fixtures)."""
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")
    pytester.makeconftest("from tests.conftest import *  # noqa: F403")
    pytester.makepyfile(test_source)


def test_a_caught_blocked_attempt_still_fails_the_test(pytester: pytest.Pytester) -> None:
    inner_session(pytester, SWALLOWING_TEST)
    before = len(BLOCKED)
    result = pytester.runpytest_inprocess("-p", "no:socket", "-W", "ignore")
    del BLOCKED[before:]  # the inner session's attempt, claimed here
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*blocked network access during call*socket.socket*"])


# ---------------------------------------------------------------- cloud environment


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("127.0.0.1:8181", True),
        ("localhost:8080", True),
        ("[::1]:8080", True),
        ("firestore.googleapis.com:443", False),
        ("10.0.0.5:8080", False),
        ("127.0.0.1", False),
        ("127.0.0.1:x", False),
    ],
)
def test_emulator_host_must_be_loopback(value: str, ok: bool) -> None:
    assert emulator_host_ok(value) is ok


def test_unmarked_tests_see_no_cloud_configuration() -> None:
    import os

    from tests.conftest import CLOUD_ENV

    assert [n for n in CLOUD_ENV if n in os.environ] == []


CLOUD_ENV_TEST = """
import os
import pytest

@pytest.mark.allow_hosts(["127.0.0.1"])
def test_marked():
    assert os.environ.get("FIRESTORE_EMULATOR_HOST") == "127.0.0.1:8181"
    assert "FIRESTORE_SERVICE_ACCOUNT_JSON" not in os.environ

def test_unmarked():
    assert "FIRESTORE_EMULATOR_HOST" not in os.environ
"""


def test_marked_tests_keep_the_emulator_host_but_no_credentials(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8181")
    monkeypatch.setenv("FIRESTORE_SERVICE_ACCOUNT_JSON", "{}")
    inner_session(pytester, CLOUD_ENV_TEST)
    result = pytester.runpytest_inprocess("-p", "no:socket")
    result.assert_outcomes(passed=2)


def test_a_non_loopback_emulator_host_fails_marked_tests(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "firestore.example.com:443")
    inner_session(pytester, CLOUD_ENV_TEST)
    result = pytester.runpytest_inprocess("-p", "no:socket")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*FIRESTORE_EMULATOR_HOST must be loopback*"])
