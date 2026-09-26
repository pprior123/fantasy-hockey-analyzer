"""The behaviour every Repository backend must share (SPEC §3).

Subclass ``RepositoryContract`` as ``Test<Backend>`` and override the ``repo``
fixture: the in-memory, local-JSON and Firestore (emulator) backends all run
these same tests, so the app can't depend on one backend's quirks.
"""

from typing import Any

import pytest

from fha.storage.repository import MAX_BATCH, MAX_DOCUMENT_BYTES, Repository, RepositoryError

EVERY_TYPE: dict[str, Any] = {
    "none": None,
    "yes": True,
    "no": False,
    "zero": 0,
    "one": 1,
    "big": 2**63 - 1,
    "negative": -(2**63),
    "float": 1.0,
    "fraction": 0.02,
    "text": "Stützle, Tim — “quoted”",
    "empty_text": "",
    "list": [1, "a", None, {"k": [True]}],
    "empty_list": [],
    "map": {"nested": {"deeper": 1.5}, "a.b": "dotted key", "with space": 1},
    "empty_map": {},
}


BAD_ID = r"not allowed|non-empty"


def _nested(depth: int) -> dict[str, Any]:
    """A document whose innermost value sits ``depth`` maps down."""
    doc: dict[str, Any] = {"leaf": 1}
    for _ in range(depth - 1):
        doc = {"k": doc}
    return doc


