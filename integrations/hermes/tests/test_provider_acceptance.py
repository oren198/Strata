"""strata-hermes S2 acceptance tests, written before the provider.

They run without Hermes installed: ``tests/fake_hermes`` is a verbatim copy of Hermes's
``agent/memory_provider.py`` at v0.23.3 (caafe53), and the Strata side is a real ``strata-mcp``
stdio server with a scripted judge. The provider talks to it only over stdio.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "src" / "strata_hermes"
FAST = 0.25  # seconds: "returns immediately"


def _call(provider, tool: str, **args) -> dict:  # noqa: ANN001, ANN003
    return json.loads(provider.handle_tool_call(tool, args))


def _snapshot(home: Path) -> dict[str, str]:
    return {
        str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(home.rglob("*"))
        if p.is_file()
    }


# -- 1. bind and read ---------------------------------------------------------------------------


def test_bind_and_read_returns_the_bound_scopes_perspective(make_provider) -> None:  # noqa: ANN001
    provider = make_provider()
    assert provider.name == "strata" and provider.is_available() is True

    perspective = _call(provider, "strata_read_perspective")
    assert perspective["scope_id"] == "g_hermes"
    assert any(layer.get("scope_id") == "g_hermes" for layer in perspective["layers"])

    status = _call(provider, "strata_status")  # read-only: no tool changes the scope
    assert status["scope"] == "g_hermes" and status["skill"] == "hermes-agent"
    assert status["server"] == "up"


def test_there_is_no_bind_tool_and_the_scope_comes_from_the_profile_env(make_provider) -> None:  # noqa: ANN001
    names = [schema["name"] for schema in make_provider().get_tool_schemas()]
    assert sorted(names) == [
        "strata_contribute",
        "strata_read_perspective",
        "strata_rejudge",
        "strata_session_closeout",
        "strata_status",
    ]
    assert "strata_bind" not in names


# -- 2. contribute goes through the judge --------------------------------------------------------


def test_a_contribute_goes_through_the_judge_and_a_declined_one_is_not_in_the_summary(
    make_provider,  # noqa: ANN001
) -> None:
    provider = make_provider()
    admitted = _call(
        provider,
        "strata_contribute",
        content="The dock pumps were switched off at 20:00.",
        proposed_classification="context",
    )
    assert admitted["judgment"]["decision"] == "accept_as_context"

    declined = _call(
        provider,
        "strata_contribute",
        content="DECLINE this note about lunch.",
        proposed_classification="context",
    )
    assert declined["judgment"]["decision"] == "decline"

    summary = json.dumps(_call(provider, "strata_read_perspective"))
    assert "The dock pumps were switched off" in summary
    assert "DECLINE this note" not in summary


# -- 3. nothing writes outside strata_contribute -------------------------------------------------


def test_no_hook_names_a_write_tool_and_the_spy_sees_only_reads(
    make_provider,  # noqa: ANN001
    slow_reads,  # noqa: ANN001
) -> None:
    source = (SRC / "provider.py").read_text(encoding="utf-8")
    for tool in ("strata_contribute", "strata_session_closeout", "strata_rejudge"):
        assert tool not in source, f"provider.py must not name {tool}: only tools.py does"

    provider = make_provider()
    calls: list[str] = []
    slow_reads(provider, 0.0, calls)
    messages = [{"role": "user", "content": "hi"}]
    provider.system_prompt_block()
    provider.queue_prefetch("what is decided?", session_id="sess_h1")
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.05)
    provider.prefetch("what is decided?", session_id="sess_h1")
    provider.sync_turn("user text", "assistant text", session_id="sess_h1", messages=messages)
    provider.on_memory_write("add", "memory", "a fact", {"write_origin": "assistant"})
    provider.on_session_end(messages)
    provider.on_pre_compress(messages)
    provider.on_turn_start(1, "hi")
    provider.on_delegation("task", "result")
    provider.on_session_switch("sess_h2", reset=True)
    provider.shutdown()
    assert set(calls) <= {"strata_read_perspective"}, calls


# -- 4. unreachable or unbound: a documented error, built-in memory untouched ---------------------


def test_strata_unreachable_is_a_documented_error_and_builtin_memory_is_untouched(
    make_provider,  # noqa: ANN001
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(hermes_home)
    monkeypatch.setenv("STRATA_MCP_COMMAND", "/nonexistent/strata-mcp")
    provider = make_provider()
    assert provider.is_available() is False
    assert "strata-mcp" in provider.unavailable_reason()
    out = _call(provider, "strata_read_perspective")  # must not raise
    assert out["code"] == "strata_unreachable"
    assert "Hermes built-in memory is untouched" in out["error"]
    assert provider.prefetch("anything", session_id="sess_h1") == ""
    provider.sync_turn("u", "a", session_id="sess_h1")
    provider.shutdown()
    assert _snapshot(hermes_home) == before


def test_an_unbound_profile_is_a_documented_error(
    make_provider,  # noqa: ANN001
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(hermes_home)
    monkeypatch.delenv("STRATA_AGENT_SCOPE")
    provider = make_provider()
    assert provider.is_available() is False
    assert "STRATA_AGENT_SCOPE" in provider.unavailable_reason()
    out = _call(provider, "strata_contribute", content="x", proposed_classification="context")
    assert out["code"] == "strata_unbound" and "STRATA_AGENT_SCOPE" in out["error"]
    assert _snapshot(hermes_home) == before


def test_a_scope_strata_does_not_know_is_reported_as_unbound(
    make_provider,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_nope")
    out = _call(make_provider(), "strata_read_perspective")
    assert out["code"] == "strata_unbound"
    assert "not bound" in out["error"].lower() or "g_nope" in out["error"]


# -- 5. sync_turn does not block, and does not write -----------------------------------------------


def test_sync_turn_returns_at_once_and_does_nothing(make_provider, slow_reads) -> None:  # noqa: ANN001
    provider = make_provider()
    calls: list[str] = []
    slow_reads(provider, 2.0, calls)
    start = time.monotonic()
    result = provider.sync_turn("user", "assistant", session_id="sess_h1", messages=[])
    assert time.monotonic() - start < FAST and result is None
    time.sleep(0.3)
    assert calls == []  # no backing call at all


# -- 6. prefetch never blocks on the server ----


def test_prefetch_returns_the_cache_at_once_while_the_backing_read_is_slow(
    make_provider,  # noqa: ANN001
    slow_reads,  # noqa: ANN001
) -> None:
    provider = make_provider()
    calls: list[str] = []
    slow_reads(provider, 1.0, calls)

    start = time.monotonic()
    provider.queue_prefetch("what is decided?", session_id="sess_h1")
    assert time.monotonic() - start < FAST  # the refresh runs in the background

    start = time.monotonic()
    provider.prefetch("what is decided?", session_id="sess_h1")
    assert time.monotonic() - start < FAST  # it did not wait for the slow read
    assert calls == [] or len(calls) == 1  # the slow read is still in flight or just began

    deadline = time.time() + 6
    while not calls and time.time() < deadline:
        time.sleep(0.05)
    start = time.monotonic()
    provider.prefetch("mid-read", session_id="sess_h1")
    assert time.monotonic() - start < FAST  # still immediate while the read is running

    deadline = time.time() + 6
    text = ""
    while "g_hermes" not in text and time.time() < deadline:
        time.sleep(0.1)
        text = provider.prefetch("again", session_id="sess_h1")
    assert "g_hermes" in text  # the perspective is served from the cache
    assert set(calls) == {"strata_read_perspective"}


# -- 7. the hooks that must stay no-ops are inherited, not overridden ----


@pytest.mark.parametrize(
    "hook",
    [
        "sync_turn",
        "on_memory_write",
        "on_session_end",
        "on_pre_compress",
        "on_delegation",
        "on_turn_start",
        "on_session_switch",
        "on_profile_created",
        "on_profile_renamed",
        "read_config_fields",
        "write_config_fields",
        "backup_paths",
    ],
)
def test_the_hook_is_the_abcs_own_no_op(hook: str) -> None:
    from agent.memory_provider import MemoryProvider  # noqa: PLC0415

    from strata_hermes.provider import StrataMemoryProvider  # noqa: PLC0415

    assert getattr(StrataMemoryProvider, hook) is getattr(MemoryProvider, hook), (
        f"{hook} must not be overridden: automatic transcript sync would bypass the judge"
    )


def test_the_abstract_members_are_implemented() -> None:
    from strata_hermes.provider import StrataMemoryProvider  # noqa: PLC0415

    StrataMemoryProvider()  # an ABC with a missing abstract member would raise TypeError


# -- the adapter imports nothing from Strata ----


def test_the_provider_imports_nothing_from_strata() -> None:
    code = (
        "import sys, strata_hermes, strata_hermes.provider;"
        "bad = [m for m in sys.modules if m == 'strata' or m.startswith('strata.')];"
        "print(bad); sys.exit(1 if bad else 0)"
    )
    paths = os.pathsep.join([str(SRC.parent), str(Path(__file__).parent / "fake_hermes")])
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": paths},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
