"""Chunked records: one logical record over several documents, written atomically."""

from typing import Any

import pytest

from fha.storage.chunks import ChunkError, load_chunked, pack, save_chunked
from fha.storage.memory import InMemoryRepository
from fha.storage.repository import RepositoryError


def test_pack_keeps_order_and_splits_at_the_byte_limit() -> None:
    items = ["aaaa", "bbbb", "cccc"]  # Firestore counts each as 4 bytes + 1
    assert pack(items, max_bytes=10) == [["aaaa", "bbbb"], ["cccc"]]
    assert pack(items, max_bytes=15) == [items]
    assert pack(items, max_bytes=5) == [["aaaa"], ["bbbb"], ["cccc"]]
    assert pack([], max_bytes=5) == []


def test_pack_measures_items_as_firestore_does() -> None:
    # 100 integers are 800 bytes to Firestore though their JSON is ~200.
    assert pack([[0] * 100, [0] * 100], max_bytes=1000) == [[[0] * 100], [[0] * 100]]


def test_pack_refuses_an_item_bigger_than_a_chunk() -> None:
    with pytest.raises(RepositoryError, match="one item is 6 bytes, over the 5 chunk size"):
        pack(["aaaaa"], max_bytes=5)


async def test_chunked_record_round_trips() -> None:
    repo = InMemoryRepository()
    lists: dict[str, list[Any]] = {"xs": [{"i": i} for i in range(50)], "empty": []}
    await save_chunked(repo, "rec", {"v": 1}, lists, max_bytes=100)
    docs = await repo.all("rec")
    assert len(docs) > 3  # really split
    assert docs["meta"]["_chunks"] == {"xs": len(docs) - 1, "empty": 0}
    assert await load_chunked(repo, "rec") == ({"v": 1}, lists)


async def test_saving_replaces_the_whole_previous_record() -> None:
    repo = InMemoryRepository()
    await save_chunked(repo, "rec", {"v": 1}, {"xs": list(range(100))}, max_bytes=50)
    await save_chunked(repo, "rec", {"v": 2}, {"xs": [1]}, max_bytes=50)
    assert set(await repo.all("rec")) == {"meta", "xs-0"}
    assert await load_chunked(repo, "rec") == ({"v": 2}, {"xs": [1]})


async def test_an_empty_collection_is_no_record() -> None:
    assert await load_chunked(InMemoryRepository(), "rec") is None


@pytest.mark.parametrize(
    ("docs", "message"),
    [
        ({"xs-0": {"items": [1]}}, "no meta document"),
        ({"meta": {"v": 1}}, "no valid chunk counts"),
        ({"meta": {"_chunks": {"xs": True}}}, "no valid chunk counts"),
        ({"meta": {"_chunks": {"xs": -1}}}, "no valid chunk counts"),
        ({"meta": {"_chunks": {"xs": 2}}, "xs-0": {"items": [1]}}, "chunk xs-1 is missing"),
        ({"meta": {"_chunks": {"xs": 1}}, "xs-0": {"items": "no"}}, "chunk xs-0 is missing"),
        (
            {"meta": {"_chunks": {"xs": 1}}, "xs-0": {"items": [1]}, "xs-1": {"items": [2]}},
            r"unexpected documents \['xs-1'\]",
        ),
    ],
)
async def test_a_damaged_record_is_refused_not_partly_read(
    docs: dict[str, Any], message: str
) -> None:
    repo = InMemoryRepository()
    await repo.replace_all("rec", docs)
    with pytest.raises(ChunkError, match=message):
        await load_chunked(repo, "rec")


@pytest.mark.parametrize("name", ["meta-ish", "Xs", "", "1x"])
async def test_list_names_are_restricted(name: str) -> None:
    with pytest.raises(RepositoryError, match="list name"):
        await save_chunked(InMemoryRepository(), "rec", {}, {name: [1]})


async def test_meta_may_not_use_the_reserved_key() -> None:
    with pytest.raises(RepositoryError, match="_chunks"):
        await save_chunked(InMemoryRepository(), "rec", {"_chunks": 1}, {})
