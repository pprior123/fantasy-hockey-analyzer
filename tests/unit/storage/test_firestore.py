"""FirestoreRepository against a mocked REST API: encoding, paging, commits, errors."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from fha.storage import firestore as fs
from fha.storage.factory import emulator_host, repository_from_env
from fha.storage.firestore import FirestoreRepository, decode_document, decode_value, encode_value
from fha.storage.local_json import LocalJsonRepository
from fha.storage.repository import RepositoryError
from tests.unit.storage.contract import EVERY_TYPE
from tests.unit.test_google_auth import key_json

BASE = "https://firestore.googleapis.com/v1/projects/p/databases/(default)/documents"
TOKEN = "ya29.secret-token"  # noqa: S105 - a fake, to prove it never leaks


class Server:
    """Answers each request with the next canned response; records what was sent."""

    def __init__(self, *responses: httpx.Response | Callable[[httpx.Request], httpx.Response]):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url).startswith("https://oauth2.example.test/"):
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600})
        answer = self.responses.pop(0)
        return answer(request) if callable(answer) else answer

    def body(self, i: int) -> Any:
        return json.loads(self.requests[i].content)


def ok(body: Any = None) -> httpx.Response:
    return httpx.Response(200, json={} if body is None else body)


def repo(server: Server) -> FirestoreRepository:
    async def token() -> str:
        return TOKEN

    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return FirestoreRepository(http, "p", token)


def raw_doc(doc_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    return {"name": f"projects/p/databases/(default)/documents/things/{doc_id}", "fields": fields}


# ---------------------------------------------------------------- typed values


def test_values_encode_as_firestore_typed_values() -> None:
    assert encode_value(None) == {"nullValue": None}
    assert encode_value(True) == {"booleanValue": True}
    assert encode_value(1) == {"integerValue": "1"}
    assert encode_value(2**63 - 1) == {"integerValue": "9223372036854775807"}
    assert encode_value(1.0) == {"doubleValue": 1.0}
    assert encode_value("x") == {"stringValue": "x"}
    assert encode_value([]) == {"arrayValue": {}}
    assert encode_value({}) == {"mapValue": {}}
    assert encode_value([1, {"a": None}]) == {
        "arrayValue": {
            "values": [
                {"integerValue": "1"},
                {"mapValue": {"fields": {"a": {"nullValue": None}}}},
            ]
        }
    }


def test_every_type_round_trips_through_the_encoding() -> None:
    encoded = {"fields": fs.encode_fields(EVERY_TYPE)}
    decoded = decode_document(json.loads(json.dumps(encoded)))  # as it crosses the wire
    assert decoded == EVERY_TYPE
    assert {k: type(v) for k, v in decoded.items()} == {k: type(v) for k, v in EVERY_TYPE.items()}


def test_encoding_refuses_a_non_json_value() -> None:
    with pytest.raises(RepositoryError, match="tuple is not a JSON value"):
        encode_value((1,))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"doubleValue": 1}, 1.0),  # Firestore may send a whole double as a JSON integer
        ({"doubleValue": "2.5"}, 2.5),
        ({"integerValue": 7}, 7),
        ({"arrayValue": {}}, []),
        ({"mapValue": {}}, {}),
    ],
)
def test_decoding_accepts_firestore_s_wire_variants(raw: Any, expected: Any) -> None:
    value = decode_value(raw)
    assert value == expected
    assert type(value) is type(expected)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"timestampValue": "2026-09-26T00:00:00Z"}, "unsupported Firestore value: timestampValue"),
        ({"integerValue": "1.5"}, "unsupported Firestore value: integerValue"),
        ({"integerValue": True}, "unsupported Firestore value: integerValue"),
        ({"integerValue": None}, "unsupported Firestore value: integerValue"),
        ({"doubleValue": "NaN"}, "unsupported Firestore value: doubleValue"),
        ({"doubleValue": "Infinity"}, "unsupported Firestore value: doubleValue"),
        ({"doubleValue": "abc"}, "unsupported Firestore value: doubleValue"),
        ({"doubleValue": False}, "unsupported Firestore value: doubleValue"),
        ({"booleanValue": 1}, "unsupported Firestore value: booleanValue"),
        ({"stringValue": 1}, "unsupported Firestore value: stringValue"),
        ({"arrayValue": []}, "unsupported Firestore value: arrayValue"),
        ({"arrayValue": {"values": {}}}, "unsupported Firestore value: arrayValue"),
        ({"mapValue": []}, "unsupported Firestore value: mapValue"),
        ({"stringValue": "a", "integerValue": "1"}, "not a Firestore value: integerValue, string"),
        ("secret text", "not a Firestore value: str$"),
    ],
)
def test_decoding_refuses_what_the_app_never_writes(raw: Any, message: str) -> None:
    with pytest.raises(RepositoryError, match=message) as caught:
        decode_value(raw)
    assert "2026-09-26" not in str(caught.value)  # kinds only, never stored content
    assert "secret text" not in str(caught.value)


def test_a_document_without_fields_is_empty_and_bad_fields_are_refused() -> None:
    assert decode_document({"name": "x"}) == {}
    with pytest.raises(RepositoryError, match="without a fields object"):
        decode_document({"fields": []})
    with pytest.raises(RepositoryError, match="without a fields object"):
        decode_document([])


# ---------------------------------------------------------------- requests


NAME = "projects/p/databases/(default)/documents/things"


def found(doc_id: str, fields: dict[str, Any]) -> httpx.Response:
    return ok([{"found": raw_doc(doc_id, fields), "readTime": "2026-09-26T00:00:00Z"}])


def missing(doc_id: str) -> httpx.Response:
    return ok([{"missing": f"{NAME}/{doc_id}", "readTime": "2026-09-26T00:00:00Z"}])


async def test_get_is_a_one_document_batch_get_with_the_bearer_token() -> None:
    server = Server(found("a", {"v": {"integerValue": "3"}}))
    assert await repo(server).get("things", "a") == {"v": 3}
    [request] = server.requests
    assert (request.method, str(request.url)) == ("POST", f"{BASE}:batchGet")
    assert server.body(0) == {"documents": [f"{NAME}/a"]}
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


async def test_a_missing_document_is_none() -> None:
    assert await repo(Server(missing("a"))).get("things", "a") is None


async def test_ids_travel_in_the_body_never_the_url() -> None:
    server = Server(missing("x"), ok(), ok())
    odd = "Pinecone Club é?#%"
    await repo(server).get("things", odd)
    await repo(server).put("things", odd, {})
    await repo(server).delete("things", odd)
    assert all("Pinecone" not in str(r.url) for r in server.requests)
    assert server.body(0) == {"documents": [f"{NAME}/{odd}"]}
    assert server.body(1)["writes"][0]["update"]["name"] == f"{NAME}/{odd}"
    assert server.body(2) == {"writes": [{"delete": f"{NAME}/{odd}"}]}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        ok({"found": {}}),
        ok([]),
        ok([{"found": {}}, {"found": {}}]),
        ok(["x"]),
    ],
)
async def test_get_refuses_anything_but_one_batch_get_result(response: httpx.Response) -> None:
    with pytest.raises(RepositoryError, match=r"^Firestore get things/a: not one batchGet result$"):
        await repo(Server(response)).get("things", "a")


async def test_get_refuses_a_result_neither_found_nor_missing() -> None:
    with pytest.raises(RepositoryError, match="neither found nor missing"):
        await repo(Server(ok([{"readTime": "x"}]))).get("things", "a")


async def test_put_is_one_update_write_with_no_mask_so_it_replaces() -> None:
    server = Server(ok())
    await repo(server).put("things", "a", {"v": 1, "f": 0.5})
    [request] = server.requests
    assert (request.method, str(request.url)) == ("POST", f"{BASE}:commit")
    assert server.body(0) == {
        "writes": [
            {
                "update": {
                    "name": f"{NAME}/a",
                    "fields": {"v": {"integerValue": "1"}, "f": {"doubleValue": 0.5}},
                }
            }
        ]
    }


async def test_put_refuses_a_request_over_10_mib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "MAX_REQUEST_BYTES", 100)
    server = Server()
    with pytest.raises(RepositoryError, match=r"^Firestore put things/a: \d+-byte request"):
        await repo(server).put("things", "a", {"v": "x" * 100})
    assert server.requests == []


async def test_put_checks_the_document_before_sending() -> None:
    server = Server()
    with pytest.raises(RepositoryError, match="not a finite number"):
        await repo(server).put("things", "a", {"v": float("nan")})
    assert server.requests == []


async def test_delete_is_one_delete_write() -> None:
    server = Server(ok())
    await repo(server).delete("things", "a")
    assert (server.requests[0].method, str(server.requests[0].url)) == ("POST", f"{BASE}:commit")
    assert server.body(0) == {"writes": [{"delete": f"{NAME}/a"}]}


async def test_all_follows_page_tokens() -> None:
    server = Server(
        ok(
            {
                "documents": [raw_doc("a", {}), raw_doc("b b", {"v": {"nullValue": None}})],
                "nextPageToken": "page-2",
            }
        ),
        ok({"documents": [raw_doc("c", {"v": {"booleanValue": False}})]}),
    )
    assert await repo(server).all("things") == {"a": {}, "b b": {"v": None}, "c": {"v": False}}
    queries = [parse_qs(urlsplit(str(r.url)).query) for r in server.requests]
    assert queries == [{"pageSize": ["300"]}, {"pageSize": ["300"], "pageToken": ["page-2"]}]


async def test_all_of_an_empty_collection() -> None:
    assert await repo(Server(ok({}))).all("things") == {}


async def test_replace_all_commits_updates_and_deletes_in_one_request() -> None:
    server = Server(ok({"documents": [raw_doc("keep", {}), raw_doc("drop", {})]}), ok())
    await repo(server).replace_all("things", {"keep": {"v": 1}, "new": {"v": 2}})
    listing, commit = server.requests
    assert (listing.method, str(listing.url).split("?")[0]) == ("GET", f"{BASE}/things")
    assert (commit.method, str(commit.url)) == ("POST", f"{BASE}:commit")
    assert server.body(1) == {
        "writes": [
            {"update": {"name": f"{NAME}/keep", "fields": {"v": {"integerValue": "1"}}}},
            {"update": {"name": f"{NAME}/new", "fields": {"v": {"integerValue": "2"}}}},
            {"delete": f"{NAME}/drop"},
        ]
    }


async def test_replacing_an_empty_collection_with_nothing_sends_no_commit() -> None:
    server = Server(ok({}))
    await repo(server).replace_all("things", {})
    assert [r.method for r in server.requests] == ["GET"]


async def test_replace_all_refuses_more_writes_than_one_commit_takes() -> None:
    existing = {"documents": [raw_doc(f"old{i}", {}) for i in range(300)]}
    server = Server(ok(existing))
    with pytest.raises(RepositoryError, match="takes 600 writes, over 500"):
        await repo(server).replace_all("things", {f"new{i}": {} for i in range(300)})
    assert [r.method for r in server.requests] == ["GET"]  # nothing committed


async def test_replace_all_refuses_a_request_over_10_mib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "MAX_REQUEST_BYTES", 100)
    server = Server(ok({}))
    with pytest.raises(RepositoryError, match="-byte request, over 10 MiB"):
        await repo(server).replace_all("things", {"a": {"v": "x" * 100}})
    assert [r.method for r in server.requests] == ["GET"]


# ---------------------------------------------------------------- errors


async def test_an_http_error_names_the_status_and_reason_but_never_the_token() -> None:
    error = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Missing perms."}}
    server = Server(httpx.Response(403, json=error))
    with pytest.raises(RepositoryError) as caught:
        await repo(server).get("things", "a")
    message = str(caught.value)
    assert message == "Firestore get things/a: HTTP 403 (PERMISSION_DENIED: Missing perms.)"
    assert TOKEN not in message


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="<html>oops</html>"),
        httpx.Response(500, json=["x"]),
        httpx.Response(500, json={"error": "flat"}),
        httpx.Response(500, json={"error": {"code": 500}}),
    ],
)
async def test_an_http_error_without_a_readable_reason(response: httpx.Response) -> None:
    with pytest.raises(RepositoryError, match=r"HTTP 500$"):
        await repo(Server(response)).put("things", "a", {})


async def test_a_transport_failure_is_a_repository_error() -> None:
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to firestore.googleapis.com")

    with pytest.raises(RepositoryError, match=r"^Firestore get failed: ConnectError$"):
        await repo(Server(offline)).get("things", "a")


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, text="not json"), "HTTP 200 without JSON"),
        (httpx.Response(200, json=[1]), "not an object"),
        (ok({"documents": [{"fields": {}}]}), "a document without a name"),
        (ok({"documents": [{"name": "nameless"}]}), "a document without a name"),
    ],
)
async def test_malformed_answers_are_errors(response: httpx.Response, message: str) -> None:
    with pytest.raises(RepositoryError, match=message):
        await repo(Server(response)).all("things")


def test_the_project_id_is_checked() -> None:
    async def token() -> str:
        return TOKEN

    with pytest.raises(RepositoryError, match="project ID 'a/b' is not allowed"):
        FirestoreRepository(httpx.AsyncClient(), "a/b", token)


# ---------------------------------------------------------------- factory


def mock_http(server: Server) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(server))


async def test_the_emulator_host_selects_the_emulator_with_its_fake_token() -> None:
    server = Server(missing("a"))
    env = {"FIRESTORE_EMULATOR_HOST": "127.0.0.1:8181", "FIRESTORE_SERVICE_ACCOUNT_JSON": "{}"}
    assert await repository_from_env(env, mock_http(server)).get("things", "a") is None
    [request] = server.requests
    assert str(request.url) == (
        "http://127.0.0.1:8181/v1/projects/demo-fha/databases/(default)/documents:batchGet"
    )
    assert request.headers["Authorization"] == "Bearer owner"


async def test_the_emulator_uses_the_configured_project_if_any() -> None:
    server = Server(missing("a"))
    env = {"FIRESTORE_EMULATOR_HOST": "[::1]:8181", "FIRESTORE_PROJECT_ID": "demo-other"}
    await repository_from_env(env, mock_http(server)).get("things", "a")
    assert "/projects/demo-other/" in str(server.requests[0].url)


@pytest.mark.parametrize("host", ["firestore.example.com:443", "10.0.0.5:8181", "127.0.0.1"])
def test_a_non_loopback_emulator_host_is_refused(host: str) -> None:
    with pytest.raises(
        RepositoryError, match=r"^FIRESTORE_EMULATOR_HOST must be a loopback host:port$"
    ):
        emulator_host(host)


async def test_project_and_key_select_production_firestore() -> None:
    server = Server(missing("a"))
    env = {"FIRESTORE_PROJECT_ID": "fha-prod", "FIRESTORE_SERVICE_ACCOUNT_JSON": key_json()}
    await repository_from_env(env, mock_http(server)).get("things", "a")
    token_request, get = server.requests
    assert str(token_request.url) == "https://oauth2.example.test/token"
    assert "scope" not in parse_qs(token_request.content.decode())  # it's inside the JWT
    assert str(get.url) == (
        "https://firestore.googleapis.com/v1/projects/fha-prod/databases/(default)/documents:batchGet"
    )
    assert get.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"FIRESTORE_PROJECT_ID": "p"}, "^FIRESTORE_SERVICE_ACCOUNT_JSON is not set"),
        ({"FIRESTORE_SERVICE_ACCOUNT_JSON": "{hidden"}, "^FIRESTORE_PROJECT_ID is not set"),
        (
            {"FIRESTORE_PROJECT_ID": "p", "FIRESTORE_SERVICE_ACCOUNT_JSON": "{hidden"},
            "^FIRESTORE_SERVICE_ACCOUNT_JSON: the service-account key is not valid JSON$",
        ),
        ({}, "^no repository configured: set FIRESTORE_PROJECT_ID and"),
    ],
)
def test_incomplete_configuration_names_variables_not_values(
    env: dict[str, str], message: str
) -> None:
    with pytest.raises(RepositoryError, match=message) as caught:
        repository_from_env(env, httpx.AsyncClient())
    assert "hidden" not in str(caught.value)


async def test_a_local_path_selects_the_dev_file(tmp_path: Path) -> None:
    path = tmp_path / "private" / "repo.json"
    local = repository_from_env({"FHA_LOCAL_REPOSITORY": str(path)}, httpx.AsyncClient())
    assert isinstance(local, LocalJsonRepository)
    await local.put("things", "a", {"v": 1})
    assert path.exists()


def test_the_dev_file_is_refused_on_vercel(tmp_path: Path) -> None:
    env = {"FHA_LOCAL_REPOSITORY": str(tmp_path / "r.json"), "VERCEL": "1"}
    with pytest.raises(RepositoryError, match="dev only"):
        repository_from_env(env, httpx.AsyncClient())
