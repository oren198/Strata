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
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import AliasChoices, Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from strata.session_state import DEFAULT_SESSION_IDLE_WINDOW_SECONDS

#: The default judge (measured 2026-09-20; see the README's "Choosing a judge").
DEFAULT_JUDGE_MODEL = "qwen/qwen3-235b-a22b-2507"
DEFAULT_JUDGE_BASE_URL = "https://openrouter.ai/api"
#: What an install that only ever had an Anthropic key keeps judging with.
KEPT_JUDGE_MODEL = "claude-haiku-4-5"

#: Why the judge resolved the way it did (:attr:`ResolvedJudge.reason`).
JUDGE_REASON_DEFAULT = "default"
JUDGE_REASON_KEPT = "kept_anthropic"
JUDGE_REASON_OVERRIDE = "override"

_ANTHROPIC_KEY_PREFIX = "sk-ant-"


@dataclass(frozen=True)
class ResolvedJudge:
    """The judge Strata will actually use, and why. ``base_url`` None = api.anthropic.com."""

    model: str
    base_url: str | None
    api_key: str | None
    reason: str


def resolve_judge(
    *,
    model: str | None,
    base_url: str | None,
    judge_api_key: str | None,
    anthropic_api_key: str | None,
) -> ResolvedJudge:
    """The single place that decides which judge model and endpoint are used.

    Inputs are the *explicit* settings only (None = not set): ``model`` is
    ``JUDGE_MODEL`` / ``STRATA_MANAGER_MODEL``; ``base_url`` is
    ``JUDGE_BASE_URL``; the two keys are ``JUDGE_API_KEY`` and the old
    ``ANTHROPIC_API_KEY`` / ``STRATA_ANTHROPIC_API_KEY``.

    The rule (no silent switch — an upgrade must never change a judge, or post a
    user's Anthropic key to a third-party router):

    * an explicit model / base URL always wins;
    * an Anthropic key with no explicit ``JUDGE_BASE_URL`` stays on the Anthropic
      endpoint (model = the explicit one, else ``claude-haiku-4-5``). Two cases count
      as an Anthropic key, each for a reason:

      - **An ``sk-ant-`` ``JUDGE_API_KEY`` is an Anthropic key.** ``strata register``
        wrote ``JUDGE_API_KEY=<key>`` before the default changed, so an existing
        install's Anthropic key lives under that name; without this it would be sent to
        OpenRouter on upgrade.
      - **An explicit model with only an Anthropic key keeps the Anthropic endpoint.**
        ``JUDGE_MODEL`` / ``STRATA_MANAGER_MODEL`` alone chose a model, not a provider;
        moving the endpoint too would send the Anthropic key to a router.

    * otherwise the default judge: ``qwen/qwen3-235b-a22b-2507`` on
      ``https://openrouter.ai/api``, each half replaced by its explicit setting.
    """
    key = judge_api_key or anthropic_api_key
    key_is_anthropic = bool(key) and (
        not judge_api_key or judge_api_key.startswith(_ANTHROPIC_KEY_PREFIX)
    )
    if not base_url and key_is_anthropic:
        model = model or KEPT_JUDGE_MODEL
        reason = JUDGE_REASON_KEPT if model == KEPT_JUDGE_MODEL else JUDGE_REASON_OVERRIDE
        return ResolvedJudge(model, None, key, reason)
    model = model or DEFAULT_JUDGE_MODEL
    base_url = base_url or DEFAULT_JUDGE_BASE_URL
    # Explicit lines that just restate the default (what `strata register` writes beside
    # a captured key) are still the default judge — "configured" only when they differ.
    is_default = model == DEFAULT_JUDGE_MODEL and base_url == DEFAULT_JUDGE_BASE_URL
    return ResolvedJudge(
        model, base_url, key, JUDGE_REASON_DEFAULT if is_default else JUDGE_REASON_OVERRIDE
    )


