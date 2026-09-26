"""``InMemoryRepository``: the Repository for tests (SPEC §3)."""

from __future__ import annotations

import copy
from collections.abc import Mapping

from fha.storage.repository import Document, check_batch, check_document, check_id


class InMemoryRepository:
    """Documents in a dict. Stores and returns copies, so callers can't alias state."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Document]] = {}

    async def get(self, collection: str, doc_id: str) -> Document | None:
        doc = self.collections.get(check_id(collection, "collection"), {}).get(check_id(doc_id))
        return copy.deepcopy(doc)

    async def put(self, collection: str, doc_id: str, doc: Document) -> None:
        stored = check_document(doc)
        self.collections.setdefault(check_id(collection, "collection"), {})[check_id(doc_id)] = (
            stored
        )

    async def delete(self, collection: str, doc_id: str) -> None:
        self.collections.get(check_id(collection, "collection"), {}).pop(check_id(doc_id), None)

    async def all(self, collection: str) -> dict[str, Document]:
        return copy.deepcopy(self.collections.get(check_id(collection, "collection"), {}))

    async def replace_all(self, collection: str, docs: Mapping[str, Document]) -> None:
        self.collections[check_id(collection, "collection")] = check_batch(docs)
