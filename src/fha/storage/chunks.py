"""Records bigger than one document, split into chunk documents (DECISIONS, M3).

A chunked record owns a whole collection: a ``meta`` document plus
``{list}-{i}`` documents holding its long lists. It is written with one
``replace_all``, so a reader never sees half of one write.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from fha.storage.repository import Document, Repository, RepositoryError

META = "meta"
CHUNK_BYTES = 800_000  # per chunk document, under MAX_DOCUMENT_BYTES with room for the key
LIST_NAME = re.compile(r"[a-z][a-z0-9_]*")


class ChunkError(RepositoryError):
    """The collection isn't one complete chunked record."""


def pack(items: Sequence[Any], max_bytes: int = CHUNK_BYTES) -> list[list[Any]]:
    """Consecutive runs of ``items``, each run's JSON under ``max_bytes``."""
    chunks: list[list[Any]] = []
    current: list[Any] = []
    size = 0
    for item in items:
        item_size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode()) + 1
        if item_size > max_bytes:
            raise RepositoryError(f"one item is {item_size} bytes, over the {max_bytes} chunk size")
        if current and size + item_size > max_bytes:
            chunks.append(current)
            current, size = [], 0
        current.append(item)
        size += item_size
    if current:
        chunks.append(current)
    return chunks


async def save_chunked(
    repo: Repository,
    collection: str,
    meta: Document,
    lists: Mapping[str, Sequence[Any]],
    *,
    max_bytes: int = CHUNK_BYTES,
) -> None:
    """Replace ``collection`` with ``meta`` and ``lists`` split into chunks."""
    if "_chunks" in meta:
        raise RepositoryError("meta may not hold a '_chunks' key")
    docs: dict[str, Document] = {}
    counts: dict[str, int] = {}
    for name, items in lists.items():
        if not LIST_NAME.fullmatch(name):
            raise RepositoryError(f"list name {name!r} must be lowercase letters, digits, _")
        chunks = pack(items, max_bytes)
        counts[name] = len(chunks)
        docs.update({f"{name}-{i}": {"items": chunk} for i, chunk in enumerate(chunks)})
    docs[META] = {**meta, "_chunks": counts}
    await repo.replace_all(collection, docs)


async def load_chunked(
    repo: Repository, collection: str
) -> tuple[Document, dict[str, list[Any]]] | None:
    """``(meta, lists)`` as saved, or None for an empty collection.

    Raises ``ChunkError`` if the collection isn't one complete chunked record
    (a missing or extra chunk), rather than returning part of it. A backend
    failure raises the backend's ``RepositoryError``.
    """
    docs = await repo.all(collection)
    if not docs:
        return None
    meta = docs.pop(META, None)
    if meta is None:
        raise ChunkError(f"{collection}: no meta document")
    counts = meta.pop("_chunks", None)
    if not isinstance(counts, dict) or not all(
        isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in counts.values()
    ):
        raise ChunkError(f"{collection}: the meta document has no valid chunk counts")
    lists: dict[str, list[Any]] = {}
    for name, count in counts.items():
        items: list[Any] = []
        for i in range(count):
            chunk = docs.pop(f"{name}-{i}", None)
            if chunk is None or not isinstance(chunk.get("items"), list):
                raise ChunkError(f"{collection}: chunk {name}-{i} is missing")
            items.extend(chunk["items"])
        lists[name] = items
    if docs:
        raise ChunkError(f"{collection}: unexpected documents {sorted(docs)[:3]}")
    return meta, lists
