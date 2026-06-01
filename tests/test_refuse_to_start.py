"""Tests for 3b refuse-to-start validation (ADR 0005 Decision 5).

The _validate_binding function is called from main() before mcp.run().
It enforces four conditions in order:

1. .strata/config.toml resolvable (project_config_found=True)
2. STRATA_AGENT_SCOPE env var set
3. Scope exists in fleet config
4. STRATA_AGENT_SKILL is in the scope's permitted_skills (when set)

Each failure → sys.exit(1) with actionable message content.
Happy path: all four conditions met → no exit, mcp.run would proceed.

Vocabulary: scope, stratum, fleet, contribution, scope-manager.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.fleet_config import FleetConfig
from strata.mcp.server import _validate_binding

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fleet_with_skills(
    tmp_path: Path, permitted_skills: list[str] | None = None
) -> FleetConfig:
    """Build a minimal FleetConfig with one scope optionally having permitted_skills."""
    scope_def: dict = {"id": "g_root", "name": "Root", "stratum_id": "L0"}
    if permitted_skills is not None:
        scope_def["permitted_skills"] = permitted_skills

    data = {
        "strata": [{"id": "L0", "name": "root", "ordinal": 0}],
        "scopes": [scope_def],
        "edges": [],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(data), encoding="utf-8")
    return FleetConfig.load(fleet_path)


# ---------------------------------------------------------------------------
# Condition 1: project config not found → exit(1)
# ---------------------------------------------------------------------------


def test_no_project_config_exits_with_message(tmp_path: Path) -> None:
    """Condition 1 failure: no .strata/config.toml → sys.exit(1) with path info."""
    fleet = _make_fleet_with_skills(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        _validate_binding(
            fleet,
            scope="g_root",
            skill="strata-worker",
            project_config_found=False,
        )

    assert exc_info.value.code == 1


def test_no_project_config_message_mentions_register(tmp_path: Path, capsys) -> None:
    """Condition 1 failure message should mention strata register."""
    fleet = _make_fleet_with_skills(tmp_path)

    with pytest.raises(SystemExit):
        _validate_binding(
            fleet,
            scope="g_root",
            skill="strata-worker",
            project_config_found=False,
        )

    captured = capsys.readouterr()
    assert "strata register" in captured.err


# ---------------------------------------------------------------------------
# Condition 2: STRATA_AGENT_SCOPE not set → exit(1)
# ---------------------------------------------------------------------------


def test_no_scope_exits_with_message(tmp_path: Path) -> None:
    """Condition 2 failure: STRATA_AGENT_SCOPE empty → sys.exit(1)."""
    fleet = _make_fleet_with_skills(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        _validate_binding(
            fleet,
            scope="",  # not set
            skill="strata-worker",
            project_config_found=True,
        )

    assert exc_info.value.code == 1


def test_no_scope_message_mentions_export(tmp_path: Path, capsys) -> None:
    """Condition 2 message must include export STRATA_AGENT_SCOPE instruction."""
    fleet = _make_fleet_with_skills(tmp_path)

    with pytest.raises(SystemExit):
        _validate_binding(
            fleet,
            scope="",
            skill="strata-worker",
            project_config_found=True,
        )

    captured = capsys.readouterr()
    assert "STRATA_AGENT_SCOPE" in captured.err
    assert "export" in captured.err


# ---------------------------------------------------------------------------
# Condition 3: scope not in fleet → exit(1)
# ---------------------------------------------------------------------------


def test_unknown_scope_exits_with_message(tmp_path: Path) -> None:
    """Condition 3 failure: scope not in fleet config → sys.exit(1)."""
    fleet = _make_fleet_with_skills(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        _validate_binding(
            fleet,
            scope="g_nonexistent",
            skill="strata-worker",
            project_config_found=True,
        )

    assert exc_info.value.code == 1


def test_unknown_scope_message_lists_available_scopes(tmp_path: Path, capsys) -> None:
    """Condition 3 message must list available scope IDs."""
    fleet = _make_fleet_with_skills(tmp_path)

    with pytest.raises(SystemExit):
        _validate_binding(
            fleet,
            scope="g_nonexistent",
            skill="strata-worker",
            project_config_found=True,
        )

    captured = capsys.readouterr()
    assert "g_root" in captured.err  # the available scope


# ---------------------------------------------------------------------------
# Condition 4a: STRATA_AGENT_SKILL not set → exit(1)
# ---------------------------------------------------------------------------


def test_no_skill_exits_with_message(tmp_path: Path) -> None:
    """Condition 3b failure: STRATA_AGENT_SKILL empty → sys.exit(1)."""
    fleet = _make_fleet_with_skills(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        _validate_binding(
            fleet,
            scope="g_root",
            skill="",  # not set
            project_config_found=True,
        )

    assert exc_info.value.code == 1


def test_no_skill_message_mentions_skill_export(tmp_path: Path, capsys) -> None:
    """Condition 3b failure message must mention STRATA_AGENT_SKILL."""
    fleet = _make_fleet_with_skills(tmp_path)

    with pytest.raises(SystemExit):
        _validate_binding(
            fleet,
            scope="g_root",
            skill="",
            project_config_found=True,
        )

    captured = capsys.readouterr()
    assert "STRATA_AGENT_SKILL" in captured.err


# ---------------------------------------------------------------------------
# Condition 4b: skill not in permitted_skills → exit(1)
# ---------------------------------------------------------------------------


def test_skill_not_in_permitted_exits_with_message(tmp_path: Path) -> None:
    """Condition 4 failure: skill not in permitted_skills → sys.exit(1)."""
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=["strata-worker", "inspector"])

    with pytest.raises(SystemExit) as exc_info:
        _validate_binding(
            fleet,
            scope="g_root",
            skill="unauthorized-skill",
            project_config_found=True,
        )

    assert exc_info.value.code == 1


def test_skill_not_in_permitted_message_lists_permitted_skills(tmp_path: Path, capsys) -> None:
    """Condition 4 message must list the permitted skills for the scope."""
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=["strata-worker", "inspector"])

    with pytest.raises(SystemExit):
        _validate_binding(
            fleet,
            scope="g_root",
            skill="unauthorized-skill",
            project_config_found=True,
        )

    captured = capsys.readouterr()
    assert "strata-worker" in captured.err
    assert "inspector" in captured.err


# ---------------------------------------------------------------------------
# Condition 4c: empty permitted_skills → any skill allowed
# ---------------------------------------------------------------------------


def test_empty_permitted_skills_allows_any_skill(tmp_path: Path) -> None:
    """When permitted_skills is empty/None, any skill is accepted (no exit)."""
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=None)

    # Must NOT raise SystemExit.
    _validate_binding(
        fleet,
        scope="g_root",
        skill="any-skill-whatsoever",
        project_config_found=True,
    )


# ---------------------------------------------------------------------------
# Happy path: all conditions met → no exit
# ---------------------------------------------------------------------------


def test_happy_path_no_exit(tmp_path: Path) -> None:
    """When all four conditions pass, _validate_binding returns without calling sys.exit."""
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=["strata-worker"])

    # Should NOT raise SystemExit.
    _validate_binding(
        fleet,
        scope="g_root",
        skill="strata-worker",
        project_config_found=True,
    )


def test_happy_path_with_empty_permitted_skills_no_exit(tmp_path: Path) -> None:
    """Happy path works when scope has no permitted_skills restriction."""
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=None)

    _validate_binding(
        fleet,
        scope="g_root",
        skill="strata-developer",
        project_config_found=True,
    )


# ---------------------------------------------------------------------------
# Ordering: condition 1 checked before condition 2
# ---------------------------------------------------------------------------


def test_all_failures_reported_in_single_error(tmp_path: Path, capsys) -> None:
    """Per ADR 0005 Decision 5: all validation failures are reported in a
    single error message before exit, not first-failure-wins.

    A user with three missing pieces (no config, no scope env, no skill env)
    sees the complete remediation list in one pass.
    """
    with pytest.raises(SystemExit) as exc_info:
        _validate_binding(
            None,  # No fleet because no config (mirrors main() behaviour)
            scope="",
            skill="",
            project_config_found=False,
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    # All three remediations appear in the same message.
    assert "strata register" in captured.err, "missing config remediation"
    assert "export STRATA_AGENT_SCOPE" in captured.err, "missing scope remediation"
    assert "export STRATA_AGENT_SKILL" in captured.err, "missing skill remediation"
    # The header announces the failure count.
    assert "3 validation failures" in captured.err