def resolve_judge_from_env(env: Mapping[str, str]) -> ResolvedJudge:
    """:func:`resolve_judge` over a raw env mapping (callers with no :class:`Settings`)."""
    return resolve_judge(
        model=env.get("STRATA_MANAGER_MODEL") or env.get("JUDGE_MODEL") or None,
        base_url=env.get("STRATA_JUDGE_BASE_URL") or env.get("JUDGE_BASE_URL") or None,
        judge_api_key=env.get("STRATA_JUDGE_API_KEY") or env.get("JUDGE_API_KEY") or None,
        anthropic_api_key=env.get("STRATA_ANTHROPIC_API_KEY")
        or env.get("ANTHROPIC_API_KEY")
        or None,
    )


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
        default=DEFAULT_JUDGE_MODEL,
        validation_alias=AliasChoices("STRATA_MANAGER_MODEL", "JUDGE_MODEL"),
    )
    summary_max_words: int = Field(default=500, ge=1)
    # ADR 0013 D3: the word budget for a scope's PUBLISHED FACE (its own
    # published items, summed the same way summary_max_words is — see
    # scope_manager._content_word_count). Publication now travels exactly
    # one edge, so a face is bounded against its readers, never multiplied
    # by chain depth. Defaults to summary_max_words' default: a publication
    # is a SELECTION from the summary it is distilled from, so it cannot
    # coherently be given more room than the summary itself. Enforced by the
    # scope-manager at judgment time (mirrors summary_max_words' own
    # enforcement point) — never retroactively against items already
    # published.
    publication_max_words: int = Field(default=500, ge=1)
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
    # #210: a scope with no description is judged for relevance against its existing
    # memory only once that memory reaches this many words (summary context plus
    # directive text); below it there is nothing to tell what the scope is about and
    # no relevance judgement is made. Mirrors scope_manager.IMPLIED_PURPOSE_MIN_WORDS.
    implied_purpose_min_words: int = Field(default=50, ge=1)
    # M3: how long (seconds) a session with no recorded end may sit idle before
    # the write-back rate counts it as ended (a killed server never stamps its
    # own end). `strata stats writeback --idle-window` overrides it per run.
    session_idle_window_seconds: int = Field(default=DEFAULT_SESSION_IDLE_WINDOW_SECONDS, ge=1)
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
    # Defaults to OpenRouter; resolve_judge() keeps an Anthropic-key-only install on
    # the Anthropic endpoint (None) — see there. Read the *resolved* value here.
    judge_base_url: str | None = Field(
        default=DEFAULT_JUDGE_BASE_URL,
        validation_alias=AliasChoices("STRATA_JUDGE_BASE_URL", "JUDGE_BASE_URL"),
    )

    _resolved_judge: ResolvedJudge | None = PrivateAttr(default=None)

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
        # Resolve the judge from what was *explicitly* set, then write the result back
        # so every reader of manager_model / judge_base_url sees the resolved judge.
        given = self.model_fields_set
        resolved = resolve_judge(
            model=self.manager_model if "manager_model" in given else None,
            base_url=self.judge_base_url if "judge_base_url" in given else None,
            judge_api_key=self.judge_api_key,
            anthropic_api_key=self.anthropic_api_key,
        )
        self.manager_model = resolved.model
        self.judge_base_url = resolved.base_url
        self._resolved_judge = resolved
        return self

    @property
    def resolved_judge(self) -> ResolvedJudge:
        """The judge in effect, and why (:func:`resolve_judge`)."""
        assert self._resolved_judge is not None
        return self._resolved_judge

    def build_judge_client(self):  # -> anthropic.Anthropic
        """Construct the judge's Anthropic-Messages-API client.

        ``JUDGE_API_KEY`` wins when set; otherwise ``anthropic_api_key``
        (the old ``ANTHROPIC_API_KEY`` / ``STRATA_ANTHROPIC_API_KEY`` names)
        is used as a working, deprecated fallback. Delegates the actual
        construction to :func:`construct_judge_client` — see that function's
        docstring for why it, not this method, is the single construction
        site.
        """
        return construct_judge_client(
            api_key=self.judge_api_key or self.anthropic_api_key,
            base_url=self.judge_base_url,
        )


def construct_judge_client(
    *, api_key: str | None, base_url: str | None = None
):  # -> anthropic.Anthropic
    """Construct the judge's Anthropic-Messages-API client from a resolved
    ``(api_key, base_url)`` pair.

    The single place every ``anthropic.Anthropic(...)`` construction in the
    codebase should go through — both :meth:`Settings.build_judge_client`
    (which resolves credentials from a constructed ``Settings``) and callers
    that resolve credentials their own way (e.g. the freshness evaluator,
    which reads a raw subprocess env dict via
    :func:`resolve_judge_credentials`) route through this one function, so
    the kwarg-assembly logic — ``base_url`` passed only when configured —
    lives in exactly one place. The endpoint must speak the Anthropic
    Messages API — a router/proxy/self-hosted gateway that does so works via
    JUDGE_BASE_URL.
    """
    import anthropic  # noqa: PLC0415

    kwargs: dict = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
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
    resolved = resolve_judge_from_env(env)
    return resolved.api_key, resolved.base_url


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton.

    The ``lru_cache`` means the :class:`Settings` object is constructed once
    per process.  Tests may clear the cache via
    ``get_settings.cache_clear()`` and then override
    ``app.dependency_overrides[get_settings]`` to inject alternative values.
    """
    return Settings()
