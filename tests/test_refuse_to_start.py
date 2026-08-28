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


def _make_two_scope_fleet(tmp_path: Path) -> FleetConfig:
    """Build a FleetConfig with two scopes — auto-bind never applies here."""
    data = {
        "strata": [{"id": "L0", "name": "root", "ordinal": 0}],
        "scopes": [
            {"id": "g_root", "name": "Root", "stratum_id": "L0"},
            {"id": "g_arch", "name": "Arch", "stratum_id": "L0"},
        ],
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
# Condition 2: STRATA_AGENT_SCOPE not set, and no single-scope fleet to
# auto-bind to → exit(1). A single-scope fleet is covered separately below
# (single-scope auto-bind no longer exits here).
# ---------------------------------------------------------------------------


def test_no_scope_exits_with_message(tmp_path: Path) -> None:
    """Condition 2 failure: STRATA_AGENT_SCOPE empty, 2+ scope fleet → sys.exit(1)."""
    fleet = _make_two_scope_fleet(tmp_path)

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
    fleet = _make_two_scope_fleet(tmp_path)

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


def test_no_scope_message_lists_available_scopes_for_multi_scope_fleet(
    tmp_path: Path, capsys
) -> None:
    """Task requirement: the unset-scope error names the valid scopes when
    the fleet has 2+ of them (no single scope to auto-bind to)."""
    fleet = _make_two_scope_fleet(tmp_path)

    with pytest.raises(SystemExit):
        _validate_binding(
            fleet,
            scope="",
            skill="strata-worker",
            project_config_found=True,
        )

    captured = capsys.readouterr()
    assert "g_root" in captured.err
    assert "g_arch" in captured.err


def test_empty_string_scope_treated_as_unset_for_multi_scope_fleet(tmp_path: Path, capsys) -> None:
    """Empty string counts as unset everywhere (Codex writes literal empty
    env values) — a 2+ scope fleet still exits."""
    fleet = _make_two_scope_fleet(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        _validate_binding(
            fleet,
            scope="",
            skill="strata-worker",
            project_config_found=True,
        )

    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Single-scope auto-bind: unset/empty STRATA_AGENT_SCOPE against a fleet
# with exactly one active scope binds to it automatically, with a one-line
# notice on stderr. An explicitly set scope is never touched by this.
# ---------------------------------------------------------------------------


def test_single_scope_fleet_auto_binds_when_scope_unset(tmp_path: Path) -> None:
    """A single-scope fleet auto-binds — no exit — and returns the scope id."""
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=None)

    resolved_scope, _resolved_skill = _validate_binding(
        fleet,
        scope="",
        skill=None,
        project_config_found=True,
    )

    assert resolved_scope == "g_root"


def test_single_scope_fleet_auto_bind_prints_notice(tmp_path: Path, capsys) -> None:
    """A one-line notice on stderr names the auto-bound scope (never stdout —
    that's the MCP stdio protocol channel)."""
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=None)

    _validate_binding(
        fleet,
        scope="",
        skill=None,
        project_config_found=True,
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "g_root" in captured.err
    assert "auto-bound" in captured.err.lower()


def test_single_scope_fleet_auto_binds_skill_to_default(tmp_path: Path) -> None:
    """When the scope was auto-bound and skill is unset, resolve the scope's
    default_skill (companion rule — otherwise a fresh install with a
    default_skill-declaring seeded scope would still refuse to start)."""
    data = {
        "strata": [{"id": "L0", "name": "root", "ordinal": 0}],
        "scopes": [
            {
                "id": "g_root",
                "name": "Root",
                "stratum_id": "L0",
                "default_skill": "strata-worker",
            }
        ],
        "edges": [],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(data), encoding="utf-8")
    fleet = FleetConfig.load(fleet_path)

    resolved_scope, resolved_skill = _validate_binding(
        fleet,
        scope="",
        skill="",
        project_config_found=True,
    )

    assert resolved_scope == "g_root"
    assert resolved_skill == "strata-worker"


def test_multi_scope_fleet_does_not_auto_bind(tmp_path: Path) -> None:
    """A 2+ scope fleet still exits on unset scope — no auto-bind target."""
    fleet = _make_two_scope_fleet(tmp_path)

    with pytest.raises(SystemExit):
        _validate_binding(
            fleet,
            scope="",
            skill="strata-worker",
            project_config_found=True,
        )


def test_explicit_scope_unchanged_by_auto_bind_logic(tmp_path: Path) -> None:
    """An explicitly set STRATA_AGENT_SCOPE is returned unchanged — auto-bind
    logic only ever engages when scope is unset/empty."""
    fleet = _make_two_scope_fleet(tmp_path)

    resolved_scope, resolved_skill = _validate_binding(
        fleet,
        scope="g_arch",
        skill="strata-worker",
        project_config_found=True,
    )

    assert resolved_scope == "g_arch"
    assert resolved_skill == "strata-worker"


def test_explicit_scope_no_auto_bind_notice_printed(tmp_path: Path, capsys) -> None:
    """No auto-bind notice appears when the scope was explicitly set, even
    against a single-scope fleet."""
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=None)

    _validate_binding(
        fleet,
        scope="g_root",
        skill="strata-worker",
        project_config_found=True,
    )

    captured = capsys.readouterr()
    assert "auto-bound" not in captured.err.lower()


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
# Condition 3 (review follow-up): an archived scope cannot be bound at
# startup either — the SAME rule strata_bind enforces (Feature B), via the
# shared _check_scope_exists(require_active=True) helper, not a rule that
# only strata_bind knows about.
# ---------------------------------------------------------------------------


