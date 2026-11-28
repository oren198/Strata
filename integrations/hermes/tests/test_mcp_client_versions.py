"""The client against a REAL ``strata-mcp`` from a client venv on mcp 2.3.0 and on 1.29.x.

Needs venvs, so it runs only when ``STRATA_MCP_VENVS`` names a directory holding
``c<mcp version>/`` client venvs (mcp only) and ``eng/`` (strata-mem installed); ``scripts/``
documents how to build them. No key, no model call: ``strata_read_perspective`` does not judge.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = os.environ.get("STRATA_MCP_VENVS", "")
pytestmark = pytest.mark.skipif(not ROOT, reason="STRATA_MCP_VENVS not set")
TESTS = Path(__file__).parent

_FLEET = """\
strata:
  - id: L0
    name: root
    ordinal: 0
scopes:
  - id: g_probe
    name: Probe
    description: "client-version probe"
    stratum_id: L0
    default_skill: probe
edges: []
"""


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    proj = tmp_path_factory.mktemp("probe_project")
    env = {"PATH": os.environ["PATH"], "HOME": str(proj)}
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True, env=env)
    subprocess.run(
        [
            str(Path(ROOT) / "eng" / "bin" / "strata"),
            "register",
            ".",
            "--harness",
            "claude-code",
            "--description",
            "client-version probe",
        ],
        cwd=proj,
        check=True,
        env=env,
        capture_output=True,
    )
    (proj / ".strata" / "fleet.yaml").write_text(_FLEET, encoding="utf-8")
    return proj


@pytest.mark.parametrize("client_mcp", ["2.3.0", "1.29.1"])
def test_the_client_talks_to_a_real_strata_mcp(client_mcp: str, project: Path) -> None:
    py = Path(ROOT) / f"c{client_mcp}" / "bin" / "python"
    if not py.exists():
        pytest.skip(f"no client venv for mcp {client_mcp}")
    done = subprocess.run(
        [
            str(py),
            "-I",
            str(TESTS / "mcp_probe.py"),
            str(TESTS),
            str(project),
            str(Path(ROOT) / "eng" / "bin" / "strata-mcp"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": os.environ["PATH"], "HOME": str(project)},
    )
    line = next((x for x in done.stdout.splitlines() if x.startswith("PROBE ")), "")
    assert line, done.stdout + done.stderr
    out = json.loads(line[6:])
    assert out["mcp"] == client_mcp
    assert out["up"] is True
    assert out["scope_id"] == "g_probe"
    assert out["unknown_tool"] == "strata_error"


def test_the_provider_starts_the_engine_even_when_the_launch_pythonpath_holds_hermes_mcp(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hermes sets PYTHONPATH to <checkout>:<its site-packages> (mcp 2.3.0). The engine, a different
    environment with mcp 1.29, must not inherit it (C10b live run 1: ModuleNotFoundError
    mcp.server.fastmcp)."""
    sites = sorted((Path(ROOT) / "c2.3.0").glob("lib/python*/site-packages"))
    if not sites:
        pytest.skip("no mcp 2.3.0 client venv")
    sys.path.insert(0, str(TESTS))
    from plugin_loader import load_plugin  # noqa: PLC0415

    monkeypatch.setenv("PYTHONPATH", f"/hermes/checkout:{sites[0]}")
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_probe")
    monkeypatch.setenv("STRATA_AGENT_SKILL", "probe")
    monkeypatch.setenv("STRATA_PROJECT_DIR", str(project))
    monkeypatch.setenv("STRATA_MCP_COMMAND", str(Path(ROOT) / "eng" / "bin" / "strata-mcp"))
    plugin = load_plugin()
    provider = plugin.StrataMemoryProvider()
    home = tmp_path / "hh"
    home.mkdir()
    try:
        provider.initialize("sess_pp", hermes_home=str(home), platform="cli")
        assert provider._init_error is None, provider._init_error  # noqa: SLF001
        reply = json.loads(provider.handle_tool_call("strata_read_perspective", {}))
        assert reply.get("scope_id") == "g_probe", reply
    finally:
        provider.shutdown()
