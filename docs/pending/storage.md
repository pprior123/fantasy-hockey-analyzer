# Pending for DECISIONS / CLAUDE.md (stream A: storage)

## 2026-09-26 — M3: the storage backends (Firestore REST, dev file, factory)

**FirestoreRepository** (`fha.storage.firestore`) talks to Firestore's REST
API v1 with httpx.
- **Typed values:** encoded exactly (`nullValue`, `booleanValue`,
  `integerValue` as a string, `doubleValue`, `stringValue`, `arrayValue`,
  `mapValue`). A bool is never an int, an int never comes back as a float,
  and a whole double is decoded to a float even when Firestore sends it as
  a JSON integer. Kinds the app never writes (timestamps, bytes, references,
  geo points) and non-finite doubles are refused. The error names the kind,
  never the content.
- **By-ID operations name the document in the request body:** `get` is a
  one-document `:batchGet`, and `put` and `delete` are one-write `:commit`s.
  The emulator answered a legal 1,500-byte ID in a URL with 404 (its
  percent-encoded path is about 4.5 KB), and body names need no path
  quoting. A `put` is an update write without a mask, so it replaces the
  whole document, as the contract requires (verified on the emulator).
- **`replace_all`:** lists the collection, then sends one atomic `:commit`
  of updates for the given documents and deletes for the rest. Its limits
  are checked before sending:
  - updates plus deletes at most `MAX_BATCH` (500);
  - the request at most 10 MiB, Firestore's limit per request.
  So a replace can be refused although the other backends would accept it
  (e.g. 300 new IDs replacing 300 old ones is 600 writes). The typed layer
  must keep chunk IDs stable (`chunk-0..n`) and chunked collections well
  under 10 MiB in total. Note: Firestore's documentation has dropped the
  500-write limit for batches in places. 500 is kept anyway, since it's
  shared with the other backends and harmless at this app's sizes.
- **Race:** a document created by another writer between the list and the
  commit survives the replace. Accepted: each collection has one writer at a
  time (the refresh, or an Admin import), and the next replace removes it. A
  read-write transaction (`beginTransaction`, a transactional list, then
  commit) would close the gap. It was rejected because it takes more
  requests per refresh and brings emulator lock semantics into the tests,
  for a race the app doesn't have.
- **Errors:** `RepositoryError("Firestore <action> <collection/id>: HTTP
  <status> (<Google status>: <message>)")`, or `"Firestore <action>
  failed: <ExceptionType>"` for transport errors. The token is never in a
  message.

**Tokens** (`fha.sources.google_auth`):
- `ServiceAccountTokens(key_json, scopes, http)` is an async callable
  returning a bearer token. It is shared with the Sheets reader (stream B),
  which is why it lives in `sources`.
- It signs the JWT with google-auth's `RSASigner` and `jwt.encode`, imported
  lazily at the first token, and exchanges it at the key's `token_uri` with
  httpx. That makes google-auth plus `cryptography` the new runtime
  dependencies, and `requests` isn't needed.
- The token is cached until 60 s before expiry, and a refresh is
  single-flight.
- No message carries the key, the assertion or the token.
- `emulator_token()` returns the emulators' documented fake, `owner`.
- Alternatives:
  - google-auth's `service_account.Credentials.refresh`, rejected because
    it needs a `requests` or `aiohttp` transport;
  - hand-rolled RS256 with `cryptography`, rejected because google-auth
    already does it correctly.

**LocalJsonRepository** (`fha.storage.local_json`), dev only:
- One JSON file, read and rewritten whole on each operation.
- Each write creates a fresh 0600 temp file (`O_EXCL`) and `os.replace`s the
  file, as the token file does. A failed write leaves the previous file.
- It refuses to construct when `VERCEL` is set.

**`repository_from_env(environ, http)`** (`fha.storage.factory`), checked in
this order:
1. `FIRESTORE_EMULATOR_HOST`, which must be a loopback `host:port`, since
   the emulator's fake token must not leave the machine. The project is
   `FIRESTORE_PROJECT_ID` or `demo-fha`; `demo-` projects never reach real
   Google services.
2. `FIRESTORE_PROJECT_ID` + `FIRESTORE_SERVICE_ACCOUNT_JSON`: production.
   Either one alone is an error naming the other.
3. `FHA_LOCAL_REPOSITORY=<path>`: the dev file.
4. Otherwise an error listing the variables.

Errors name variables, never values. The caller owns the httpx client. The
Firestore and file modules are imported only by the branch that needs them.

**Emulator tests** (`tests/unit/storage/test_firestore_emulator.py`):
- **What runs:** the shared contract, plus a raw check that ints are
  `integerValue`s and a 450-document listing that crosses page boundaries.
- **Isolation:** function-scoped. Each test first clears the database with
  the emulator's `DELETE /emulator/v1/projects/{p}/databases/(default)/documents`.
  A unique project per test isn't possible with `singleProjectMode`.
- **Skipping:** they skip without `FIRESTORE_EMULATOR_HOST`, but under
  `FHA_REQUIRE_EMULATOR=1`, as in CI, that is a failure instead.
- **Config:** `firebase.json` pins the emulator to 127.0.0.1:8181 with the
  UI off.
- **CI:** a second job, `firestore-emulator`, runs setup-java 6.0.1
  (Temurin 21) and setup-node 7.0.0, then `firebase-tools@15.31.0
  emulators:exec`.

## CLAUDE.md "Commands" addition

```
PATH="/opt/homebrew/opt/openjdk/bin:$PATH" npx -y firebase-tools@15.31.0 emulators:exec \
  --only firestore --project demo-fha "FHA_REQUIRE_EMULATOR=1 uv run pytest tests/unit/storage -q"
                                # storage contract against the Firestore emulator (needs Java)
```
