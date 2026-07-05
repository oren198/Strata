"""Central env-var-driven settings for the Strata backend.

All settings are prefixed ``STRATA_`` in the environment.  The ``db_path``
and ``summaries_dir`` values may also be set via ``.env`` files.

The ``anthropic_api_key`` field accepts ``STRATA_ANTHROPIC_API_KEY``.  As a
convenience, if that variable is absent the validator falls back to the bare
``ANTHROPIC_API_KEY`` variable (the convention used by the Anthropic SDK and
most tooling).

The fleet config path is read from ``STRATA_FLEET_CONFIG`` (an explicit
alias, not the auto-generated ``STRATA_FLEET_YAML_PATH``) so that the CLI,
the README, and the backend all resolve the same single canonical file.

Usage::

    from strata.settings import get_settings

    settings = get_settings()  # cached singleton
"""

from __future__ import annotations

import functools
import os

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide configuration driven by environment variables.

    All fields use the ``STRATA_`` prefix (set via ``model_config``).
    """

    model_config = SettingsConfigDict(
        env_prefix="STRATA_",
        env_file=".env",
        extra="ignore",
        # Allow fields with an explicit validation_alias (fleet_yaml_path ←
        # STRATA_FLEET_CONFIG) to still be set by their Python name in code
        # and tests, not only via the env alias.
        populate_by_name=True,
    )

    db_path: str = Field(default="./strata.db")
    summaries_dir: str = Field(default="./summaries")
    fleet_yaml_path: str = Field(
        default="./fleet.yaml",
        validation_alias="STRATA_FLEET_CONFIG",
    )
    manager_model: str = Field(default="claude-haiku-4-5")
    summary_max_words: int = Field(default=500, ge=1)
    anthropic_api_key: str | None = Field(default=None)

    @model_validator(mode="after")
    def _fallback_api_key(self) -> Settings:
        """If ``STRATA_ANTHROPIC_API_KEY`` is unset, fall back to ``ANTHROPIC_API_KEY``."""
        if self.anthropic_api_key is None:
            self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
        return self


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton.

    The ``lru_cache`` means the :class:`Settings` object is constructed once
    per process.  Tests may clear the cache via
    ``get_settings.cache_clear()`` and then override
    ``app.dependency_overrides[get_settings]`` to inject alternative values.
    """
    return Settings()
