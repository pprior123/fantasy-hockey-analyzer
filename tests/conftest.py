import pytest
from pytest_socket import disable_socket

from tests.network_policy import violations


def pytest_configure(config: pytest.Config) -> None:
    # Block sockets before collection so module-level code in tests is covered.
    disable_socket(allow_unix_socket=True)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    problems = violations(items)
    if problems:
        raise pytest.UsageError("Network policy violations:\n" + "\n".join(problems))
