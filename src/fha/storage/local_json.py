"""``LocalJsonRepository``: the Repository in one local JSON file, for dev only (SPEC §10 M3).

It lets the app run locally against real data before any cloud setup. The
file lives under the gitignored ``private/``. Each operation reads the file
and writes it back whole, and each write atomically replaces it with an
owner-only (0600) file. It refuses to run on Vercel: production has no
persistent filesystem, and its state belongs in Firestore.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fha.storage.repository import (
    Document,
    RepositoryError,
    check_batch,
    check_document,
    check_id,
)

Collections = dict[str, dict[str, Document]]


class LocalJsonRepository:
    def __init__(self, path: Path, environ: Mapping[str, str] = os.environ) -> None:
        if environ.get("VERCEL"):
            raise RepositoryError("LocalJsonRepository is for dev only, not on Vercel")
        self.path = path

    async def get(self, collection: str, doc_id: str) -> Document | None:
        return self._read().get(check_id(collection, "collection"), {}).get(check_id(doc_id))

    async def put(self, collection: str, doc_id: str, doc: Document) -> None:
        stored = check_document(doc)
        name, key = check_id(collection, "collection"), check_id(doc_id)
        data = self._read()
        data.setdefault(name, {})[key] = stored
        self._write(data)

    async def delete(self, collection: str, doc_id: str) -> None:
        name, key = check_id(collection, "collection"), check_id(doc_id)
        data = self._read()
        if key in data.get(name, {}):
            del data[name][key]
            self._write(data)

    async def all(self, collection: str) -> dict[str, Document]:
        return self._read().get(check_id(collection, "collection"), {})

    async def replace_all(self, collection: str, docs: Mapping[str, Document]) -> None:
        name, stored = check_id(collection, "collection"), check_batch(docs)
        data = self._read()
        data[name] = stored
        self._write(data)

    def _read(self) -> Collections:
        if not self.path.exists():
            return {}
        try:
            data: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise RepositoryError(f"{self.path} is not valid JSON: {e}") from None
        if not isinstance(data, dict) or not all(isinstance(c, dict) for c in data.values()):
            raise RepositoryError(f"{self.path} is not a repository file")
        return data

    def _write(self, data: Collections) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.unlink(missing_ok=True)  # a leftover could be readable by others
        # O_EXCL: a new file, so the 0600 mode applies before any byte is written.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
