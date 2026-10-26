"""The judge under test, taken from the user's settings: the ONLY thing judge-check reads from
them. Model, provider pin, base URL and key come from the same place the engine's own judge
reads them; nothing else (storage paths, project config, daily cap) is consulted."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from strata.judgecheck.runner import JudgeSpec


def judge_spec_from_settings(settings: Any, *, label: str = "candidate") -> JudgeSpec:
    """A :class:`JudgeSpec` for the judge *settings* configure (a `strata.settings.Settings`)."""
    return JudgeSpec(
        label=label,
        client=settings.build_judge_client(),
        model=settings.manager_model,
        provider=settings.judge_provider,
    )


def describe_judge(settings: Any) -> dict[str, str | None]:
    """What a report may say about the judge: model, provider pin, and the base URL's HOST only.
    Never the key, never the full URL (no path, query, or credentials)."""
    base = getattr(settings, "judge_base_url", None)
    host = urlparse(base).hostname if base else None
    return {
        "model": settings.manager_model,
        "provider": settings.judge_provider,
        "base_url_host": host,
    }
