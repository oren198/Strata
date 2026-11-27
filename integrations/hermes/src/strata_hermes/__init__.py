"""strata-hermes: a Strata memory provider for Hermes Agent."""

from __future__ import annotations

__version__ = "0.1.0"


def register(ctx) -> None:  # noqa: ANN001 — the Hermes plugin context
    """The ``hermes_agent.memory_providers`` entry point: hand Hermes a fresh provider."""
    from strata_hermes.provider import StrataMemoryProvider  # noqa: PLC0415

    ctx.register_memory_provider(StrataMemoryProvider())
