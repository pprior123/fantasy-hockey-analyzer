"""The ``Repository`` seam (SPEC §3): a small document store.

Everything the app persists (the stats cache and its timestamp, salaries,
name bindings, aliases, the Yahoo token, config) is a JSON document in a
named collection. The typed records live on top of this, so each backend
(in-memory for tests, a local JSON file for dev, Firestore in production)
implements only these five operations, and all of them must behave alike.
``check_document`` and ``check_id`` hold the rules they share, including
Firestore's limits, so the in-memory and file backends refuse what
Firestore would.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from typing import Any, Protocol

Document = dict[str, Any]  # JSON: None, bool, int, float, str, list, dict with str keys

# Firestore allows 1 MiB per document, measured its way (``firestore_size``);
# this keeps headroom for the document's name (up to ~1.6 KB) and overhead.
MAX_DOCUMENT_BYTES = 900_000
MAX_BATCH = 500  # Firestore's limit on writes in one commit
MAX_DEPTH = 20  # Firestore's limit on nested maps and arrays
INT64 = (-(2**63), 2**63 - 1)
RESERVED_ID = re.compile(r"__.*__")  # Firestore reserves these IDs
# Field names: Firestore reserves __x__; the app also keeps every other "__"
# name out of documents' top-level fields, so the Firestore backend's ID-only
# listing (a field mask of one of them) can never match a stored field.
RESERVED_FIELD = re.compile(r"__.*")


class RepositoryError(Exception):
    """A backend failed, or refused a document or ID.

    The message can name the Firestore project or a document, so logs get
    ``summary`` instead: the type, plus ``detail`` when the backend gave one (an
    HTTP status, Google's status code, a transport error's type), which never
    names the project, a document, a key or a token."""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail

    @property
    def summary(self) -> str:
        name = type(self).__name__
        return f"{name}: {self.detail}" if self.detail else name


class Repository(Protocol):
    async def get(self, collection: str, doc_id: str) -> Document | None:
        """The document, or None if there is none."""
        ...

    async def put(self, collection: str, doc_id: str, doc: Document) -> None:
        """Create or replace the document."""
        ...

    async def delete(self, collection: str, doc_id: str) -> None:
        """Remove the document; no error if there is none."""
        ...

    async def all(self, collection: str) -> dict[str, Document]:
        """Every document in the collection, by ID."""
        ...

    async def replace_all(self, collection: str, docs: Mapping[str, Document]) -> None:
        """Atomically make the collection exactly ``docs`` (at most ``MAX_BATCH``)."""
        ...


def check_id(value: str, what: str = "document ID") -> str:
    """A collection name or document ID Firestore accepts."""
    if not isinstance(value, str) or not value:
        raise RepositoryError(f"{what} must be a non-empty string, got {value!r}")
    if "/" in value or value in (".", "..") or RESERVED_ID.fullmatch(value):
        raise RepositoryError(f"{what} {value!r} is not allowed (no '/', '.', '..', '__x__')")
    if len(value.encode()) > 1500:
        raise RepositoryError(f"{what} is longer than 1500 bytes")
    return value


def check_document(doc: Any) -> Document:
    """A deep copy of ``doc`` if every value is storable; the path of the first that isn't."""
    if not isinstance(doc, dict):
        raise RepositoryError(f"a document must be a dict, got {type(doc).__name__}")
    _check_value(doc, "$", 0)
    size = firestore_size(doc)
    if size > MAX_DOCUMENT_BYTES:
        raise RepositoryError(f"document is {size} bytes, over {MAX_DOCUMENT_BYTES}")
    return copy.deepcopy(doc)


def check_batch(docs: Mapping[str, Document]) -> dict[str, Document]:
    if len(docs) > MAX_BATCH:
        raise RepositoryError(f"{len(docs)} documents in one batch, over {MAX_BATCH}")
    return {check_id(k): check_document(v) for k, v in docs.items()}


def firestore_size(value: Any) -> int:
    """A value's size as Firestore counts it against the 1 MiB document limit:
    a string is its UTF-8 bytes + 1, a number 8, a boolean or null 1, an array
    the sum of its values, and a map the sum of (key bytes + 1 + value)."""
    if value is None or isinstance(value, bool):
        return 1
    if isinstance(value, int | float):
        return 8
    if isinstance(value, str):
        return len(value.encode()) + 1
    if isinstance(value, list):
        return sum(firestore_size(v) for v in value)
    if isinstance(value, dict):
        return sum(len(k.encode()) + 1 + firestore_size(v) for k, v in value.items())
    raise RepositoryError(f"{type(value).__name__} is not a JSON value")


def _check_value(value: Any, path: str, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise RepositoryError(f"{path}: nested deeper than Firestore's {MAX_DEPTH} levels")
    if value is None or isinstance(value, bool | str):
        return
    if isinstance(value, int):
        if not INT64[0] <= value <= INT64[1]:
            raise RepositoryError(f"{path}: integer {value} is outside 64 bits")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RepositoryError(f"{path}: {value} is not a finite number")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            if isinstance(item, list):
                raise RepositoryError(f"{path}[{i}]: Firestore can't store a list in a list")
            _check_value(item, f"{path}[{i}]", depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise RepositoryError(f"{path}: keys must be non-empty strings, got {key!r}")
            # Top-level fields: no "__" names (the ID-only listing's mask). Nested
            # map keys (tab names, row keys): only Firestore's own __x__ rule.
            if (RESERVED_FIELD if depth == 0 else RESERVED_ID).fullmatch(key):
                rule = "starts with __" if depth == 0 else "__x__"
                raise RepositoryError(f"{path}: field name {key!r} is reserved ({rule})")
            _check_value(item, f"{path}.{key}", depth + 1)
        return
    raise RepositoryError(f"{path}: {type(value).__name__} is not a JSON value")
