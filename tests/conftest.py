from collections.abc import Generator
from contextlib import AbstractContextManager, nullcontext

import pytest

from tests.network_policy import block, loopback_only, violations


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "allow_hosts(hosts): loopback-only network access for emulator tests",
    )
    # Block sockets before collection so module-level code in tests is covered.
    block()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    problems = violations(items)
    if problems:
        raise pytest.UsageError("Network policy violations:\n" + "\n".join(problems))


def _network(item: pytest.Item) -> AbstractContextManager[None]:
    return loopback_only() if item.get_closest_marker("allow_hosts") else nullcontext()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None, None, None]:
    with _network(item):
        return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, None, None]:
    with _network(item):
        return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None, None, None]:
    with _network(item):
        return (yield)