class RepositoryContract:
    @pytest.fixture
    def repo(self) -> Repository:
        raise NotImplementedError("override in the backend's test class")

    async def test_a_missing_document_is_none(self, repo: Repository) -> None:
        assert await repo.get("things", "nope") is None

    async def test_every_json_type_round_trips_with_its_type(self, repo: Repository) -> None:
        await repo.put("things", "a", EVERY_TYPE)
        got = await repo.get("things", "a")
        assert got == EVERY_TYPE
        assert got is not None
        assert {k: type(v) for k, v in got.items()} == {k: type(v) for k, v in EVERY_TYPE.items()}
        assert type(got["map"]["nested"]["deeper"]) is float
        assert type(got["list"][0]) is int

    async def test_put_replaces_rather_than_merges(self, repo: Repository) -> None:
        await repo.put("things", "a", {"x": 1, "y": 2})
        await repo.put("things", "a", {"x": 3})
        assert await repo.get("things", "a") == {"x": 3}

    async def test_stored_documents_are_not_aliased(self, repo: Repository) -> None:
        doc: dict[str, Any] = {"list": [1], "map": {"k": 1}}
        await repo.put("things", "a", doc)
        doc["list"].append(2)
        got = await repo.get("things", "a")
        assert got == {"list": [1], "map": {"k": 1}}
        assert got is not None
        got["map"]["k"] = 99
        assert await repo.get("things", "a") == {"list": [1], "map": {"k": 1}}

    async def test_delete_removes_and_tolerates_a_missing_document(self, repo: Repository) -> None:
        await repo.put("things", "a", {"x": 1})
        await repo.delete("things", "a")
        await repo.delete("things", "a")
        await repo.delete("never", "was")
        assert await repo.get("things", "a") is None

    async def test_all_lists_one_collection(self, repo: Repository) -> None:
        assert await repo.all("things") == {}
        await repo.put("things", "a", {"x": 1})
        await repo.put("things", "Pinecone Club é", {"x": 2})
        await repo.put("other", "a", {"x": 3})
        assert await repo.all("things") == {"a": {"x": 1}, "Pinecone Club é": {"x": 2}}
        assert await repo.all("other") == {"a": {"x": 3}}

    async def test_replace_all_makes_the_collection_exactly_the_given_documents(
        self, repo: Repository
    ) -> None:
        await repo.put("things", "keep", {"v": 1})
        await repo.put("things", "drop", {"v": 2})
        await repo.put("other", "untouched", {"v": 3})
        await repo.replace_all("things", {"keep": {"v": 10}, "new": {"v": 11}})
        assert await repo.all("things") == {"keep": {"v": 10}, "new": {"v": 11}}
        assert await repo.all("other") == {"untouched": {"v": 3}}
        await repo.replace_all("things", {})
        assert await repo.all("things") == {}

    async def test_replace_all_refuses_an_oversized_batch_and_changes_nothing(
        self, repo: Repository
    ) -> None:
        await repo.put("things", "a", {"v": 1})
        too_many = {f"d{i}": {"i": i} for i in range(MAX_BATCH + 1)}
        with pytest.raises(RepositoryError, match=f"over {MAX_BATCH}"):
            await repo.replace_all("things", too_many)
        assert await repo.all("things") == {"a": {"v": 1}}

    async def test_replace_all_with_one_bad_document_changes_nothing(
        self, repo: Repository
    ) -> None:
        await repo.put("things", "a", {"v": 1})
        with pytest.raises(RepositoryError, match=r"\$\.v: nan is not a finite number"):
            await repo.replace_all("things", {"b": {"v": 2}, "c": {"v": float("nan")}})
        assert await repo.all("things") == {"a": {"v": 1}}

    @pytest.mark.parametrize(
        ("doc", "message"),
        [
            ({"v": float("inf")}, r"\$\.v: inf is not a finite number"),
            ({"v": 2**63}, r"\$\.v: integer .* outside 64 bits"),
            ({"v": [[1]]}, r"\$\.v\[0\]: Firestore can't store a list in a list"),
            ({"v": {1: "x"}}, "keys must be non-empty strings"),
            ({"v": {"": "x"}}, "keys must be non-empty strings"),
            ({"v": (1, 2)}, r"\$\.v: tuple is not a JSON value"),
            ({"v": {1, 2}}, r"\$\.v: set is not a JSON value"),
            ({"v": "x" * (MAX_DOCUMENT_BYTES + 1)}, f"over {MAX_DOCUMENT_BYTES}"),
            # Small as JSON, over the limit as Firestore counts it (8 bytes per number).
            ({"v": [0] * (MAX_DOCUMENT_BYTES // 8)}, f"over {MAX_DOCUMENT_BYTES}"),
            ({"v": {"__x__": 1}}, r"field name '__x__' is reserved"),
            ({"__none": 1}, r"field name '__none' is reserved \(starts with __\)"),
            ({"__name__": 1}, r"field name '__name__' is reserved"),
            (_nested(21), "nested deeper than Firestore's 20 levels"),
            ([1], "a document must be a dict, got list"),
        ],
    )
    async def test_unstorable_documents_are_refused(
        self, repo: Repository, doc: Any, message: str
    ) -> None:
        with pytest.raises(RepositoryError, match=message):
            await repo.put("things", "a", doc)
        assert await repo.get("things", "a") is None

    @pytest.mark.parametrize("bad", ["", "a/b", ".", "..", "__x__"])
    async def test_bad_ids_are_refused(self, repo: Repository, bad: str) -> None:
        with pytest.raises(RepositoryError, match=BAD_ID):
            await repo.put("things", bad, {"v": 1})
        with pytest.raises(RepositoryError, match=BAD_ID):
            await repo.put(bad, "a", {"v": 1})
        with pytest.raises(RepositoryError, match=BAD_ID):
            await repo.get(bad, "a")
        with pytest.raises(RepositoryError, match=BAD_ID):
            await repo.replace_all("things", {bad: {"v": 1}})
        assert await repo.all("things") == {}

    async def test_twenty_levels_of_nesting_are_fine(self, repo: Repository) -> None:
        await repo.put("things", "deep", _nested(20))
        assert await repo.get("things", "deep") == _nested(20)

    async def test_an_id_over_1500_bytes_is_refused(self, repo: Repository) -> None:
        with pytest.raises(RepositoryError, match="longer than 1500 bytes"):
            await repo.put("things", "é" * 751, {"v": 1})
        await repo.put("things", "é" * 750, {"v": 1})  # exactly 1500 bytes is fine
        assert list(await repo.all("things")) == ["é" * 750]
