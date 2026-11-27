"""A NAMED Hermes profile's settings live in its secret scope, not in ``os.environ``.

Run 3 of the C10 demo: in 0.23.3's chat runtime the profile ``.env`` is read through
``agent.secret_scope.get_secret`` (the launch process's ``os.environ`` is the launch profile's),
so a provider reading ``os.environ`` saw no ``STRATA_AGENT_SCOPE`` and Hermes reported
"Memory provider 'strata' is selected but reports unavailable". The provider must read through the
scope, bind the values once in ``initialize`` and pass them to ``strata-mcp`` explicitly.
"""

from __future__ import annotations

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
    scope_values = {
        "STRATA_AGENT_SCOPE": "g_hermes",
        "STRATA_AGENT_SKILL": "hermes-demo",
        "STRATA_PROJECT_DIR": str(strata_env),
        "STRATA_MCP_COMMAND": f"{shlex.quote(sys.executable)} {shlex.quote(str(SERVER))}",
    }
    for name in scope_values:
        monkeypatch.delenv(name)
    return scope_values


def _new():  # noqa: ANN202
    from strata_hermes.provider import StrataMemoryProvider  # noqa: PLC0415

    return StrataMemoryProvider()


def test_a_scope_only_in_the_profile_scope_makes_the_provider_available(named_profile) -> None:  # noqa: ANN001
    provider = _new()
    assert provider.is_available() is False  # nothing bound: os.environ has nothing
    with ss.scoped(named_profile):
        assert provider.is_available() is True


def test_initialize_binds_the_scoped_values_and_reads_the_scopes_perspective(
    named_profile, hermes_home
) -> None:  # noqa: ANN001
    provider = _new()
    try:
        with ss.scoped(named_profile):
            provider.initialize("sess_p1", hermes_home=str(hermes_home), platform="cli")
        # the binding is kept on the instance: it holds after the scope is gone
        status = json.loads(provider.handle_tool_call("strata_status", {}))
        assert status["scope"] == "g_hermes" and status["skill"] == "hermes-demo"
        assert status["server"] == "up" and status["problem"] is None
        perspective = json.loads(provider.handle_tool_call("strata_read_perspective", {}))
        assert perspective["scope_id"] == "g_hermes"  # the subprocess got the scoped scope
    finally:
        provider.shutdown()


def test_the_profile_env_file_under_hermes_home_is_the_fallback(named_profile, hermes_home) -> None:  # noqa: ANN001
    (hermes_home / ".env").write_text(
        "".join(f"{k}={v}\n" for k, v in named_profile.items()), encoding="utf-8"
    )
    provider = _new()
    try:
        provider.initialize(
            "sess_p2", hermes_home=str(hermes_home), platform="cli"
        )  # no scope bound
        assert provider._init_error is None  # noqa: SLF001
        assert json.loads(provider.handle_tool_call("strata_status", {}))["scope"] == "g_hermes"
    finally:
        provider.shutdown()


def test_a_routed_profile_never_reads_the_launch_environment(
    named_profile, hermes_home, monkeypatch
) -> None:  # noqa: ANN001
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_other_profile")  # the LAUNCH profile's value
    provider = _new()
    with ss.scoped({"STRATA_AGENT_SKILL": "x"}, routed=True):  # this profile sets no scope
        assert provider.is_available() is False
        provider.initialize("sess_p3", hermes_home=str(hermes_home), platform="cli")
    assert provider._init_error is not None  # noqa: SLF001
    assert "STRATA_AGENT_SCOPE" in provider.unavailable_reason()


def test_a_default_profile_still_reads_os_environ(strata_env, hermes_home) -> None:  # noqa: ANN001
    provider = _new()
    assert provider.is_available() is True  # the original behaviour: no scope bound
    with ss.scoped({}, routed=False):  # a non-routed scope falls through to os.environ
        assert _new().is_available() is True


def test_two_instances_keep_their_own_binding(named_profile, hermes_home) -> None:  # noqa: ANN001
    a, b = _new(), _new()
    try:
        with ss.scoped(named_profile):
            a.initialize("sess_a", hermes_home=str(hermes_home), platform="cli")
        other = {**named_profile, "STRATA_AGENT_SKILL": "other-skill"}
        with ss.scoped(other):
            b.initialize("sess_b", hermes_home=str(hermes_home), platform="cli")
        assert a._skill() == "hermes-demo" and b._skill() == "other-skill"  # noqa: SLF001
    finally:
        a.shutdown()
        b.shutdown()


def test_a_scope_missing_from_the_profile_scope_and_os_environ_is_unavailable_with_a_clear_reason(
    named_profile, hermes_home, monkeypatch
) -> None:  # noqa: ANN001
    """Neither the profile scope nor os.environ has STRATA_AGENT_SCOPE: the provider is
    unavailable, says WHICH setting is missing, and initialize reports the UNBOUND code."""
    from strata_hermes.client import UNBOUND  # noqa: PLC0415

    monkeypatch.delenv("STRATA_AGENT_SCOPE", raising=False)
    without_scope = {k: v for k, v in named_profile.items() if k != "STRATA_AGENT_SCOPE"}
    provider = _new()
    with ss.scoped(without_scope):
        assert provider.is_available() is False
        reason = provider.unavailable_reason()
        assert "STRATA_AGENT_SCOPE" in reason and "not set" in reason
        provider.initialize("sess_missing", hermes_home=str(hermes_home), platform="cli")
    assert provider._init_error is not None  # noqa: SLF001
    assert provider._init_error.code == UNBOUND  # noqa: SLF001
    assert "STRATA_AGENT_SCOPE" in provider._init_error.message  # noqa: SLF001
    out = json.loads(provider.handle_tool_call("strata_read_perspective", {}))
    assert out["code"] == UNBOUND and "STRATA_AGENT_SCOPE" in out["error"]
    assert provider._client is None  # noqa: SLF001  no server was started
