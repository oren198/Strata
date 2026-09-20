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


def test_judge_base_url_defaults_to_openrouter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)

    from strata.settings import get_settings

    assert get_settings().judge_base_url == "https://openrouter.ai/api"


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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-judge-key")

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


def test_build_judge_client_uses_the_default_endpoint_for_the_default_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("JUDGE_API_KEY", "sk-or-key")

    from strata.settings import get_settings

    captured: dict = {}

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    get_settings().build_judge_client()

    assert captured == {"api_key": "sk-or-key", "base_url": "https://openrouter.ai/api"}


# ---------------------------------------------------------------------------
# resolve_judge_credentials — the raw-env-dict counterpart used by callers
# that don't have a constructed Settings object (the freshness evaluator).
# ---------------------------------------------------------------------------


def test_resolve_judge_credentials_prefers_judge_api_key() -> None:
    from strata.settings import resolve_judge_credentials

    env = {"JUDGE_API_KEY": "jk", "ANTHROPIC_API_KEY": "ak"}
    api_key, base_url = resolve_judge_credentials(env)
    assert api_key == "jk"
    assert base_url == "https://openrouter.ai/api"


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


# ---------------------------------------------------------------------------
# Default judge (qwen on OpenRouter) and the no-silent-switch rule.
# ---------------------------------------------------------------------------

_QWEN = "qwen/qwen3-235b-a22b-2507"
_OPENROUTER = "https://openrouter.ai/api"


def test_settings_default_judge_is_the_measured_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    from strata.settings import Settings

    s = Settings()
    assert s.manager_model == _QWEN
    assert s.judge_base_url == _OPENROUTER
    assert s.resolved_judge.reason == "default"


def test_settings_judge_key_alone_gets_the_default_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JUDGE_API_KEY (an OpenRouter key) and nothing else -> the default judge."""
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("JUDGE_API_KEY", "sk-or-abc")
    from strata.settings import Settings

    s = Settings()
    assert (s.manager_model, s.judge_base_url) == (_QWEN, _OPENROUTER)


def test_settings_anthropic_key_alone_keeps_haiku_on_anthropic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing install (only ANTHROPIC_API_KEY) never changes judge on upgrade."""
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abc")
    from strata.settings import Settings

    s = Settings()
    assert s.manager_model == "claude-haiku-4-5"
    assert s.judge_base_url is None
    assert s.resolved_judge.reason == "kept_anthropic"


def test_settings_strata_anthropic_key_alone_keeps_haiku(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("STRATA_ANTHROPIC_API_KEY", "anything")
    from strata.settings import Settings

    assert Settings().manager_model == "claude-haiku-4-5"


def test_settings_anthropic_key_in_dotenv_keeps_haiku(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-x\n")
    from strata.settings import Settings

    s = Settings()
    assert (s.manager_model, s.judge_base_url) == ("claude-haiku-4-5", None)


def test_settings_judge_key_wins_so_anthropic_key_alongside_gets_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JUDGE_API_KEY (non-Anthropic) beside an old ANTHROPIC key: the JUDGE_* choice wins."""
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("JUDGE_API_KEY", "sk-or-abc")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abc")
    from strata.settings import Settings

    s = Settings()
    assert (s.manager_model, s.judge_base_url) == (_QWEN, _OPENROUTER)


def test_settings_judge_key_that_is_an_anthropic_key_is_kept_on_anthropic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`register` v1.12 wrote JUDGE_API_KEY=<key>; an sk-ant- key there is an existing install."""
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("JUDGE_API_KEY", "sk-ant-api03-zzz")
    from strata.settings import Settings

    s = Settings()
    assert (s.manager_model, s.judge_base_url) == ("claude-haiku-4-5", None)


def test_settings_manager_model_override_alone_keeps_the_anthropic_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STRATA_MANAGER_MODEL / JUDGE_MODEL beside an Anthropic key: the endpoint stays."""
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abc")
    monkeypatch.setenv("STRATA_MANAGER_MODEL", "claude-sonnet-4-5")
    from strata.settings import Settings

    s = Settings()
    assert (s.manager_model, s.judge_base_url) == ("claude-sonnet-4-5", None)
    assert s.resolved_judge.reason == "override"


def test_settings_explicit_overrides_win_over_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("JUDGE_API_KEY", "k")
    monkeypatch.setenv("JUDGE_MODEL", "my/model")
    monkeypatch.setenv("JUDGE_BASE_URL", "https://gw.example")
    from strata.settings import Settings

    s = Settings()
    assert (s.manager_model, s.judge_base_url) == ("my/model", "https://gw.example")
    assert s.resolved_judge.reason == "override"


def test_settings_model_override_with_judge_key_takes_the_default_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_judge_env(monkeypatch)
    monkeypatch.setenv("JUDGE_API_KEY", "sk-or-k")
    monkeypatch.setenv("JUDGE_MODEL", "anthropic/claude-haiku-4.5")
    from strata.settings import Settings

    s = Settings()
    assert (s.manager_model, s.judge_base_url) == ("anthropic/claude-haiku-4.5", _OPENROUTER)


def test_settings_explicit_kwargs_count_as_overrides() -> None:
    from strata.settings import Settings

    s = Settings(manager_model="claude-haiku-4-5", judge_base_url=None, anthropic_api_key="ak")
    assert (s.manager_model, s.judge_base_url) == ("claude-haiku-4-5", None)


def test_resolve_judge_from_env_matches_settings_for_every_combination() -> None:
    from strata.settings import resolve_judge_from_env

    assert resolve_judge_from_env({}).model == _QWEN
    assert resolve_judge_from_env({"JUDGE_API_KEY": "sk-or-1"}).base_url == _OPENROUTER
    kept = resolve_judge_from_env({"ANTHROPIC_API_KEY": "sk-ant-1"})
    assert (kept.model, kept.base_url, kept.api_key, kept.reason) == (
        "claude-haiku-4-5",
        None,
        "sk-ant-1",
        "kept_anthropic",
    )
    both = resolve_judge_from_env({"JUDGE_MODEL": "m", "ANTHROPIC_API_KEY": "sk-ant-1"})
    assert (both.model, both.base_url) == ("m", None)


def test_resolve_judge_credentials_returns_the_resolved_endpoint() -> None:
    """The freshness drafter and the judge must land on the same endpoint."""
    from strata.settings import resolve_judge_credentials

    assert resolve_judge_credentials({"JUDGE_API_KEY": "sk-or-1"}) == ("sk-or-1", _OPENROUTER)
    assert resolve_judge_credentials({"ANTHROPIC_API_KEY": "sk-ant-1"}) == ("sk-ant-1", None)
