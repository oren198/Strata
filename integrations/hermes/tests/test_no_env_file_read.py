"""Catalog rule 11: a plugin never reads ``.env`` files; settings come through ``get_secret``.

(Replaces the run-3 fallback that read the profile ``.env`` under ``hermes_home`` directly.)
"""

from __future__ import annotations

import builtins
import json
import shlex
import sys
from pathlib import Path

import pytest
from agent import secret_scope as ss


SERVER = Path(__file__).parent / "fake_strata_server.py"


@pytest.fixture
def named_profile(strata_env: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The profile's own settings, with NONE of them in os.environ."""
    values = {
        "STRATA_AGENT_SCOPE": "g_hermes",
        "STRATA_AGENT_SKILL": "hermes-demo",
        "STRATA_PROJECT_DIR": str(strata_env),
        "STRATA_MCP_COMMAND": f"{shlex.quote(sys.executable)} {shlex.quote(str(SERVER))}",
    }
    for name in values:
        monkeypatch.delenv(name)
    return values


def _new():  # noqa: ANN202
    from strata_hermes.provider import StrataMemoryProvider  # noqa: PLC0415

    return StrataMemoryProvider()


@pytest.fixture
def watch_env_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every attempt to read a ``.env`` file (``load_env_file``, ``open``, ``read_text``)."""
    touched: list[str] = []
    monkeypatch.setattr(
        ss, "load_env_file", lambda path: touched.append(f"load_env_file({path})") or {}
    )
    real_open, real_read = builtins.open, Path.read_text

    def spy_open(file, *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN202
        if str(file).endswith(".env"):
            touched.append(f"open({file})")
        return real_open(file, *a, **k)

    def spy_read(self, *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN202
        if self.name == ".env":
            touched.append(f"read_text({self})")
        return real_read(self, *a, **k)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(Path, "read_text", spy_read)
    return touched


def test_the_named_profiles_settings_resolve_through_get_secret_alone(
    named_profile, hermes_home, watch_env_reads
) -> None:  # noqa: ANN001
    (hermes_home / ".env").write_text("STRATA_AGENT_SCOPE=g_from_the_env_file\n", encoding="utf-8")
    provider = _new()
    try:
        with ss.scoped(named_profile):
            provider.initialize("sess_r11", hermes_home=str(hermes_home), platform="cli")
        status = json.loads(provider.handle_tool_call("strata_status", {}))
        assert status["scope"] == "g_hermes"  # the scoped value, not the file's
        assert status["server"] == "up"
    finally:
        provider.shutdown()
    assert [t for t in watch_env_reads if "fake_strata" not in t] == []


def test_when_get_secret_has_nothing_the_provider_is_unavailable_and_says_why(
    named_profile, hermes_home, watch_env_reads
) -> None:  # noqa: ANN001
    """The profile's ``.env`` HOLDS the scope, but the provider may not read that file."""
    from strata_hermes.client import UNBOUND  # noqa: PLC0415

    (hermes_home / ".env").write_text("STRATA_AGENT_SCOPE=g_hermes\n", encoding="utf-8")
    nothing = {k: v for k, v in named_profile.items() if k != "STRATA_AGENT_SCOPE"}
    provider = _new()
    with ss.scoped(nothing):
        assert provider.is_available() is False
        assert "STRATA_AGENT_SCOPE" in provider.unavailable_reason()
        provider.initialize("sess_r11b", hermes_home=str(hermes_home), platform="cli")
    assert provider._init_error is not None  # noqa: SLF001
    assert provider._init_error.code == UNBOUND  # noqa: SLF001
    assert provider._client is None  # noqa: SLF001
    assert watch_env_reads == []
