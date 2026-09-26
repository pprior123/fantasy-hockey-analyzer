"""Rating settings (SPEC §5, §7) persisted in the Repository's config.

Saved settings are validated by ``EngineConfig.from_mapping`` and stored in
its normal form (``to_mapping``). The cache holds raw stats, so a change
re-rates every player on the next view with no refresh.
"""

from __future__ import annotations

from collections.abc import Mapping

from fha.domain.engine import DEFAULT_CONFIG, EngineConfig
from fha.storage.repository import Repository

COLLECTION = "config"
RATING = "rating"


class SettingsError(ValueError):
    """The stored or submitted rating settings are invalid."""


async def load_rating_settings(repo: Repository) -> EngineConfig:
    """The settings in use: the stored ones, or the defaults if none were saved."""
    stored = await repo.get(COLLECTION, RATING)
    if stored is None:
        return DEFAULT_CONFIG
    try:
        return EngineConfig.from_mapping(stored)
    except ValueError as e:
        raise SettingsError(f"stored rating settings are invalid: {e}") from None


async def save_rating_settings(repo: Repository, submitted: Mapping[str, object]) -> EngineConfig:
    """Validate ``submitted`` (e.g. the Admin form's strings), store it, return it."""
    try:
        config = EngineConfig.from_mapping(submitted)
    except ValueError as e:
        raise SettingsError(str(e)) from None
    await repo.put(COLLECTION, RATING, config.to_mapping())
    return config
