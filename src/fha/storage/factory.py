"""Which Repository the environment asks for (SPEC §8 lists the variables).

- ``FIRESTORE_EMULATOR_HOST`` (loopback only): the emulator, in tests.
- ``FIRESTORE_PROJECT_ID`` + ``FIRESTORE_SERVICE_ACCOUNT_JSON``: Firestore, in production.
- ``FHA_LOCAL_REPOSITORY=<path>``: a local JSON file, in dev (under ``private/``).

Errors name the variables, never their values.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import httpx

from fha.sources.google_auth import GoogleAuthError, ServiceAccountTokens, emulator_token
from fha.storage.repository import Repository, RepositoryError

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
EMULATOR_PROJECT = "demo-fha"  # "demo-" projects never reach real Google services
PROJECT = "FIRESTORE_PROJECT_ID"
KEY = "FIRESTORE_SERVICE_ACCOUNT_JSON"
EMULATOR = "FIRESTORE_EMULATOR_HOST"
LOCAL = "FHA_LOCAL_REPOSITORY"


def repository_from_env(
    environ: Mapping[str, str], http: httpx.AsyncClient, *, private_dir: Path | None = None
) -> Repository:
    """The configured Repository. ``http`` is the caller's client (it owns closing it).

    ``private_dir``: where the dev file may live (default: the checkout's ``private/``).
    """
    from fha.storage.firestore import SCOPE, FirestoreRepository

    if environ.get(EMULATOR):
        host = emulator_host(environ[EMULATOR])
        emulated = environ.get(PROJECT) or EMULATOR_PROJECT
        return FirestoreRepository(http, emulated, emulator_token, base_url=f"http://{host}")
    project, key = environ.get(PROJECT, ""), environ.get(KEY, "")
    if (project or key) and environ.get(LOCAL):
        raise RepositoryError(f"set either {PROJECT}/{KEY} (Firestore) or {LOCAL} (dev), not both")
    if project or key:
        if not (project and key):
            missing = PROJECT if not project else KEY
            raise RepositoryError(f"{missing} is not set (Firestore needs {PROJECT} and {KEY})")
        try:
            tokens = ServiceAccountTokens(key, [SCOPE], http)
        except GoogleAuthError as e:
            raise RepositoryError(f"{KEY}: {e}") from None
        return FirestoreRepository(http, project, tokens)
    if environ.get(LOCAL):
        from fha.storage.local_json import LocalJsonRepository

        if private_dir is None:
            return LocalJsonRepository(Path(environ[LOCAL]), environ)
        return LocalJsonRepository(Path(environ[LOCAL]), environ, private_dir=private_dir)
    raise RepositoryError(
        f"no repository configured: set {PROJECT} and {KEY} (production), "
        f"{EMULATOR} (tests) or {LOCAL} (dev)"
    )


def emulator_host(value: str) -> str:
    """``value`` if it is a loopback ``host:port``: the fake token must stay on the machine."""
    host, sep, port = value.rpartition(":")
    bare = host.removeprefix("[").removesuffix("]")
    if not sep or not port.isdigit() or bare not in LOOPBACK:
        raise RepositoryError(f"{EMULATOR} must be a loopback host:port")
    return value
