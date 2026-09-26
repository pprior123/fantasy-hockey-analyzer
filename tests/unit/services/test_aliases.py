"""The alias table: seeded once from the packaged workbook aliases (SPEC §6)."""

import json
from pathlib import Path

from fha.services.aliases import COLLECTION, add_alias, load_aliases, seed_entries
from fha.storage.memory import InMemoryRepository

FIXTURE = Path(__file__).parents[2] / "fixtures" / "alias_seed.json"


def test_the_packaged_seed_is_the_extracted_fixture() -> None:
    assert seed_entries() == json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(seed_entries()) == 57


async def test_first_load_seeds_every_alias() -> None:
    repo = InMemoryRepository()
    aliases = await load_aliases(repo)
    for entry in seed_entries():
        assert entry["stats_name"] in aliases.targets(entry["salary_name"])
    assert len(await repo.all(COLLECTION)) == len(aliases.table)


async def test_seeding_happens_once_and_owner_aliases_survive() -> None:
    repo = InMemoryRepository()
    await load_aliases(repo)
    await add_alias(repo, "Bo Stone", "Robert Stonewall")
    aliases = await load_aliases(repo)
    assert aliases.targets("Stonewall, Robert") == ("Bo Stone",)
    assert aliases.targets("Matthew Boldy") == ("Matt Boldy",)


async def test_adding_to_an_existing_salary_name_keeps_both_targets() -> None:
    repo = InMemoryRepository()
    await add_alias(repo, "Matt Boldy II", "Matthew Boldy")
    assert (await load_aliases(repo)).targets("Matthew Boldy") == ("Matt Boldy", "Matt Boldy II")
