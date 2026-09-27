"""Repositories that count what they're asked, for write-nothing assertions."""

from typing import Any

from fha.storage.memory import InMemoryRepository


class CountingRepository(InMemoryRepository):
    def __init__(self) -> None:
        super().__init__()
        self.writes = 0
        self.reads = 0

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        self.reads += 1
        return await super().get(collection, doc_id)

    async def put(self, collection: str, doc_id: str, doc: dict[str, Any]) -> None:
        self.writes += 1
        await super().put(collection, doc_id, doc)

    async def replace_all(self, collection: str, docs: Any) -> None:
        self.writes += 1
        await super().replace_all(collection, docs)