def _make_fleet_with_archived_scope(tmp_path: Path) -> FleetConfig:
    data = {
        "strata": [{"id": "L0", "name": "root", "ordinal": 0}],
        "scopes": [
            {"id": "g_root", "name": "Root", "stratum_id": "L0", "status": "archived"},
            {"id": "g_active", "name": "Active", "stratum_id": "L0"},
        ],
        "edges": [],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(data), encoding="utf-8")
    return FleetConfig.load(fleet_path)


def test_archived_scope_exits_with_message(tmp_path: Path) -> None:
    """An archived scope must be refused at startup, exactly like strata_bind refuses it."""
    fleet = _make_fleet_with_archived_scope(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        _validate_binding(
            fleet,
            scope="g_root",
            skill=None,
            project_config_found=True,
        )

    assert exc_info.value.code == 1


def test_archived_scope_message_names_it_archived_and_lists_active(tmp_path: Path, capsys) -> None:
    fleet = _make_fleet_with_archived_scope(tmp_path)

    with pytest.raises(SystemExit):
        _validate_binding(
            fleet,
            scope="g_root",
            skill=None,
            project_config_found=True,
        )

    captured = capsys.readouterr()
    assert "archived" in captured.err
    assert "g_active" in captured.err


# ---------------------------------------------------------------------------
# Condition 4a: STRATA_AGENT_SKILL not set → exit(1)
# ---------------------------------------------------------------------------


def test_no_skill_exits_with_message(tmp_path: Path) -> None:
    """Condition 3b failure: STRATA_AGENT_SKILL empty on a skill-declaring scope → sys.exit(1).

    Issue #121: a scope that DECLARES skills keeps today's "skill required"
    semantics; only an unrestricted scope may bind skill-less.
    """
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=["strata-worker"])

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
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=["strata-worker"])

    with pytest.raises(SystemExit):
        _validate_binding(
            fleet,
            scope="g_root",
            skill="",
            project_config_found=True,
        )

    captured = capsys.readouterr()
    assert "STRATA_AGENT_SKILL" in captured.err


def test_no_skill_on_unrestricted_scope_is_accepted(tmp_path: Path) -> None:
    """Issue #121: a scope declaring no skills may bind skill-less — no exit.

    The scope is confirmed unrestricted (no default_skill, no
    permitted_skills), so the missing STRATA_AGENT_SKILL is waived rather
    than a refuse-to-start failure.
    """
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=None)

    # Must NOT raise SystemExit.
    _validate_binding(
        fleet,
        scope="g_root",
        skill=None,
        project_config_found=True,
    )


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
