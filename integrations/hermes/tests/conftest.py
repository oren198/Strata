"""Fixtures for the strata-hermes tests. They run without Hermes installed.

``tests/fake_hermes`` holds a verbatim copy of Hermes's ``agent/memory_provider.py`` at the pinned
commit (see its header). The Strata side is a REAL ``strata-mcp`` stdio server started by
``fake_strata_server.py`` with a scripted judge; the provider under test never imports Strata.
"""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
REPO_SRC = TESTS.parents[2] / "src"  # the Strata engine the fake server imports
SERVER = TESTS / "fake_strata_server.py"

_FLEET = """\
strata:
  - id: L0
    name: Org
    ordinal: 0
scopes:
  - id: g_hermes
    name: Hermes
    stratum_id: L0
edges: []
"""


@pytest.fixture
def hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "MEMORY.md").write_text("Hermes built-in memory.\n", encoding="utf-8")
    (home / "USER.md").write_text("About the user.\n", encoding="utf-8")
    return home


@pytest.fixture
def strata_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The profile env a Hermes profile would carry, pointed at a scratch Strata store."""
    store = tmp_path / "store"
    store.mkdir()
    (store / "fleet.yaml").write_text(_FLEET, encoding="utf-8")
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_hermes")
    monkeypatch.setenv("STRATA_AGENT_SKILL", "hermes-agent")
    monkeypatch.delenv("STRATA_AGENT_SESSION_ID", raising=False)
    monkeypatch.setenv(
        "STRATA_MCP_COMMAND", f"{shlex.quote(sys.executable)} {shlex.quote(str(SERVER))}"
    )
    monkeypatch.setenv("STRATA_FLEET_CONFIG", str(store / "fleet.yaml"))
    monkeypatch.setenv("STRATA_DB_PATH", str(store / "strata.db"))
    monkeypatch.setenv("STRATA_SUMMARIES_DIR", str(store / "summaries"))
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(REPO_SRC), os.environ.get("PYTHONPATH", "")])
    )
    monkeypatch.chdir(store)  # no project .strata/ is walked up to
    return store


@pytest.fixture
def make_provider(strata_env: Path, hermes_home: Path):  # noqa: ANN201
    """Build and initialize a provider the way Hermes's MemoryManager does."""
    from strata_hermes.provider import StrataMemoryProvider  # noqa: PLC0415

    made: list[StrataMemoryProvider] = []

    def build(**init_kwargs) -> StrataMemoryProvider:  # noqa: ANN003
        provider = StrataMemoryProvider()
        provider.initialize("sess_h1", hermes_home=str(hermes_home), platform="cli", **init_kwargs)
        made.append(provider)
        return provider

    yield build
    for provider in made:
        provider.shutdown()


@pytest.fixture
def slow_reads(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Make the provider's backing call slow, recording every tool name it is asked for."""
    import time  # noqa: PLC0415

    def install(provider, delay: float, calls: list[str]) -> None:  # noqa: ANN001
        real = provider._client.call_tool  # noqa: SLF001

        def slow(name: str, args: dict | None = None, **kwargs):  # noqa: ANN003, ANN202
            calls.append(name)
            time.sleep(delay)
            return real(name, args or {}, **kwargs)

        monkeypatch.setattr(provider._client, "call_tool", slow)  # noqa: SLF001

    return install
