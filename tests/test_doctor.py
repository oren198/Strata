"""Tests for `strata doctor` (Task 2.1, local-launch-bar plan).

`strata doctor` diagnoses a project's Strata wiring entirely offline — it
reads files, opens the SQLite DB directly, and inspects fleet.yaml/env
vars. It never makes an HTTP request; no backend needs to be running.

Each check prints one line with a pass/fail glyph. Every failure line names
the fix. The command exits 0 when every check passes, 1 otherwise.

Checks covered, one test (at least) per check, each with exactly one thing
broken in an otherwise fully-registered project:

1. project config resolvable
2. DB reachable and migrated
3. fleet.yaml valid
4. MCP server entry present in .claude/settings.json
5. Stop hook script present and matching shipped
6. hooks.Stop entry present
7. skills present
8. binding env vars (STRATA_AGENT_SCOPE / _SKILL / _SESSION_ID) set and
   valid against the fleet

Vocabulary: scope, fleet, skill, scope-manager.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.__main__ import cmd_doctor, cmd_register

# ---------------------------------------------------------------------------
# Fixture: a fully-registered, fully-bound project — every check passes.
# ---------------------------------------------------------------------------


@pytest.fixture
def registered_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> Path:
    """A project registered end-to-end: wiring on disk, DB migrated, env bound.

    All eight `strata doctor` checks pass against the returned directory.
    """
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)

    register_args = argparse.Namespace(path=str(tmp_path), diff=False, bootstrap_venv=False)
    rc = cmd_register(register_args)
    assert rc == 0, "fixture setup: strata register must succeed"

    from strata.migrator import run_migrations

    run_migrations(str(tmp_path / ".strata" / "strata.db"))

    # The seeded minimal.yaml fleet has one scope, g_root, with
    # default_skill: strata-worker (see src/strata/_templates/minimal.yaml).
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_root")
    monkeypatch.setenv("STRATA_AGENT_SKILL", "strata-worker")
    monkeypatch.setenv("STRATA_AGENT_SESSION_ID", "sess_test")

    capsys.readouterr()  # discard strata register's own output
    return tmp_path


def _run_doctor(capsys: pytest.CaptureFixture) -> tuple[int, str]:
    """Run cmd_doctor and return (exit_code, combined stdout+stderr)."""
    rc = cmd_doctor(argparse.Namespace())
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


# ---------------------------------------------------------------------------
# Baseline: a fully-wired project passes every check.
# ---------------------------------------------------------------------------


def test_doctor_all_checks_pass(registered_project: Path, capsys: pytest.CaptureFixture) -> None:
    rc, output = _run_doctor(capsys)

    assert rc == 0
    lower = output.lower()
    assert "project config" in lower
    assert "database" in lower
    assert "fleet" in lower
    assert "mcp" in lower
    assert "stop hook" in lower
    assert "skill" in lower
    assert "binding" in lower
    assert "session id" in lower


# ---------------------------------------------------------------------------
# 1. Project config resolvable.
# ---------------------------------------------------------------------------


def test_doctor_flags_bad_project_config(
    registered_project: Path, capsys: pytest.CaptureFixture
) -> None:
    config_toml = registered_project / ".strata" / "config.toml"
    config_toml.write_text("this is not valid toml ][=", encoding="utf-8")

    rc, output = _run_doctor(capsys)

    assert rc == 1
    lower = output.lower()
    assert "config" in lower
    # Every failure line must say how to fix it.
    assert "register" in lower or "config.toml" in lower


# ---------------------------------------------------------------------------
# 2. DB reachable and migrated.
# ---------------------------------------------------------------------------


def test_doctor_flags_unreachable_db(
    registered_project: Path, capsys: pytest.CaptureFixture
) -> None:
    db_path = registered_project / ".strata" / "strata.db"
    db_path.unlink()
    db_path.mkdir()  # a directory where sqlite3 expects a file: guaranteed open failure

    rc, output = _run_doctor(capsys)

    assert rc == 1
    lower = output.lower()
    assert "database" in lower


# ---------------------------------------------------------------------------
# 3. fleet.yaml valid.
# ---------------------------------------------------------------------------


def test_doctor_flags_missing_fleet_yaml(
    registered_project: Path, capsys: pytest.CaptureFixture
) -> None:
    (registered_project / ".strata" / "fleet.yaml").unlink()

    rc, output = _run_doctor(capsys)

    assert rc == 1
    lower = output.lower()
    assert "fleet" in lower
    assert "register" in lower


def test_doctor_flags_invalid_fleet_yaml(
    registered_project: Path, capsys: pytest.CaptureFixture
) -> None:
    fleet_yaml = registered_project / ".strata" / "fleet.yaml"
    # Violates the fleet-config invariant that every scope references a
    # declared stratum_id.
    fleet_yaml.write_text(
        "strata: []\nscopes:\n  - id: g_root\n    name: Root\n    stratum_id: L0\nedges: []\n",
        encoding="utf-8",
    )

    rc, output = _run_doctor(capsys)

    assert rc == 1
    assert "fleet" in output.lower()


# ---------------------------------------------------------------------------
# 4. MCP server entry present.
# ---------------------------------------------------------------------------


def test_doctor_flags_missing_mcp_entry(
    registered_project: Path, capsys: pytest.CaptureFixture
) -> None:
    settings_json = registered_project / ".claude" / "settings.json"
    data = json.loads(settings_json.read_text(encoding="utf-8"))
    del data["mcpServers"]["strata"]
    settings_json.write_text(json.dumps(data), encoding="utf-8")

    rc, output = _run_doctor(capsys)

    assert rc == 1
    lower = output.lower()
    assert "mcp" in lower
    assert "register" in lower


# ---------------------------------------------------------------------------
# 5. Stop hook script present and matching shipped.
# ---------------------------------------------------------------------------


def test_doctor_flags_missing_stop_hook(
    registered_project: Path, capsys: pytest.CaptureFixture
) -> None:
    (registered_project / ".claude" / "hooks" / "strata-stop-hook").unlink()

    rc, output = _run_doctor(capsys)

    assert rc == 1
    lower = output.lower()
    assert "stop hook" in lower
    assert "register" in lower


def test_doctor_flags_edited_stop_hook_script(
    registered_project: Path, capsys: pytest.CaptureFixture
) -> None:
    hook_script = registered_project / ".claude" / "hooks" / "strata-stop-hook"
    hook_script.write_text("#!/bin/sh\necho tampered\n", encoding="utf-8")

    rc, output = _run_doctor(capsys)

    assert rc == 1
    assert "stop hook" in output.lower()


# ---------------------------------------------------------------------------
# 6. hooks.Stop entry present.
# ---------------------------------------------------------------------------


def test_doctor_flags_missing_stop_hook_entry(
    registered_project: Path, capsys: pytest.CaptureFixture
) -> None:
    settings_json = registered_project / ".claude" / "settings.json"
    data = json.loads(settings_json.read_text(encoding="utf-8"))
    del data["hooks"]["Stop"]
    settings_json.write_text(json.dumps(data), encoding="utf-8")

    rc, output = _run_doctor(capsys)

    assert rc == 1
    lower = output.lower()
    assert "hooks.stop" in lower or "stop hook entry" in lower
    assert "register" in lower


# ---------------------------------------------------------------------------
# 7. Skills present.
# ---------------------------------------------------------------------------


def test_doctor_flags_missing_skill(
    registered_project: Path, capsys: pytest.CaptureFixture
) -> None:
    import shutil

    shutil.rmtree(registered_project / ".claude" / "skills" / "strata-worker")

    rc, output = _run_doctor(capsys)

    assert rc == 1
    lower = output.lower()
    assert "skill" in lower
    assert "strata-worker" in lower
    assert "register" in lower


# ---------------------------------------------------------------------------
# 8. Binding env vars set and valid against the fleet.
# ---------------------------------------------------------------------------


def test_doctor_flags_missing_scope_env(
    registered_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv("STRATA_AGENT_SCOPE", raising=False)

    rc, output = _run_doctor(capsys)

    assert rc == 1
    lower = output.lower()
    assert "binding" in lower
    assert "strata_agent_scope" in lower


def test_doctor_flags_missing_skill_env(
    registered_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv("STRATA_AGENT_SKILL", raising=False)

    rc, output = _run_doctor(capsys)

    assert rc == 1
    lower = output.lower()
    assert "binding" in lower
    assert "strata_agent_skill" in lower


def test_doctor_warns_but_passes_on_missing_session_id_env(
    registered_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """STRATA_AGENT_SESSION_ID is auto-generated at runtime when absent (mirrors
    strata.mcp.server) — an operator's shell will almost never export it, so its
    absence must warn, not fail `strata doctor`'s exit code."""
    monkeypatch.delenv("STRATA_AGENT_SESSION_ID", raising=False)

    rc, output = _run_doctor(capsys)

    assert rc == 0
    lower = output.lower()
    assert "session id" in lower
    assert "strata_agent_session_id" in lower


