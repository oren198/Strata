"""Tests for :mod:`strata.settings` — the ``anthropic_api_key`` fallback.

The operator-facing error message (``scope_manager.py``) tells the user to
"export it or add it to .env", naming the bare ``ANTHROPIC_API_KEY``. But
``Settings`` only mapped the ``STRATA_``-prefixed env var from ``.env``; the
bare-name fallback (``_fallback_api_key``) only ever read live process
environment (``os.environ``), never the ``.env`` file. An operator who
followed the message exactly — put a plain ``ANTHROPIC_API_KEY=...`` line in
``.env`` — got nothing. These tests pin both accepted spellings working from
a ``.env`` file in the resolved cwd.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from strata.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_bare_anthropic_api_key_from_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare ``ANTHROPIC_API_KEY=...`` line in ``.env`` must be honored."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=x\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("STRATA_ANTHROPIC_API_KEY", raising=False)

    from strata.settings import get_settings

    assert get_settings().anthropic_api_key == "x"


def test_prefixed_anthropic_api_key_from_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``STRATA_``-prefixed spelling must keep working from ``.env``."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("STRATA_ANTHROPIC_API_KEY=y\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("STRATA_ANTHROPIC_API_KEY", raising=False)

    from strata.settings import get_settings

    assert get_settings().anthropic_api_key == "y"


def test_prefixed_env_var_wins_over_bare_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prefixed process env var still takes priority over a bare .env line."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-dotenv\n")
    monkeypatch.setenv("STRATA_ANTHROPIC_API_KEY", "from-process-env")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from strata.settings import get_settings

    assert get_settings().anthropic_api_key == "from-process-env"


def test_bare_process_env_var_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-existing os.environ fallback path keeps working."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STRATA_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-process-env")

    from strata.settings import get_settings

    assert get_settings().anthropic_api_key == "from-process-env"
