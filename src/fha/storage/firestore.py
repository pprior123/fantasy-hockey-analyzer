"""``FirestoreRepository``: the production Repository, over Firestore's REST API (SPEC §2, §3).

httpx rather than google-cloud-firestore (DECISIONS: "Firestore and Google
Sheets over REST"). Documents are encoded as Firestore typed values, and
types round-trip exactly: an int is an ``integerValue`` and never comes back
as a float. The same code talks to the emulator in tests, with the
emulator's fake bearer token.

- ``put`` is a PATCH without an update mask, which replaces the whole
  document rather than merging.
- ``replace_all`` lists the collection, then sends one atomic ``:commit``:
  updates for the given documents, and deletes for the others. A document
  created by another writer between the list and the commit survives (see
  DECISIONS). The app's collections have one writer at a time: the refresh,
  or an Admin import.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from fha.sources.google_auth import TokenProvider
from fha.storage.repository import (
    MAX_BATCH,
    Document,
    RepositoryError,
    check_batch,
    check_document,
    check_id,
)

PRODUCTION_URL = "https://firestore.googleapis.com"
SCOPE = "https://www.googleapis.com/auth/datastore"
PAGE_SIZE = 300
MAX_REQUEST_BYTES = 10 * 1024 * 1024  # Firestore's limit on one request


class FirestoreRepository:
    def __init__(
        self,
        http: httpx.AsyncClient,
        project_id: str,
        token: TokenProvider,
        *,
        base_url: str = PRODUCTION_URL,
        database: str = "(default)",
    ) -> None:
        check_id(project_id, "project ID")
        self._http = http
        self._token = token
        self._base = base_url.rstrip("/")
        self._database = f"projects/{project_id}/databases/{database}"

    # ---------------------------------------------------------------- Repository

    async def get(self, collection: str, doc_id: str) -> Document | None:
        response = await self._request("GET", self._doc_url(collection, doc_id), ok=(200, 404))
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        return decode_document(_json(response))

    async def put(self, collection: str, doc_id: str, doc: Document) -> None:
        body = {"fields": encode_fields(check_document(doc))}
        await self._request("PATCH", self._doc_url(collection, doc_id), json_body=body)

    async def delete(self, collection: str, doc_id: str) -> None:
        await self._request("DELETE", self._doc_url(collection, doc_id), ok=(200, 404))

    async def all(self, collection: str) -> dict[str, Document]:
        url = f"{self._base}/v1/{self._documents}/{_segment(collection, 'collection')}"
        docs: dict[str, Document] = {}
        page_token: str | None = None
        while True:
            params = {"pageSize": str(PAGE_SIZE)}
            if page_token:
                params["pageToken"] = page_token
            body = _json(await self._request("GET", url, params=params))
            for raw in body.get("documents", []):
                docs[_doc_id(raw)] = decode_document(raw)
            page_token = body.get("nextPageToken")
            if not page_token:
                return docs

    async def replace_all(self, collection: str, docs: Mapping[str, Document]) -> None:
        stored = check_batch(docs)
        existing = await self.all(collection)
        writes: list[dict[str, Any]] = [
            {"update": {"name": self._doc_name(collection, k), "fields": encode_fields(v)}}
            for k, v in stored.items()
        ]
        writes += [{"delete": self._doc_name(collection, k)} for k in existing if k not in stored]
        if len(writes) > MAX_BATCH:
            raise RepositoryError(
                f"replacing {collection} takes {len(writes)} writes, over {MAX_BATCH}"
            )
        body = {"writes": writes}
        size = len(json.dumps(body, separators=(",", ":")).encode())
        if size > MAX_REQUEST_BYTES:
            raise RepositoryError(f"replacing {collection} is a {size}-byte request, over 10 MiB")
        if writes:
            await self._request("POST", f"{self._base}/v1/{self._documents}:commit", json_body=body)

    # ---------------------------------------------------------------- HTTP

    @property
    def _documents(self) -> str:
        return f"{self._database}/documents"

    def _doc_name(self, collection: str, doc_id: str) -> str:
        return f"{self._documents}/{check_id(collection, 'collection')}/{check_id(doc_id)}"

    def _doc_url(self, collection: str, doc_id: str) -> str:
        path = f"{_segment(collection, 'collection')}/{_segment(doc_id, 'document ID')}"
        return f"{self._base}/v1/{self._documents}/{path}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json_body: Any = None,
        ok: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        try:
            response = await self._http.request(
                method, url, params=params, json=json_body, headers=headers
            )
        except httpx.HTTPError as e:
            raise RepositoryError(f"Firestore {method} failed: {type(e).__name__}") from None
        if response.status_code not in ok:
            raise RepositoryError(
                f"Firestore {method} {_short(url)}: HTTP {response.status_code}{_reason(response)}"
            )
        return response


def _segment(value: str, what: str) -> str:
    return quote(check_id(value, what), safe="")


def _short(url: str) -> str:
    """The URL from ``documents`` on: which document, without host or project."""
    _, sep, rest = url.partition("/documents")
    return f"documents{rest}" if sep else url


def _reason(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return ""
    if not isinstance(error, dict):
        return ""
    parts = [str(error[k]) for k in ("status", "message") if isinstance(error.get(k), str)]
    return f" ({': '.join(parts)})" if parts else ""


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise RepositoryError(
            f"Firestore answered HTTP {response.status_code} without JSON"
        ) from None
    if not isinstance(body, dict):
        raise RepositoryError("Firestore answered with JSON that is not an object")
    return body


def _doc_id(raw: Any) -> str:
    name = raw.get("name") if isinstance(raw, dict) else None
    if not isinstance(name, str) or "/" not in name:
        raise RepositoryError("Firestore listed a document without a name")
    return name.rsplit("/", 1)[1]


# ---------------------------------------------------------------- typed values


def encode_fields(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {key: encode_value(value) for key, value in doc.items()}


def encode_value(value: Any) -> dict[str, Any]:
    """A checked JSON value (``check_document``) as a Firestore typed value."""
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [encode_value(v) for v in value]} if value else {}}
    if isinstance(value, dict):
        return {"mapValue": {"fields": encode_fields(value)} if value else {}}
    raise RepositoryError(f"{type(value).__name__} is not a JSON value")


def decode_document(raw: Any) -> Document:
    fields = raw.get("fields", {}) if isinstance(raw, dict) else None
    if not isinstance(fields, dict):
        raise RepositoryError("a Firestore document without a fields object")
    return {key: decode_value(value) for key, value in fields.items()}


def decode_value(raw: Any) -> Any:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise RepositoryError(f"not a Firestore value: {_describe(raw)}")
    [(kind, value)] = raw.items()
    decoded = _DECODERS.get(kind, _unsupported)(value)
    if decoded is _UNSUPPORTED:
        raise RepositoryError(f"unsupported Firestore value: {_describe(raw)}")
    return decoded


_UNSUPPORTED = object()


def _unsupported(value: Any) -> Any:
    return _UNSUPPORTED


def _null(value: Any) -> Any:
    return None


def _boolean(value: Any) -> Any:
    return value if isinstance(value, bool) else _UNSUPPORTED


def _integer(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return _UNSUPPORTED
    try:
        return int(value)
    except ValueError:
        return _UNSUPPORTED


def _double(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return _UNSUPPORTED
    try:
        number = float(value)
    except ValueError:
        return _UNSUPPORTED
    return number if math.isfinite(number) else _UNSUPPORTED


def _string(value: Any) -> Any:
    return value if isinstance(value, str) else _UNSUPPORTED


def _array(value: Any) -> Any:
    if not isinstance(value, dict) or not isinstance(value.get("values", []), list):
        return _UNSUPPORTED
    return [decode_value(v) for v in value.get("values", [])]


def _map(value: Any) -> Any:
    return decode_document(value) if isinstance(value, dict) else _UNSUPPORTED


_DECODERS = {
    "nullValue": _null,
    "booleanValue": _boolean,
    "integerValue": _integer,
    "doubleValue": _double,
    "stringValue": _string,
    "arrayValue": _array,
    "mapValue": _map,
}


def _describe(raw: Any) -> str:
    """The value's kind, not its content: stored documents may hold anything."""
    return ", ".join(sorted(raw)) if isinstance(raw, dict) else type(raw).__name__
