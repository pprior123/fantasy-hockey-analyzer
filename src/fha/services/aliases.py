"""The alias table (SPEC §6), seeded once from the workbook's aliases.

``fha/data/alias_seed.json`` ships with the app (NHL player names only); the
test fixture of the same name is what ``scripts/extract_golden.py`` writes,
and a test keeps the two identical.
"""

from __future__ import annotations

import json
from importlib import resources

from fha.domain.matcher import Aliases
from fha.domain.names import normalize_name
from fha.storage.repository import Repository

COLLECTION = "aliases"


def seed_entries() -> list[dict[str, str]]:
    text = resources.files("fha").joinpath("data/alias_seed.json").read_text(encoding="utf-8")
    entries: list[dict[str, str]] = json.loads(text)
    return entries


async def load_aliases(repo: Repository) -> Aliases:
    """The alias table, seeding it on first use (an empty collection)."""
    stored = await repo.all(COLLECTION)
    if not stored:
        await seed_aliases(repo)
        stored = await repo.all(COLLECTION)
    aliases = Aliases()
    for doc in stored.values():
        for stats_name in doc["stats_names"]:
            aliases = aliases.with_alias(stats_name, doc["salary_name"])
    return aliases


async def seed_aliases(repo: Repository) -> None:
    """Write the seed aliases, one document per salary name (idempotent)."""
    table = Aliases.from_seed(seed_entries()).table
    first_spelling: dict[str, str] = {}
    for entry in seed_entries():
        first_spelling.setdefault(_key(entry["salary_name"]), entry["salary_name"])
    docs = {
        key: {"salary_name": first_spelling[key], "stats_names": list(targets)}
        for key, targets in table.items()
    }
    await repo.replace_all(COLLECTION, docs)


async def add_alias(repo: Repository, stats_name: str, salary_name: str) -> Aliases:
    """Record that ``salary_name`` (a sheet or CSV spelling) means ``stats_name``."""
    aliases = (await load_aliases(repo)).with_alias(stats_name, salary_name)
    key = _key(salary_name)
    await repo.put(
        COLLECTION, key, {"salary_name": salary_name, "stats_names": list(aliases.table[key])}
    )
    return aliases


def _key(salary_name: str) -> str:
    return normalize_name(salary_name)
