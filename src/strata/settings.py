"""Central env-var-driven settings for the Strata backend.

All settings are prefixed ``STRATA_`` in the environment.  The ``db_path``
and ``summaries_dir`` values may also be set via ``.env`` files.

The ``anthropic_api_key`` field accepts either ``STRATA_ANTHROPIC_API_KEY``
or the bare ``ANTHROPIC_API_KEY`` (the convention used by the Anthropic SDK
and most tooling) — from process env *or* a ``.env`` file. The prefixed name
wins when both are set.

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

from pydantic import AliasChoices, Field, model_validator
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
    # Reachable as JUDGE_MODEL (provider-generic name) or the original
    # STRATA_MANAGER_MODEL — both are listed explicitly because setting a
    # validation_alias suppresses pydantic-settings' auto-generated
    # STRATA_-prefixed mapping. STRATA_MANAGER_MODEL wins when both are set.
    manager_model: str = Field(
        default="claude-haiku-4-5",
        validation_alias=AliasChoices("STRATA_MANAGER_MODEL", "JUDGE_MODEL"),
    )
    summary_max_words: int = Field(default=500, ge=1)
    # ADR 0011 D2: how many of the newest contributions in the scope-manager's
    # recency window keep their full verbatim text. Everything older renders as
    # a mechanical digest row. Raise it when phrasing-level duplicate detection
    # needs more than the digest carries.
    window_verbatim_tail: int = Field(default=3, ge=0)
    # ADR 0011 D2: how many of the newest contributions the recency window
    # spans — the windowed record read the judgment and refresh paths hand the
    # scope-manager. Raise it when judgment needs deeper record history in
    # view; lower it to shrink the prompt.
    recency_window_size: int = Field(default=20, ge=1)
    # Issue #130: how many contributions one page of a record read carries.
    # The record is append-only and only ever grows, so an unbounded read is
    # unbounded by construction; a page bounds the response without hiding
    # anything — the rest is one cursor away. Raise it to walk a long record in
    # fewer round trips; lower it to fit a tighter response budget.
    record_page_size: int = Field(default=20, ge=1)
    # ADR 0011 D3: how many queued contributions one judgment call may carry.
    # A cap keeps the prompt bounded and keeps a failed call from stranding
    # more than a cap's worth of contributions at once. 1 disables coalescing
    # — every contribution is judged on its own, as before this ADR.
    judgment_batch_cap: int = Field(default=5, ge=1)
    # An explicit validation_alias (rather than the auto-generated
    # STRATA_ANTHROPIC_API_KEY-only mapping) so the bare ANTHROPIC_API_KEY
    # spelling — the convention used by the Anthropic SDK and most tooling —
    # is honored from *both* process env and the .env file, not just process
    # env. Order matters: STRATA_ANTHROPIC_API_KEY is tried first, so it
    # wins when both are set. Before this, a bare `ANTHROPIC_API_KEY=...`
    # line in .env was silently ignored — pydantic-settings only mapped the
    # prefixed name from the env file, and the runtime fallback below only
    # ever read live process env, never .env.
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("STRATA_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )
    # Provider-generic judge configuration. JUDGE_API_KEY / JUDGE_BASE_URL
    # let the judge point at any endpoint that speaks the Anthropic Messages
    # API (a router, a proxy, a self-hosted gateway) — not only the direct
    # Anthropic API. JUDGE_API_KEY wins over anthropic_api_key when both are
    # set; see build_judge_client() below for the precedence.
    judge_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("STRATA_JUDGE_API_KEY", "JUDGE_API_KEY"),
    )
    judge_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("STRATA_JUDGE_BASE_URL", "JUDGE_BASE_URL"),
    )

    @model_validator(mode="after")
    def _fallback_api_key(self) -> Settings:
        """Last-resort fallback: read bare ``ANTHROPIC_API_KEY``/``JUDGE_API_KEY``
        from process env.

        The ``validation_alias`` above already covers both spellings from
        both env-var and .env sources; this only matters if some other
        settings-construction path (e.g. explicit kwargs) bypassed that.
        """
        if self.anthropic_api_key is None:
            self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
        if self.judge_api_key is None:
            self.judge_api_key = os.environ.get("JUDGE_API_KEY")
        return self

    def build_judge_client(self):  # -> anthropic.Anthropic
        """Construct the judge's Anthropic-Messages-API client.

        The single place every ``anthropic.Anthropic(...)`` construction in
        the codebase should go through, so the JUDGE_API_KEY / JUDGE_BASE_URL
        wiring — and the deprecated ANTHROPIC_API_KEY fallback — lives in one
        place. ``JUDGE_API_KEY`` wins when set; otherwise ``anthropic_api_key``
        (the old ``ANTHROPIC_API_KEY`` / ``STRATA_ANTHROPIC_API_KEY`` names)
        is used as a working, deprecated fallback. ``base_url`` is passed only
        when configured, so the client falls back to the SDK's own default
        (the direct Anthropic API) otherwise. The endpoint must speak the
        Anthropic Messages API — a router/proxy/self-hosted gateway that does
        so works via JUDGE_BASE_URL.
        """
        import anthropic  # noqa: PLC0415

        kwargs: dict = {"api_key": self.judge_api_key or self.anthropic_api_key}
        if self.judge_base_url:
            kwargs["base_url"] = self.judge_base_url
        return anthropic.Anthropic(**kwargs)


def resolve_judge_credentials(env: dict[str, str]) -> tuple[str | None, str | None]:
    """Resolve ``(api_key, base_url)`` for the judge from a raw env mapping.

    For callers that hold a raw ``env`` dict rather than a constructed
    :class:`Settings` — e.g. the freshness evaluator, which runs as a
    detached subprocess and reads its own env snapshot rather than
    ``get_settings()``. Same precedence as :meth:`Settings.build_judge_client`:
    ``JUDGE_API_KEY`` (either spelling) wins; the deprecated
    ``ANTHROPIC_API_KEY`` / ``STRATA_ANTHROPIC_API_KEY`` names are a working
    fallback.
    """
    api_key = (
        env.get("STRATA_JUDGE_API_KEY")
        or env.get("JUDGE_API_KEY")
        or env.get("STRATA_ANTHROPIC_API_KEY")
        or env.get("ANTHROPIC_API_KEY")
    )
    base_url = env.get("STRATA_JUDGE_BASE_URL") or env.get("JUDGE_BASE_URL")
    return api_key, base_url


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton.

    The ``lru_cache`` means the :class:`Settings` object is constructed once
    per process.  Tests may clear the cache via
    ``get_settings.cache_clear()`` and then override
    ``app.dependency_overrides[get_settings]`` to inject alternative values.
    """
    return Settings()
