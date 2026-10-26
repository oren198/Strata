"""v1.20 item 6: the binding and the session survive an MCP reconnect.

Unit tests of `binding_persist` and `record_connect`, the server's restore step, and a real
two-process reconnect (the same harness pid starts a second server). No network."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

import strata
from strata import binding_persist as bp
from strata.fleet_config import FleetConfig
from strata.mcp import server
from strata.migrator import run_migrations
from strata.session_state import SessionStateStore, session_id_is_auto

SID = "sess_auto_4242"
TOKEN = "4242:99001"


def _ok(scope: str, skill: str | None) -> tuple[str | None, list[str]]:
    return skill, []


def _bad(scope: str, skill: str | None) -> tuple[str | None, list[str]]:
    return None, [f"scope {scope!r} is not in the fleet"]


# --- persistence ---------------------------------------------------------------------------


def test_a_bind_is_written_atomically_with_mode_0600(tmp_path: Path) -> None:
    assert bp.write_binding(tmp_path, SID, TOKEN, "g_a", "s1")
    path = bp.binding_path(tmp_path, SID)
    assert path.name == f"{SID}.binding" and not list(tmp_path.glob("*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    body = json.loads(path.read_text(encoding="utf-8"))
    assert (body["session_id"], body["harness_token"], body["scope_id"]) == (SID, TOKEN, "g_a")
    assert list(tmp_path.glob("*.json")) == []  # invisible to the session-state scans


def test_two_binds_in_a_row_leave_the_last(tmp_path: Path) -> None:
    bp.write_binding(tmp_path, SID, TOKEN, "g_a", None)
    bp.write_binding(tmp_path, SID, TOKEN, "g_b", "s2")
    r = bp.restore_binding(tmp_path, SID, TOKEN, _ok)
    assert (r.status, r.scope_id, r.skill) == ("restored", "g_b", "s2")


def test_an_empty_token_writes_and_restores_nothing(tmp_path: Path) -> None:
    assert bp.write_binding(tmp_path, SID, "", "g_a", None) is False
    assert not bp.binding_path(tmp_path, SID).exists()
    bp.write_binding(tmp_path, SID, TOKEN, "g_a", None)
    assert bp.restore_binding(tmp_path, SID, "", _ok).status == "none"
    assert bp.binding_path(tmp_path, SID).exists()


def test_a_different_harness_token_is_ignored_and_the_file_deleted(tmp_path: Path) -> None:
    bp.write_binding(tmp_path, SID, TOKEN, "g_a", None)
    assert bp.restore_binding(tmp_path, SID, "4242:99002", _ok).status == "none"
    assert not bp.binding_path(tmp_path, SID).exists()


def test_a_file_whose_inner_session_id_differs_is_ignored_and_deleted(tmp_path: Path) -> None:
    bp.write_binding(tmp_path, "sess_auto_1", TOKEN, "g_a", None)
    bp.binding_path(tmp_path, "sess_auto_1").rename(bp.binding_path(tmp_path, SID))
    assert bp.restore_binding(tmp_path, SID, TOKEN, _ok).status == "none"
    assert not bp.binding_path(tmp_path, SID).exists()


def test_a_corrupt_file_is_ignored_and_deleted(tmp_path: Path) -> None:
    bp.binding_path(tmp_path, SID).write_text("{not json", encoding="utf-8")
    assert bp.restore_binding(tmp_path, SID, TOKEN, _ok).status == "none"
    assert not bp.binding_path(tmp_path, SID).exists()


def test_a_binding_that_no_longer_validates_is_reported_and_deleted(tmp_path: Path) -> None:
    bp.write_binding(tmp_path, SID, TOKEN, "g_gone", None)
    r = bp.restore_binding(tmp_path, SID, TOKEN, _bad)
    assert r.status == "invalid"
    assert "your earlier binding to 'g_gone' no longer holds" in r.message
    assert not bp.binding_path(tmp_path, SID).exists()


def test_a_config_class_error_blocks_the_restore_and_keeps_the_file(tmp_path: Path) -> None:
    bp.write_binding(tmp_path, SID, TOKEN, "g_a", None)
    r = bp.restore_binding(tmp_path, SID, TOKEN, _ok, config_errors=["fleet.yaml is invalid"])
    assert r.status == "blocked"
    assert bp.binding_path(tmp_path, SID).exists()


def test_binding_files_older_than_30_days_are_deleted(tmp_path: Path) -> None:
    bp.write_binding(tmp_path, "sess_auto_1", TOKEN, "g_a", None)
    bp.write_binding(tmp_path, "sess_auto_2", TOKEN, "g_a", None)
    old = bp.binding_path(tmp_path, "sess_auto_1")
    t = old.stat().st_mtime - bp.STALE_AFTER_SECONDS - 10
    os.utime(old, (t, t))
    assert bp.delete_stale(tmp_path) == 1
    assert not old.exists() and bp.binding_path(tmp_path, "sess_auto_2").exists()


def test_the_token_is_pid_and_start_time_and_is_stable() -> None:
    token = bp.harness_token(os.getpid())
    if os.name == "nt":
        assert token == ""
        return
    assert token.startswith(f"{os.getpid()}:") and token == bp.harness_token(os.getpid())
    assert bp.harness_token(2**22 + 12345) == ""  # no such process


def test_an_explicit_session_id_is_not_auto() -> None:
    assert session_id_is_auto({}) and session_id_is_auto({"STRATA_AGENT_SESSION_ID": ""})
    assert not session_id_is_auto({"STRATA_AGENT_SESSION_ID": "mine"})


# --- session continuity ----------------------------------------------------------------------


def test_a_reconnect_under_the_same_harness_keeps_the_record_and_counters(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path)
    store.record_connect(SID, pid=100, harness_token=TOKEN)
    store.record_read(SID, "g_a", watermark="w1")
    store.record_end(SID, pid=100)
    state = store.record_connect(SID, pid=200, harness_token=TOKEN)
    assert state.server_pid == 200 and state.ended_at == "" and state.reconnects == 1
    assert state.reads == 1
    assert list(tmp_path.glob("*.rolled-*.json")) == []
    assert len(store.all_states()) == 1


def test_a_different_harness_token_rolls_the_session_over(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path)
    store.record_connect(SID, pid=100, harness_token=TOKEN)
    state = store.record_connect(SID, pid=200, harness_token="4242:99002")
    assert state.reconnects == 0 and len(list(tmp_path.glob("*.rolled-*.json"))) == 1


def test_an_empty_token_rolls_the_session_over_as_before(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path)
    store.record_connect(SID, pid=100)
    store.record_connect(SID, pid=200)
    assert len(list(tmp_path.glob("*.rolled-*.json"))) == 1


def test_the_old_servers_late_exit_does_not_end_the_reconnected_session(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path)
    store.record_connect(SID, pid=100, harness_token=TOKEN)
    store.record_connect(SID, pid=200, harness_token=TOKEN)
    store.record_end(SID, pid=100)
    assert store.read(SID).ended_at == ""


# --- the server's restore step ---------------------------------------------------------------


@pytest.fixture
def srv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # noqa: ANN201
    monkeypatch.setattr(server, "_sessions_dir", str(tmp_path))
    monkeypatch.setattr(server, "_AGENT_SESSION_ID", SID)
    monkeypatch.setattr(server, "_HARNESS_TOKEN", TOKEN)
    monkeypatch.setattr(server, "_AGENT_SCOPE", "")
    monkeypatch.setattr(server, "_AGENT_SKILL", None)
    monkeypatch.setattr(server, "_UNRESOLVED", True)
    monkeypatch.setattr(server, "_STARTUP_ERRORS_CONFIG", [])
    monkeypatch.setattr(server, "_STARTUP_ERRORS_BINDING", ["not bound"])
    return server


def _fleet() -> FleetConfig:
    return FleetConfig.model_validate(
        {
            "strata": [{"id": "L0", "name": "root", "ordinal": 0}],
            "scopes": [
                {"id": "g_a", "name": "A", "stratum_id": "L0"},
                {"id": "g_b", "name": "B", "stratum_id": "L0", "permitted_skills": ["s1"]},
            ],
            "edges": [],
        }
    )


def test_restore_rebinds_the_confirmed_scope(srv, tmp_path: Path) -> None:  # noqa: ANN001
    bp.write_binding(tmp_path, SID, TOKEN, "g_b", "s1")
    srv._restore_persisted_binding(_fleet())
    assert (srv._AGENT_SCOPE, srv._AGENT_SKILL) == ("g_b", "s1")
    assert srv._UNRESOLVED is False and srv._STARTUP_ERRORS_BINDING == []


def test_a_scope_removed_from_the_fleet_comes_up_not_bound_with_the_reason(
    srv,
    tmp_path: Path,  # noqa: ANN001
) -> None:
    bp.write_binding(tmp_path, SID, TOKEN, "g_gone", None)
    srv._restore_persisted_binding(_fleet())
    assert srv._UNRESOLVED is True and srv._AGENT_SCOPE == ""
    assert "your earlier binding to 'g_gone' no longer holds" in srv._STARTUP_ERRORS_BINDING[0]
    assert not bp.binding_path(tmp_path, SID).exists()


def test_a_skill_no_longer_permitted_comes_up_not_bound(srv, tmp_path: Path) -> None:  # noqa: ANN001
    bp.write_binding(tmp_path, SID, TOKEN, "g_b", "retired_skill")
    srv._restore_persisted_binding(_fleet())
    assert srv._UNRESOLVED is True and srv._AGENT_SCOPE == ""
    assert "no longer holds" in srv._STARTUP_ERRORS_BINDING[0]


def test_a_broken_fleet_blocks_the_restore(srv, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(srv, "_STARTUP_ERRORS_CONFIG", ["fleet.yaml is invalid"])
    bp.write_binding(tmp_path, SID, TOKEN, "g_b", "s1")
    srv._restore_persisted_binding(None)
    assert srv._AGENT_SCOPE == "" and srv._UNRESOLVED is True
    assert bp.binding_path(tmp_path, SID).exists()


def test_no_token_means_no_restore(srv, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    bp.write_binding(tmp_path, SID, TOKEN, "g_b", "s1")
    monkeypatch.setattr(srv, "_HARNESS_TOKEN", "")
    srv._restore_persisted_binding(_fleet())
    assert srv._AGENT_SCOPE == "" and srv._UNRESOLVED is True


def test_persist_writes_only_a_bound_session(srv, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    srv._persist_binding()  # unresolved: nothing
    assert not bp.binding_path(tmp_path, SID).exists()
    monkeypatch.setattr(srv, "_AGENT_SCOPE", "g_a")
    monkeypatch.setattr(srv, "_UNRESOLVED", False)
    srv._persist_binding()
    assert bp.binding_path(tmp_path, SID).exists()


def test_the_key_reads_no_environment_beyond_the_session_id_rule() -> None:
    text = Path(bp.__file__).read_text(encoding="utf-8")
    assert "environ" not in text and "getenv" not in text


# --- a real reconnect ------------------------------------------------------------------------


def _project(root: Path) -> None:
    d = root / ".strata"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(
        'db = ".strata/strata.db"\nfleet_yaml = ".strata/fleet.yaml"\n'
        'summaries_dir = ".strata/summaries"\n',
        encoding="utf-8",
    )
    (d / "fleet.yaml").write_text(
        yaml.dump(
            {
                "strata": [{"id": "L0", "name": "root", "ordinal": 0}],
                "scopes": [
                    {"id": "g_a", "name": "A", "stratum_id": "L0"},
                    {"id": "g_b", "name": "B", "stratum_id": "L0"},
                ],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    (d / "summaries").mkdir(exist_ok=True)
    run_migrations(str(d / "strata.db"))


def _params(root: Path, session_id: str = "") -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "strata.mcp.server"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(strata.__file__).resolve().parent.parent),
            "HOME": str(root),
            "STRATA_AGENT_SCOPE": "",
            "STRATA_AGENT_SESSION_ID": session_id,
        },
        cwd=str(root),
    )


async def _connect(root: Path, calls: list[tuple[str, dict]], session_id: str = "") -> list[dict]:
    out: list[dict] = []
    async with (
        stdio_client(_params(root, session_id)) as (read, write),
        ClientSession(
            read, write, client_info=types.Implementation(name="claude-code", version="0")
        ) as session,
    ):
        await session.initialize()
        for name, args in calls:
            result = await session.call_tool(name, args)
            text = result.content[0].text
            try:
                out.append(json.loads(text))
            except ValueError:  # an unbound error is plain text
                out.append({"error": text})
    return out


def test_a_real_reconnect_keeps_the_binding_and_the_session(tmp_path: Path) -> None:
    if not bp.harness_token():
        pytest.skip("no process start time on this platform")
    _project(tmp_path)
    first = asyncio.run(_connect(tmp_path, [("strata_bind", {"scope_id": "g_b"})]))
    assert "Bound to 'g_b'" in first[0]["message"]
    second = asyncio.run(
        _connect(tmp_path, [("strata_read_perspective", {}), ("strata_session_stats", {})])
    )
    assert "error" not in second[0] and second[0].get("scope_id", "g_b") == "g_b"
    sessions = tmp_path / ".strata" / "sessions"
    assert len(list(sessions.glob("*.json"))) == 1  # one session record, no .rolled-
    state = json.loads(next(sessions.glob("*.json")).read_text(encoding="utf-8"))
    assert state["reconnects"] == 1


def test_an_explicit_session_id_writes_and_restores_nothing(tmp_path: Path) -> None:
    _project(tmp_path)
    asyncio.run(_connect(tmp_path, [("strata_bind", {"scope_id": "g_b"})], session_id="mine"))
    assert list((tmp_path / ".strata" / "sessions").glob("*.binding")) == []
    again = asyncio.run(_connect(tmp_path, [("strata_read_perspective", {})], session_id="mine"))
    assert "strata_bind" in json.dumps(again[0])  # still unbound: the error asks for a bind
