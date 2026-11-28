"""Strata memory provider for Hermes Agent (a directory plugin).

Hermes loads this directory under a synthetic module name, so every import here is relative.
``register`` hands Hermes the provider with ``register_memory_provider``.
"""

from __future__ import annotations

__version__ = "0.2.0"

from . import client, engine, tools
from . import provider as provider_module
from .provider import StrataMemoryProvider


def register(ctx) -> None:  # noqa: ANN001 — the Hermes plugin context
    ctx.register_memory_provider(StrataMemoryProvider())


__all__ = ["StrataMemoryProvider", "client", "engine", "provider_module", "register", "tools"]