def test_doctor_flags_scope_not_in_fleet(
    registered_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_does_not_exist")

    rc, output = _run_doctor(capsys)

    assert rc == 1
    lower = output.lower()
    assert "binding" in lower
    assert "g_does_not_exist" in lower


def test_doctor_flags_skill_not_permitted(
    registered_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    fleet_yaml = registered_project / ".strata" / "fleet.yaml"
    fleet_yaml.write_text(
        "strata:\n  - id: L0\n    name: root\n    ordinal: 0\n"
        "scopes:\n  - id: g_root\n    name: Root\n    stratum_id: L0\n"
        "    permitted_skills: [architect]\n"
        "edges: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("STRATA_AGENT_SKILL", "strata-worker")

    rc, output = _run_doctor(capsys)

    assert rc == 1
    lower = output.lower()
    assert "binding" in lower
    assert "strata-worker" in lower


# ---------------------------------------------------------------------------
# Plain-language contract: every failure line says how to fix it.
# ---------------------------------------------------------------------------


def test_doctor_failure_lines_are_actionable(
    registered_project: Path, capsys: pytest.CaptureFixture
) -> None:
    (registered_project / ".claude" / "hooks" / "strata-stop-hook").unlink()

    rc, output = _run_doctor(capsys)

    assert rc == 1
    # The failing line must point at a concrete remedy, not just name the problem.
    assert "strata register" in output
