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


# ---------------------------------------------------------------------------
# Generic judge configuration (JUDGE_API_KEY / JUDGE_BASE_URL / JUDGE_MODEL).
#
# The judge key was ANTHROPIC_API_KEY-only; the operator wants provider
# genericity. These names are additive: JUDGE_API_KEY wins when set, the old
# ANTHROPIC_API_KEY / STRATA_ANTHROPIC_API_KEY names remain a working
# fallback (deprecated, not removed).
# ---------------------------------------------------------------------------


def _clear_judge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "JUDGE_API_KEY",
        "STRATA_JUDGE_API_KEY",
        "JUDGE_BASE_URL",
        "STRATA_JUDGE_BASE_URL",
        "JUDGE_MODEL",
        "STRATA_MANAGER_MODEL",
        "ANTHROPIC_API_KEY",
        "STRATA_ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_bare_judge_api_key_from_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("JUDGE_API_KEY", "from-judge-key")

    from strata.settings import get_settings

    assert get_settings().judge_api_key == "from-judge-key"


def test_prefixed_judge_api_key_from_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("STRATA_JUDGE_API_KEY", "from-strata-judge-key")

    from strata.settings import get_settings

    assert get_settings().judge_api_key == "from-strata-judge-key"


def test_judge_api_key_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    (tmp_path / ".env").write_text("JUDGE_API_KEY=from-dotenv\n")

    from strata.settings import get_settings

    assert get_settings().judge_api_key == "from-dotenv"


def test_prefixed_judge_api_key_wins_over_bare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("STRATA_JUDGE_API_KEY", "prefixed")
    monkeypatch.setenv("JUDGE_API_KEY", "bare")

    from strata.settings import get_settings

    assert get_settings().judge_api_key == "prefixed"


def test_judge_base_url_from_process_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("JUDGE_BASE_URL", "https://router.example/v1")

    from strata.settings import get_settings

    assert get_settings().judge_base_url == "https://router.example/v1"


def test_judge_base_url_defaults_to_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)

    from strata.settings import get_settings

    assert get_settings().judge_base_url is None


def test_judge_model_alias_reaches_manager_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("JUDGE_MODEL", "some-router-model")

    from strata.settings import get_settings

    assert get_settings().manager_model == "some-router-model"


def test_strata_manager_model_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-existing STRATA_MANAGER_MODEL name must keep working."""
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("STRATA_MANAGER_MODEL", "manager-model-name")

    from strata.settings import get_settings

    assert get_settings().manager_model == "manager-model-name"


def test_judge_api_key_wins_over_anthropic_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JUDGE_API_KEY takes precedence; ANTHROPIC_API_KEY is a deprecated fallback."""
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("JUDGE_API_KEY", "judge-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")

    from strata.settings import get_settings

    settings = get_settings()
    assert settings.judge_api_key == "judge-key"
    assert settings.anthropic_api_key == "anthropic-key"

    import anthropic

    client = settings.build_judge_client()
    assert client.api_key == "judge-key"
    assert isinstance(client, anthropic.Anthropic)


def test_anthropic_api_key_fallback_still_builds_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No JUDGE_API_KEY set — the old ANTHROPIC_API_KEY name is a working fallback."""
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")

    from strata.settings import get_settings

    settings = get_settings()
    client = settings.build_judge_client()
    assert client.api_key == "anthropic-key"


def test_build_judge_client_passes_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("JUDGE_API_KEY", "judge-key")
    monkeypatch.setenv("JUDGE_BASE_URL", "https://router.example/v1")

    from strata.settings import get_settings

    settings = get_settings()
    client = settings.build_judge_client()
    assert str(client.base_url).startswith("https://router.example/v1")


def test_build_judge_client_uses_construction_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify build_judge_client wires api_key/base_url into anthropic.Anthropic(...)."""
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("JUDGE_API_KEY", "judge-key")
    monkeypatch.setenv("JUDGE_BASE_URL", "https://router.example/v1")

    from strata.settings import get_settings

    settings = get_settings()

    captured: dict = {}

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    client = settings.build_judge_client()

    assert isinstance(client, _FakeAnthropic)
    assert captured["api_key"] == "judge-key"
    assert captured["base_url"] == "https://router.example/v1"


def test_build_judge_client_omits_base_url_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("JUDGE_API_KEY", "judge-key")

    from strata.settings import get_settings

    settings = get_settings()

    captured: dict = {}

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    settings.build_judge_client()

    assert "base_url" not in captured


# ---------------------------------------------------------------------------
# resolve_judge_credentials — the raw-env-dict counterpart used by callers
# that don't have a constructed Settings object (the freshness evaluator).
# ---------------------------------------------------------------------------


def test_resolve_judge_credentials_prefers_judge_api_key() -> None:
    from strata.settings import resolve_judge_credentials

    env = {"JUDGE_API_KEY": "jk", "ANTHROPIC_API_KEY": "ak"}
    api_key, base_url = resolve_judge_credentials(env)
    assert api_key == "jk"
    assert base_url is None


def test_resolve_judge_credentials_prefixed_wins() -> None:
    from strata.settings import resolve_judge_credentials

    env = {"STRATA_JUDGE_API_KEY": "prefixed", "JUDGE_API_KEY": "bare"}
    api_key, _ = resolve_judge_credentials(env)
    assert api_key == "prefixed"


def test_resolve_judge_credentials_falls_back_to_anthropic() -> None:
    from strata.settings import resolve_judge_credentials

    env = {"ANTHROPIC_API_KEY": "ak"}
    api_key, _ = resolve_judge_credentials(env)
    assert api_key == "ak"


def test_resolve_judge_credentials_base_url() -> None:
    from strata.settings import resolve_judge_credentials

    env = {"JUDGE_API_KEY": "jk", "JUDGE_BASE_URL": "https://router.example/v1"}
    api_key, base_url = resolve_judge_credentials(env)
    assert api_key == "jk"
    assert base_url == "https://router.example/v1"
