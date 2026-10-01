"""``FirestoreRepository``: the production Repository, over Firestore's REST API (SPEC §2, §3).

httpx rather than google-cloud-firestore (DECISIONS: "Firestore and Google
Sheets over REST"). Documents are encoded as Firestore typed values, and
types round-trip exactly: an int is an ``integerValue`` and never comes back
as a float. The same code talks to the emulator in tests, with the
emulator's fake bearer token.

- By-ID operations name the document in the request body, never the URL:
  ``get`` is a one-document ``:batchGet``, and ``put`` and ``delete`` are
  one-write ``:commit``s. A 1,500-byte ID is legal, but percent-encoded it can
  outgrow a URL (the emulator answers 404).
- ``put`` is an update write without a mask, which replaces the whole
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
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from fha.sources.google_auth import GoogleAuthError, TokenProvider
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
# A field mask matching nothing, to list IDs without bodies: stored documents
# can't have "__" fields (RESERVED_FIELD), and Firestore refuses "__x__" masks.
NO_FIELDS = "__none"


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
        what = f"{collection}/{doc_id}"
        body = {"documents": [self._doc_name(collection, doc_id)]}
        response = await self._request("get", what, f"{self._url}:batchGet", json_body=body)
        try:
            results = response.json()
        except ValueError:
            results = None
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise RepositoryError(f"Firestore get {what}: not one batchGet result")
        if "found" in results[0]:
            return decode_document(results[0]["found"])
        if "missing" in results[0]:
            return None
        raise RepositoryError(f"Firestore get {what}: neither found nor missing")

    async def put(self, collection: str, doc_id: str, doc: Document) -> None:
        fields = encode_fields(check_document(doc))
        write = {"update": {"name": self._doc_name(collection, doc_id), "fields": fields}}
        await self._commit("put", f"{collection}/{doc_id}", [write])

    async def delete(self, collection: str, doc_id: str) -> None:
        write = {"delete": self._doc_name(collection, doc_id)}  # no error if it's missing
        await self._commit("delete", f"{collection}/{doc_id}", [write])

    async def all(self, collection: str) -> dict[str, Document]:
        return await self._list(collection, names_only=False)

    async def _list(self, collection: str, *, names_only: bool) -> dict[str, Document]:
        """Every document by ID; with ``names_only``, their IDs with empty bodies."""
        url = f"{self._url}/{quote(check_id(collection, 'collection'), safe='')}"
        docs: dict[str, Document] = {}
        page_token: str | None = None
        while True:
            params = {"pageSize": str(PAGE_SIZE)}
            if names_only:
                params["mask.fieldPaths"] = NO_FIELDS
            if page_token:
                params["pageToken"] = page_token
            response = await self._request("list", collection, url, method="GET", params=params)
            body = _json(response)
            for raw in body.get("documents", []):
                docs[_doc_id(raw)] = {} if names_only else decode_document(raw)
            page_token = body.get("nextPageToken")
            if not page_token:
                return docs

    async def replace_all(self, collection: str, docs: Mapping[str, Document]) -> None:
        stored = check_batch(docs)
        existing = await self._list(collection, names_only=True)  # IDs only: no bodies
        writes: list[dict[str, Any]] = [
            {"update": {"name": self._doc_name(collection, k), "fields": encode_fields(v)}}
            for k, v in stored.items()
        ]
        writes += [{"delete": self._doc_name(collection, k)} for k in existing if k not in stored]
        if len(writes) > MAX_BATCH:
            raise RepositoryError(
                f"replacing {collection} takes {len(writes)} writes, over {MAX_BATCH}"
            )
        if writes:
            await self._commit("replace", collection, writes)

    # ---------------------------------------------------------------- HTTP

    @property
    def _url(self) -> str:
        return f"{self._base}/v1/{self._database}/documents"

    def _doc_name(self, collection: str, doc_id: str) -> str:
        name = f"{check_id(collection, 'collection')}/{check_id(doc_id)}"
        return f"{self._database}/documents/{name}"

    async def _commit(self, action: str, what: str, writes: list[dict[str, Any]]) -> None:
        """One atomic commit, after checking Firestore's request-size limit."""
        body = {"writes": writes}
        size = len(json.dumps(body, separators=(",", ":")).encode())
        if size > MAX_REQUEST_BYTES:
            raise RepositoryError(f"Firestore {action} {what}: {size}-byte request, over 10 MiB")
        await self._request(action, what, f"{self._url}:commit", json_body=body)

    async def _request(
        self,
        action: str,
        what: str,
        url: str,
        *,
        method: str = "POST",
        params: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        try:
            token = await self._token()
        except GoogleAuthError as e:  # its messages carry no key, token or project
            raise RepositoryError(f"Firestore {action}: {e}", detail=str(e)) from None
        headers = {"Authorization": f"Bearer {token}"}
        query = {**(params or {}), "prettyPrint": "false"}  # compact JSON: less egress
        try:
            response = await self._http.request(
                method, url, params=query, json=json_body, headers=headers
            )
        except httpx.HTTPError as e:
            kind = type(e).__name__
            raise RepositoryError(f"Firestore {action} failed: {kind}", detail=kind) from None
        if response.status_code != httpx.codes.OK:
            status, message = _error(response)
            parts = [p for p in (status, message) if p]
            reason = f" ({': '.join(parts)})" if parts else ""
            code = f"HTTP {response.status_code}"
            raise RepositoryError(
                f"Firestore {action} {what}: {code}{reason}",
                detail=f"{code} {status}" if status else code,
            )
        return response


def _error(response: httpx.Response) -> tuple[str, str]:
    """Google's error ``status`` (e.g. PERMISSION_DENIED) and ``message``, or "".

    The body is ``{"error": {...}}``, or for ``:batchGet`` (a streaming
    method) a one-item list of that. The status is a fixed code word, safe to
    log; the message can name the project."""
    try:
        body: Any = response.json()
    except ValueError:
        return "", ""
    if isinstance(body, list) and len(body) == 1:
        body = body[0]
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return "", ""
    status, message = error.get("status"), error.get("message")
    return (
        status if isinstance(status, str) and _CODE.fullmatch(status) else "",
        message if isinstance(message, str) else "",
    )


_CODE = re.compile(r"[A-Z_]{1,40}")  # google.rpc.Code names: PERMISSION_DENIED, NOT_FOUND


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
