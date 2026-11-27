"""A MINIMAL stand-in for Hermes 0.23.3's ``agent/secret_scope.py`` (not a verbatim copy).

It models only the contract the provider relies on (``get_secret`` at agent/secret_scope.py:249,
``load_env_file`` at :391, ``UnscopedSecretError``): with a profile scope bound, a ROUTED profile reads
its own mapping only (a miss returns the default, never ``os.environ``); a non-routed scope falls
through to ``os.environ``; with no scope it reads ``os.environ``. ``scoped(...)`` binds a scope.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from collections.abc import Iterator, Mapping
from pathlib import Path

_BOUND: contextvars.ContextVar[tuple[Mapping[str, str], bool] | None] = contextvars.ContextVar(
    "fake_secret_scope", default=None
)


class UnscopedSecretError(RuntimeError):
    pass


@contextlib.contextmanager
def scoped(mapping: Mapping[str, str], *, routed: bool = True) -> Iterator[None]:
    token = _BOUND.set((dict(mapping), routed))
    try:
        yield
    finally:
        _BOUND.reset(token)


def get_secret(name: str, default: str | None = None) -> str | None:
    bound = _BOUND.get()
    if bound is None:
        return os.environ.get(name, default)
    mapping, routed = bound
    if (value := mapping.get(name)) is not None:
        return value
    return default if routed else os.environ.get(name, default)


def load_env_file(env_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = Path(env_path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out
