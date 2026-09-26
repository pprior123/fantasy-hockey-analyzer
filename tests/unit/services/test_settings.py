"""Rating settings persisted in the Repository (SPEC §5, §7)."""

import pytest

from fha.domain.engine import DEFAULT_CONFIG, DivisorMethod, EngineConfig
from fha.domain.models import Category
from fha.services.settings import (
    COLLECTION,
    RATING,
    SettingsError,
    load_rating_settings,
    save_rating_settings,
)
from fha.storage.memory import InMemoryRepository


async def test_nothing_saved_means_the_workbook_defaults() -> None:
    assert await load_rating_settings(InMemoryRepository()) == DEFAULT_CONFIG


async def test_form_strings_are_validated_stored_normalized_and_read_back() -> None:
    repo = InMemoryRepository()
    form = {"divisor_method": "top_per82", "divisor_top_n": "", "gp_floor_fraction": "0.05"}
    saved = await save_rating_settings(repo, form)
    assert saved == EngineConfig(divisor_method=DivisorMethod.TOP_PER82, gp_floor_fraction=0.05)
    assert await repo.get(COLLECTION, RATING) == {
        "categories": [c.value for c in Category],
        "gp_floor_fraction": 0.05,
        "divisor_method": "top_per82",
        "divisor_top_n": None,
    }
    assert await load_rating_settings(repo) == saved
    assert (await load_rating_settings(repo)).top_n == 10


async def test_invalid_form_values_are_refused_and_nothing_is_stored() -> None:
    repo = InMemoryRepository()
    with pytest.raises(SettingsError, match=r"^divisor_top_n must be a whole number, got 'x'$"):
        await save_rating_settings(repo, {"divisor_top_n": "x"})
    assert await repo.get(COLLECTION, RATING) is None


async def test_damaged_stored_settings_are_an_error_not_silently_the_default() -> None:
    repo = InMemoryRepository()
    await repo.put(COLLECTION, RATING, {"gp_floor_fraction": 7})
    with pytest.raises(SettingsError, match=r"^stored rating settings are invalid: gp_floor"):
        await load_rating_settings(repo)
