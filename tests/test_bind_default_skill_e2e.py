"""A freshly registered project binds with no hand edits.

`strata register` ships STRATA_AGENT_SCOPE / STRATA_AGENT_SKILL as empty strings
(Codex's config) or unset (`.mcp.json`). A scope that declares a ``default_skill``
must bind with an empty skill whether the scope was auto-bound (the fleet's only
scope) or named explicitly; found by the M4 demo runner, which named the scope
``demo`` and was refused until STRATA_AGENT_SKILL was set by hand.

Real subprocess servers driven over stdio, so the whole startup path runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import yaml
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

import strata
from strata.migrator import run_migrations

_SRC = str(Path(strata.__file__).resolve().parent.parent)


def _project(root: Path, scopes: list[dict]) -> None:
    strata_dir = root / ".strata"
    strata_dir.mkdir()
    (strata_dir / "config.toml").write_text(
        'db = ".strata/strata.db"\n'
        'fleet_yaml = ".strata/fleet.yaml"\n'
        'summaries_dir = ".strata/summaries"\n',
        encoding="utf-8",
    )
    (strata_dir / "fleet.yaml").write_text(
        yaml.dump(
            {
                "strata": [{"id": "L0", "name": "root", "ordinal": 0}],
                "scopes": [{"stratum_id": "L0", **s} for s in scopes],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    (strata_dir / "summaries").mkdir()
    run_migrations(str(strata_dir / "strata.db"))


def _read_perspective(root: Path, *, scope: str | None, skill: str | None) -> str:
    """Start a real server with the given (possibly empty/unset) identity env, call
    strata_read_perspective, and return the tool's text."""
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": _SRC, "HOME": str(root)}
    if scope is not None:
        env["STRATA_AGENT_SCOPE"] = scope
    if skill is not None:
        env["STRATA_AGENT_SKILL"] = skill
    env["STRATA_AGENT_SESSION_ID"] = ""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "strata.mcp.server"], env=env, cwd=str(root)
    )

    async def run() -> str:
        async with (
            stdio_client(params) as (read, write),
            ClientSession(
                read, write, client_info=types.Implementation(name="test", version="0")
            ) as session,
        ):
            await session.initialize()
            result = await session.call_tool("strata_read_perspective", {})
            return "".join(c.text for c in result.content if hasattr(c, "text"))

    return asyncio.run(run())


def _bound(text: str, scope_id: str) -> bool:
    try:
        return json.loads(text)["scope_id"] == scope_id
    except (ValueError, KeyError, TypeError):
        return False


def test_a_single_scope_with_a_default_skill_auto_binds_with_an_empty_skill(
    tmp_path: Path,
) -> None:
    _project(tmp_path, [{"id": "g_root", "name": "Root", "default_skill": "strata-worker"}])

    text = _read_perspective(tmp_path, scope="", skill="")

    assert _bound(text, "g_root"), text


def test_an_explicitly_named_scope_with_a_default_skill_binds_with_an_empty_skill(
    tmp_path: Path,
) -> None:
    _project(tmp_path, [{"id": "demo", "name": "Demo", "default_skill": "strata-worker"}])

    text = _read_perspective(tmp_path, scope="demo", skill="")

    assert _bound(text, "demo"), text


def test_an_explicit_scope_binds_with_the_skill_env_var_absent_too(tmp_path: Path) -> None:
    _project(tmp_path, [{"id": "demo", "name": "Demo", "default_skill": "strata-worker"}])

    text = _read_perspective(tmp_path, scope="demo", skill=None)

    assert _bound(text, "demo"), text


def test_in_a_multi_scope_fleet_an_explicit_scope_still_uses_its_default_skill(
    tmp_path: Path,
) -> None:
    _project(
        tmp_path,
        [
            {"id": "demo", "name": "Demo", "default_skill": "strata-worker"},
            {"id": "other", "name": "Other"},
        ],
    )

    text = _read_perspective(tmp_path, scope="demo", skill="")

    assert _bound(text, "demo"), text


def test_a_scope_with_only_permitted_skills_still_needs_an_explicit_skill(
    tmp_path: Path,
) -> None:
    """No default to fall back on: the agent must choose, and the error says so."""
    _project(
        tmp_path,
        [{"id": "demo", "name": "Demo", "permitted_skills": ["strata-worker", "strata-inspect"]}],
    )

    text = _read_perspective(tmp_path, scope="demo", skill="")

    assert not _bound(text, "demo")
    assert "skill" in text.lower()


# ---------------------------------------------------------------------------
# End to end from `strata register`: run register for real, then start the MCP
# server with EXACTLY the command and env the seeded config hands each harness,
# and make a real tool call that binds. No hand edits in between.
# ---------------------------------------------------------------------------


def _register(project: Path, monkeypatch, harness: str) -> Path:
    """Run `strata register --harness <h>` in *project*; return CODEX_HOME."""
    from strata.__main__ import _build_parser, cmd_register

    codex_home = project / "codex_home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("HOME", str(project))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    for var in ("JUDGE_API_KEY", "ANTHROPIC_API_KEY", "STRATA_JUDGE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    (project / ".git").mkdir(exist_ok=True)
    args = _build_parser().parse_args(["register", str(project), "--harness", harness])
    assert cmd_register(args) == 0
    return codex_home


def _seeded_server_params(
    project: Path, harness: str, codex_home: Path, extra_env: dict[str, str] | None = None
) -> StdioServerParameters:
    """The command and env the harness's config gives the server, verbatim."""
    if harness == "claude-code":
        entry = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
            "strata"
        ]
        command, seeded_env = entry["command"], dict(entry.get("env", {}))
    else:
        import tomllib

        server = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))[
            "mcp_servers"
        ]["strata"]
        command, seeded_env = server["command"], dict(server.get("env", {}))
    # `strata-mcp` on a real machine is the pipx entry point; here a shim on PATH runs
    # this checkout's server, so the seeded command is used unchanged.
    bin_dir = project / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / command
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m strata.mcp.server "$@"\n')
    shim.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": _SRC,
        "HOME": str(project),
        **seeded_env,
        **(extra_env or {}),
    }
    return StdioServerParameters(command=command, args=[], env=env, cwd=str(project))


def _call_read_perspective(params: StdioServerParameters) -> str:
    async def run() -> str:
        async with (
            stdio_client(params) as (read, write),
            ClientSession(
                read, write, client_info=types.Implementation(name="test", version="0")
            ) as session,
        ):
            await session.initialize()
            result = await session.call_tool("strata_read_perspective", {})
            return "".join(c.text for c in result.content if hasattr(c, "text"))

    return asyncio.run(run())


import pytest  # noqa: E402


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_a_freshly_registered_project_binds_with_no_hand_edits(
    tmp_path: Path, monkeypatch, harness: str
) -> None:
    codex_home = _register(tmp_path, monkeypatch, harness)

    text = _call_read_perspective(_seeded_server_params(tmp_path, harness, codex_home))

    assert _bound(text, "g_root"), text


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_a_registered_project_binds_when_the_scope_is_named_explicitly(
    tmp_path: Path, monkeypatch, harness: str
) -> None:
    """The M4 runner's case: the operator names the scope; the seeded config still
    leaves the skill empty, and the scope's default_skill must fill it."""
    codex_home = _register(tmp_path, monkeypatch, harness)

    text = _call_read_perspective(
        _seeded_server_params(
            tmp_path, harness, codex_home, extra_env={"STRATA_AGENT_SCOPE": "g_root"}
        )
    )

    assert _bound(text, "g_root"), text
