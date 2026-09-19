"""Real-subprocess check of per-session identity (M1).

Two real ``strata-mcp`` server processes, each launched by its own stand-in
"harness" process (so each server's parent pid differs, exactly as with two
Codex or Claude Code sessions), with ``STRATA_AGENT_SESSION_ID`` empty. Each is
driven over stdio as an MCP client: they must resolve to different session ids,
keep their own counters and state files, keep one id across every tool call,
and record the harness the client named in its ``initialize`` handshake.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

import strata
from strata.migrator import run_migrations

# The stand-in harness: it is the server's parent process, and the server
# inherits its stdio, so the MCP client talks straight through to the server.
_HARNESS_LAUNCHER = (
    "import subprocess, sys; sys.exit(subprocess.call([sys.executable, '-m', 'strata.mcp.server']))"
)


def _project(root: Path) -> None:
    """Lay out a scratch project the way ``strata register`` does: a
    ``.strata/`` directory with config.toml, fleet.yaml and a migrated db."""
    strata_dir = root / ".strata"
    strata_dir.mkdir(parents=True, exist_ok=True)
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
                "scopes": [{"id": "g_root", "name": "Root", "stratum_id": "L0"}],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    (strata_dir / "summaries").mkdir(exist_ok=True)
    run_migrations(str(strata_dir / "strata.db"))


def _server_params(tmp_path: Path, session_id: str) -> StdioServerParameters:
    _project(tmp_path)
    env = {
        "PATH": os.environ.get("PATH", ""),
        # Import the strata under test, not whatever an editable install points at.
        "PYTHONPATH": str(Path(strata.__file__).resolve().parent.parent),
        "HOME": str(tmp_path),
        "STRATA_AGENT_SCOPE": "g_root",
        "STRATA_AGENT_SESSION_ID": session_id,
    }
    return StdioServerParameters(
        command=sys.executable, args=["-c", _HARNESS_LAUNCHER], env=env, cwd=str(tmp_path)
    )


def _payload(result: types.CallToolResult) -> dict:
    return json.loads(result.content[0].text)  # type: ignore[union-attr]


async def _drive(params: StdioServerParameters, client_name: str, reads: int) -> dict:
    """Connect as *client_name*, read ``reads`` times, and return what each call saw."""
    async with (
        stdio_client(params) as (read, write),
        ClientSession(
            read, write, client_info=types.Implementation(name=client_name, version="0")
        ) as session,
    ):
        await session.initialize()
        seen_ids = []
        for _ in range(reads):
            await session.call_tool("strata_read_perspective", {})
            seen_ids.append(
                _payload(await session.call_tool("strata_session_stats", {}))["session_id"]
            )
        stats = _payload(await session.call_tool("strata_session_stats", {}))
    return {"stats": stats, "seen_ids": seen_ids}


def test_two_concurrent_servers_resolve_distinct_session_ids(tmp_path: Path) -> None:
    """Empty STRATA_AGENT_SESSION_ID, two servers with different parent pids, one
    shared store: each session keeps its own counters and its own state file."""
    store_root = tmp_path / "shared"
    store_root.mkdir()
    params_a = _server_params(store_root, "")
    params_b = _server_params(store_root, "")

    async def both() -> tuple[dict, dict]:
        return await asyncio.gather(
            _drive(params_a, "codex-mcp-client", reads=1),
            _drive(params_b, "claude-code", reads=1),
        )

    a, b = asyncio.run(both())

    assert a["stats"]["session_id"].startswith("sess_auto_")
    assert b["stats"]["session_id"].startswith("sess_auto_")
    assert a["stats"]["session_id"] != b["stats"]["session_id"]
    assert a["stats"]["reads"] == 1
    assert b["stats"]["reads"] == 1

    files = sorted((store_root / ".strata" / "sessions").glob("*.json"))
    assert len(files) == 2
    harness_by_session = {
        json.loads(f.read_text(encoding="utf-8"))["session_id"]: json.loads(
            f.read_text(encoding="utf-8")
        )["harness"]
        for f in files
    }
    assert harness_by_session[a["stats"]["session_id"]] == "codex"
    assert harness_by_session[b["stats"]["session_id"]] == "claude-code"


def test_session_id_is_stable_across_tool_calls_within_one_server(tmp_path: Path) -> None:
    result = asyncio.run(_drive(_server_params(tmp_path, ""), "codex-mcp-client", reads=3))

    assert len(set(result["seen_ids"])) == 1
    assert result["stats"]["session_id"] == result["seen_ids"][0]
    assert result["stats"]["reads"] == 3


def test_explicit_session_id_still_wins(tmp_path: Path) -> None:
    result = asyncio.run(_drive(_server_params(tmp_path, "my-explicit-id"), "codex-mcp-client", 1))

    assert result["stats"]["session_id"] == "my-explicit-id"
    assert (tmp_path / ".strata" / "sessions" / "my-explicit-id.json").exists()


def test_connect_alone_records_the_session_before_any_tool_call(tmp_path: Path) -> None:
    """The write-back denominator: a server that gets initialize and exits with
    no tool call still leaves a state file — harness set, counters zero."""

    async def connect_only() -> None:
        params = _server_params(tmp_path, "")
        async with (
            stdio_client(params) as (read, write),
            ClientSession(
                read, write, client_info=types.Implementation(name="codex-mcp-client", version="0")
            ) as session,
        ):
            await session.initialize()

    asyncio.run(connect_only())

    files = list((tmp_path / ".strata" / "sessions").glob("*.json"))
    assert len(files) == 1
    state = json.loads(files[0].read_text(encoding="utf-8"))
    assert state["harness"] == "codex"
    assert state["connected_at"] != ""
    assert (state["reads"], state["contributions"], state["submitted"], state["declines"]) == (
        0,
        0,
        0,
        0,
    )


# ---------------------------------------------------------------------------
# M3 — session end and rollover, driven against a bare server process (no
# launcher in between, so the test owns the pid and can close stdin or signal it).
# ---------------------------------------------------------------------------

_INITIALIZE = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "codex-mcp-client", "version": "0"},
        },
    }
)
_INITIALIZED = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})


def _spawn_server(project: Path, session_id: str) -> subprocess.Popen:
    """Start ``strata.mcp.server`` directly, complete the handshake, and return it
    once its session-state file exists."""
    params = _server_params(project, session_id)
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "strata.mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=params.env,
        cwd=str(project),
        text=True,
    )
    assert proc.stdin is not None
    proc.stdin.write(_INITIALIZE + "\n" + _INITIALIZED + "\n")
    proc.stdin.flush()
    sessions = project / ".strata" / "sessions"
    _wait_for(lambda: any(sessions.glob("*.json")))
    return proc


def _wait_for(predicate, timeout: float = 15.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def _states(project: Path) -> list[dict]:
    return [
        json.loads(f.read_text(encoding="utf-8"))
        for f in sorted((project / ".strata" / "sessions").glob("*.json"))
    ]


def test_closing_stdin_stamps_ended_at(tmp_path: Path) -> None:
    proc = _spawn_server(tmp_path, "")
    assert proc.stdin is not None
    proc.stdin.close()
    proc.wait(timeout=15)

    (state,) = _states(tmp_path)
    assert state["ended_at"] != ""
    assert state["server_pid"] == proc.pid


def test_sigterm_stamps_ended_at(tmp_path: Path) -> None:
    proc = _spawn_server(tmp_path, "")
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=15)

    (state,) = _states(tmp_path)
    assert state["ended_at"] != ""


def test_a_killed_server_counts_as_ended_only_after_the_idle_window(tmp_path: Path) -> None:
    from strata.session_state import SessionStateStore, compute_writeback_report

    proc = _spawn_server(tmp_path, "")
    proc.kill()
    proc.wait(timeout=15)

    (state,) = _states(tmp_path)
    assert state["ended_at"] == ""  # SIGKILL: nothing ran to stamp it
    store = SessionStateStore(tmp_path / ".strata" / "sessions")
    idle = timedelta(seconds=30)
    connected = datetime.fromisoformat(state["connected_at"])

    inside = compute_writeback_report(store, idle_window=idle, now=connected + timedelta(seconds=5))
    outside = compute_writeback_report(
        store, idle_window=idle, now=connected + timedelta(seconds=60)
    )

    assert (inside.overall.n, inside.open_excluded) == (0, 1)
    assert (outside.overall.n, outside.open_excluded) == (1, 0)


def test_two_sequential_servers_with_the_same_explicit_id_are_two_sessions(tmp_path: Path) -> None:
    from strata.session_state import SessionStateStore, compute_writeback_report

    for _ in range(2):
        proc = _spawn_server(tmp_path, "shared-id")
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=15)

    store = SessionStateStore(tmp_path / ".strata" / "sessions")
    report = compute_writeback_report(store)

    assert report.overall.n == 2
    assert report.open_excluded == 0
    assert len(list((tmp_path / ".strata" / "sessions").glob("shared-id*.json"))) == 2
