import os
from collections.abc import Generator
from contextlib import AbstractContextManager, nullcontext

import pytest

from fha.domain.matcher import clear_caches
from tests.network_policy import (
    BLOCKED,
    block,
    blocked_since,
    emulator_host_ok,
    loopback_only,
    violations,
)

# Configuration no test may see: code reading the environment must never find
# a real project, key, sheet, secret or the owner's dev file (network-policy
# gap 2; every SPEC §8 name). Emulator tests keep FIRESTORE_EMULATOR_HOST only.
EMULATOR_HOST = "FIRESTORE_EMULATOR_HOST"
CLOUD_ENV = (
    EMULATOR_HOST,
    "APP_PASSWORD",
    "FHA_LOCAL_REPOSITORY",
    "FIRESTORE_PROJECT_ID",
    "FIRESTORE_SERVICE_ACCOUNT_JSON",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "LEAGUE_SHEET_ID",
    "LEAGUE_SHEET_XLSX",
    "SESSION_SECRET",
    "VERCEL",
    "YAHOO_CLIENT_ID",
    "YAHOO_CLIENT_SECRET",
    "YAHOO_REDIRECT_URI",
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "allow_hosts(hosts): loopback-only network access for emulator tests",
    )
    # Block sockets before collection so module-level code in tests is covered.
    block()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if BLOCKED:  # e.g. module-level code whose broad except hid the error
        raise pytest.UsageError(f"blocked network access during collection: {BLOCKED[0]}")
    problems = violations(items)
    if problems:
        raise pytest.UsageError("Network policy violations:\n" + "\n".join(problems))


def _network(item: pytest.Item) -> AbstractContextManager[None]:
    return loopback_only() if item.get_closest_marker("allow_hosts") else nullcontext()


def _fail_if_blocked(start: int, phase: str) -> None:
    """Fail the phase on any blocked attempt, even one the code under test caught
    (network-policy gap 4)."""
    attempts = blocked_since(start)
    if attempts:
        pytest.fail(
            f"blocked network access during {phase} (a mock is missing?): {attempts[0]}",
            pytrace=False,
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None, None, None]:
    start = len(BLOCKED)
    try:
        with _network(item):
            result = yield
    except BaseException:
        _fail_if_blocked(start, "setup")  # a blocked attempt beats a skip or a failure
        raise
    _fail_if_blocked(start, "setup")
    return result


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, None, None]:
    start = len(BLOCKED)
    try:
        with _network(item):
            result = yield
    except BaseException:
        _fail_if_blocked(start, "call")  # a blocked attempt beats a skip or a failure
        raise
    _fail_if_blocked(start, "call")
    return result


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None, None, None]:
    start = len(BLOCKED)
    try:
        with _network(item):
            result = yield
    except BaseException:
        _fail_if_blocked(start, "teardown")  # a blocked attempt beats a skip or a failure
        raise
    _fail_if_blocked(start, "teardown")
    return result


@pytest.fixture(autouse=True)
def fresh_name_caches() -> None:
    # The matcher memoizes name functions; a cache shared across tests could hide
    # a mutant (mutmut) or couple tests.
    clear_caches()


@pytest.fixture(autouse=True)
def cloud_env_policy(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    marked = request.node.get_closest_marker("allow_hosts") is not None
    for name in CLOUD_ENV:
        if not (marked and name == EMULATOR_HOST):
            monkeypatch.delenv(name, raising=False)
    if not marked:
        return
    host = os.environ.get(EMULATOR_HOST)
    if host is not None and not emulator_host_ok(host):
        pytest.fail(f"FIRESTORE_EMULATOR_HOST must be loopback, got {host!r}", pytrace=False)
