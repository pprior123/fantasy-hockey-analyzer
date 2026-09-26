"""Exit every client ``logged_in`` entered, so each lifespan ends with its test."""

from collections.abc import Iterator

import pytest

from tests.unit.web.helpers import OPEN


@pytest.fixture(autouse=True)
def _exit_clients() -> Iterator[None]:
    yield
    while OPEN:
        OPEN.pop().__exit__(None, None, None)
