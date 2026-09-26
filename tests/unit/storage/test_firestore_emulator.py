"""FirestoreRepository against the Firestore emulator: the shared contract, for real.

The emulator runs outside pytest (DECISIONS, network-policy gap 3):

    firebase emulators:exec --only firestore --project demo-fha "uv run pytest tests/unit/storage"

Without ``FIRESTORE_EMULATOR_HOST`` these tests skip, unless
``FHA_REQUIRE_EMULATOR=1`` (CI), where a missing emulator is a failure.
"""

import os
from collections.abc import AsyncIterator

import httpx
import pytest

from fha.sources.google_auth import emulator_token
from fha.storage.factory import EMULATOR_PROJECT, emulator_host
from fha.storage.firestore import FirestoreRepository
from fha.storage.repository import Repository
from tests.unit.storage.contract import RepositoryContract

pytestmark = pytest.mark.allow_hosts(["127.0.0.1"])

Emulator = tuple[httpx.AsyncClient, str, str]  # client, base URL, project


@pytest.fixture
async def emulator() -> AsyncIterator[Emulator]:
    host = os.environ.get("FIRESTORE_EMULATOR_HOST")
    if not host:
        if os.environ.get("FHA_REQUIRE_EMULATOR") == "1":
            pytest.fail("FHA_REQUIRE_EMULATOR=1 but FIRESTORE_EMULATOR_HOST is not set")
        pytest.skip("no Firestore emulator (FIRESTORE_EMULATOR_HOST unset)")
    base = f"http://{emulator_host(host)}"
    project = os.environ.get("FIRESTORE_PROJECT_ID") or EMULATOR_PROJECT
    async with httpx.AsyncClient(timeout=10.0) as http:
        # A clean database for every test (the emulator's own reset endpoint).
        reset = f"{base}/emulator/v1/projects/{project}/databases/(default)/documents"
        (await http.delete(reset)).raise_for_status()
        yield http, base, project


def repository(emulator: Emulator) -> FirestoreRepository:
    http, base, project = emulator
    return FirestoreRepository(http, project, emulator_token, base_url=base)


class TestFirestoreEmulator(RepositoryContract):
    @pytest.fixture
    def repo(self, emulator: Emulator) -> Repository:
        return repository(emulator)


async def test_the_emulator_stores_ints_as_integer_values(emulator: Emulator) -> None:
    http, base, project = emulator
    await repository(emulator).put("things", "a", {"i": 1, "f": 1.0, "b": True})
    url = f"{base}/v1/projects/{project}/databases/(default)/documents/things/a"
    raw = (await http.get(url, headers={"Authorization": "Bearer owner"})).json()["fields"]
    assert raw["i"] == {"integerValue": "1"}
    assert raw["b"] == {"booleanValue": True}
    assert set(raw["f"]) == {"doubleValue"}


async def test_listing_pages_through_a_large_collection(emulator: Emulator) -> None:
    repo = repository(emulator)
    docs = {f"d{i:03d}": {"i": i} for i in range(450)}  # more than one 300-document page
    await repo.replace_all("things", docs)
    assert await repo.all("things") == docs


async def test_the_stats_cache_round_trips_through_firestore(emulator: Emulator) -> None:
    from fha.services.refresh import Cached, load_cached, save_cached
    from tests.unit.services.snapshots import synthetic_snapshot

    repo = repository(emulator)
    snap = await synthetic_snapshot(available=200)
    await save_cached(repo, Cached(snap, 1_800_000_000.5))
    assert await load_cached(repo) == Cached(snap, 1_800_000_000.5)


async def test_a_smaller_chunked_record_replaces_a_bigger_one_exactly(emulator: Emulator) -> None:
    from fha.storage.chunks import load_chunked, save_chunked

    repo = repository(emulator)
    await save_chunked(repo, "rec", {"v": 1}, {"xs": [{"i": i} for i in range(200)]}, max_bytes=300)
    assert len(await repo.all("rec")) > 5
    await save_chunked(repo, "rec", {"v": 2}, {"xs": [{"i": 0}]}, max_bytes=300)
    assert set(await repo.all("rec")) == {"meta", "xs-0"}
    assert await load_chunked(repo, "rec") == ({"v": 2}, {"xs": [{"i": 0}]})
