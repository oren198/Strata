"""Tests for startup binding validation (ADR 0005 Decision 5, soft-start addendum).

The _validate_binding function is called from main() before mcp.run().
It enforces four conditions in order:

1. .strata/config.toml resolvable (project_config_found=True)
2. STRATA_AGENT_SCOPE env var set
3. Scope exists in fleet config
4. STRATA_AGENT_SKILL is in the scope's permitted_skills (when set)

Soft-start (dated addendum): a failure no longer exits the process. Each
condition's failure is instead appended to one of two classified lists —
the ``(resolved_scope, resolved_skill, config_errors, binding_errors)``
tuple's last two elements (review follow-up: strata_bind/elicitation used
to clear EVERY startup failure unconditionally, including a broken
.strata/config.toml it can never actually fix; see _validate_binding's
docstring for the incident):

- config_errors: condition 1 (.strata/config.toml not found) — a config/
  storage-source problem strata_bind can never clear, since fixing it only
  takes effect on the next restart.
- binding_errors: conditions 2-5 (scope/skill selection) — live-fixable via
  strata_bind or an accepted elicitation.

Happy path: both lists empty, mcp.run would proceed exactly as before.

Vocabulary: scope, stratum, fleet, contribution, scope-manager.
"""

from __future__ import annotations

import sys
from pathlib import Path

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
# Condition 1: project config not found → config-class, reported, no exit
# ---------------------------------------------------------------------------


def test_no_project_config_does_not_exit(tmp_path: Path) -> None:
    """Condition 1 failure: no .strata/config.toml → config_errors, no exit."""
    fleet = _make_fleet_with_skills(tmp_path)

    _resolved_scope, _resolved_skill, config_errors, binding_errors = _validate_binding(
        fleet,
        scope="g_root",
        skill="strata-worker",
        project_config_found=False,
    )

    assert config_errors  # non-empty: the failure is present
    assert binding_errors == []


def test_no_project_config_message_mentions_register(tmp_path: Path) -> None:
    """Condition 1 failure message should mention strata register."""
    fleet = _make_fleet_with_skills(tmp_path)

    _resolved_scope, _resolved_skill, config_errors, _binding_errors = _validate_binding(
        fleet,
        scope="g_root",
        skill="strata-worker",
        project_config_found=False,
    )

    assert any("strata register" in e for e in config_errors)


# ---------------------------------------------------------------------------
# Condition 2: STRATA_AGENT_SCOPE not set, and no single-scope fleet to
# auto-bind to → binding-class, reported. A single-scope fleet is covered
# separately below (single-scope auto-bind never reports a failure here).
# ---------------------------------------------------------------------------


def test_no_scope_reports_failure(tmp_path: Path) -> None:
    """Condition 2 failure: STRATA_AGENT_SCOPE empty, 2+ scope fleet → binding_errors."""
    fleet = _make_two_scope_fleet(tmp_path)

    _resolved_scope, _resolved_skill, config_errors, binding_errors = _validate_binding(
        fleet,
        scope="",  # not set
        skill="strata-worker",
        project_config_found=True,
    )

    assert config_errors == []
    assert binding_errors


def test_no_scope_message_mentions_strata_bind_and_restart(tmp_path: Path) -> None:
    """Condition 2 message must name both live-fix paths: strata_bind, or a
    restart with the env var set (dropped the misleading "export ... and
    call strata_bind" combo — the env var is read once, at process start)."""
    fleet = _make_two_scope_fleet(tmp_path)

    _resolved_scope, _resolved_skill, _config_errors, binding_errors = _validate_binding(
        fleet,
        scope="",
        skill="strata-worker",
        project_config_found=True,
    )

    joined = "\n".join(binding_errors)
    assert "STRATA_AGENT_SCOPE" in joined
    assert "strata_bind" in joined
    assert "restart" in joined.lower()


def test_no_scope_message_lists_available_scopes_for_multi_scope_fleet(tmp_path: Path) -> None:
    """Task requirement: the unset-scope error names the valid scopes when
    the fleet has 2+ of them (no single scope to auto-bind to)."""
    fleet = _make_two_scope_fleet(tmp_path)

    _resolved_scope, _resolved_skill, _config_errors, binding_errors = _validate_binding(
        fleet,
        scope="",
        skill="strata-worker",
        project_config_found=True,
    )

    joined = "\n".join(binding_errors)
    assert "g_root" in joined
    assert "g_arch" in joined


def test_empty_string_scope_treated_as_unset_for_multi_scope_fleet(tmp_path: Path) -> None:
    """Empty string counts as unset everywhere (Codex writes literal empty
    env values) — a 2+ scope fleet still reports a failure."""
    fleet = _make_two_scope_fleet(tmp_path)

    _resolved_scope, _resolved_skill, _config_errors, binding_errors = _validate_binding(
        fleet,
        scope="",
        skill="strata-worker",
        project_config_found=True,
    )

    assert binding_errors


# ---------------------------------------------------------------------------
# Single-scope auto-bind: unset/empty STRATA_AGENT_SCOPE against a fleet
# with exactly one active scope binds to it automatically, with a one-line
# notice on stderr. An explicitly set scope is never touched by this.
# ---------------------------------------------------------------------------


def test_single_scope_fleet_auto_binds_when_scope_unset(tmp_path: Path) -> None:
    """A single-scope fleet auto-binds — no failure reported — and returns the scope id."""
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=None)

    resolved_scope, _resolved_skill, config_errors, binding_errors = _validate_binding(
        fleet,
        scope="",
        skill=None,
        project_config_found=True,
    )

    assert resolved_scope == "g_root"
    assert config_errors == []
    assert binding_errors == []


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
    default_skill-declaring seeded scope would still report a failure)."""
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

    resolved_scope, resolved_skill, config_errors, binding_errors = _validate_binding(
        fleet,
        scope="",
        skill="",
        project_config_found=True,
    )

    assert resolved_scope == "g_root"
    assert resolved_skill == "strata-worker"
    assert config_errors == []
    assert binding_errors == []


def test_multi_scope_fleet_does_not_auto_bind(tmp_path: Path) -> None:
    """A 2+ scope fleet still reports a failure on unset scope — no auto-bind target."""
    fleet = _make_two_scope_fleet(tmp_path)

    _resolved_scope, _resolved_skill, _config_errors, binding_errors = _validate_binding(
        fleet,
        scope="",
        skill="strata-worker",
        project_config_found=True,
    )

    assert binding_errors


def test_explicit_scope_unchanged_by_auto_bind_logic(tmp_path: Path) -> None:
    """An explicitly set STRATA_AGENT_SCOPE is returned unchanged — auto-bind
    logic only ever engages when scope is unset/empty."""
    fleet = _make_two_scope_fleet(tmp_path)

    resolved_scope, resolved_skill, config_errors, binding_errors = _validate_binding(
        fleet,
        scope="g_arch",
        skill="strata-worker",
        project_config_found=True,
    )

    assert resolved_scope == "g_arch"
    assert resolved_skill == "strata-worker"
    assert config_errors == []
    assert binding_errors == []


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
# Condition 3: scope not in fleet → binding-class, reported
# ---------------------------------------------------------------------------


def test_unknown_scope_reports_failure(tmp_path: Path) -> None:
    """Condition 3 failure: scope not in fleet config → binding_errors."""
    fleet = _make_fleet_with_skills(tmp_path)

    _resolved_scope, _resolved_skill, config_errors, binding_errors = _validate_binding(
        fleet,
        scope="g_nonexistent",
        skill="strata-worker",
        project_config_found=True,
    )

    assert config_errors == []
    assert binding_errors


def test_unknown_scope_message_lists_available_scopes(tmp_path: Path) -> None:
    """Condition 3 message must list available scope IDs."""
    fleet = _make_fleet_with_skills(tmp_path)

    _resolved_scope, _resolved_skill, _config_errors, binding_errors = _validate_binding(
        fleet,
        scope="g_nonexistent",
        skill="strata-worker",
        project_config_found=True,
    )

    assert any("g_root" in e for e in binding_errors)  # the available scope


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


def test_archived_scope_reports_failure(tmp_path: Path) -> None:
    """An archived scope must be refused at startup, exactly like strata_bind refuses it."""
    fleet = _make_fleet_with_archived_scope(tmp_path)

    _resolved_scope, _resolved_skill, config_errors, binding_errors = _validate_binding(
        fleet,
        scope="g_root",
        skill=None,
        project_config_found=True,
    )

    assert config_errors == []
    assert binding_errors


def test_archived_scope_message_names_it_archived_and_lists_active(tmp_path: Path) -> None:
    fleet = _make_fleet_with_archived_scope(tmp_path)

    _resolved_scope, _resolved_skill, _config_errors, binding_errors = _validate_binding(
        fleet,
        scope="g_root",
        skill=None,
        project_config_found=True,
    )

    joined = "\n".join(binding_errors)
    assert "archived" in joined
    assert "g_active" in joined


# ---------------------------------------------------------------------------
# Condition 4a: STRATA_AGENT_SKILL not set → binding-class, reported
# ---------------------------------------------------------------------------


def test_no_skill_reports_failure(tmp_path: Path) -> None:
    """Condition 3b failure: STRATA_AGENT_SKILL empty on a skill-declaring scope → reported.

    Issue #121: a scope that DECLARES skills keeps today's "skill required"
    semantics; only an unrestricted scope may bind skill-less.
    """
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=["strata-worker"])

    _resolved_scope, _resolved_skill, config_errors, binding_errors = _validate_binding(
        fleet,
        scope="g_root",
        skill="",  # not set
        project_config_found=True,
    )

    assert config_errors == []
    assert binding_errors


def test_no_skill_message_mentions_skill_and_bind(tmp_path: Path) -> None:
    """Condition 3b failure message must mention STRATA_AGENT_SKILL and the
    strata_bind recovery path."""
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=["strata-worker"])

    _resolved_scope, _resolved_skill, _config_errors, binding_errors = _validate_binding(
        fleet,
        scope="g_root",
        skill="",
        project_config_found=True,
    )

    joined = "\n".join(binding_errors)
    assert "STRATA_AGENT_SKILL" in joined
    assert "strata_bind" in joined


def test_no_skill_on_unrestricted_scope_is_accepted(tmp_path: Path) -> None:
    """Issue #121: a scope declaring no skills may bind skill-less — no failure.

    The scope is confirmed unrestricted (no default_skill, no
    permitted_skills), so the missing STRATA_AGENT_SKILL is waived rather
    than a validation failure.
    """
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=None)

    _resolved_scope, _resolved_skill, config_errors, binding_errors = _validate_binding(
        fleet,
        scope="g_root",
        skill=None,
        project_config_found=True,
    )

    assert config_errors == []
    assert binding_errors == []


# ---------------------------------------------------------------------------
# Condition 4b: skill not in permitted_skills → binding-class, reported
# ---------------------------------------------------------------------------


def test_skill_not_in_permitted_reports_failure(tmp_path: Path) -> None:
    """Condition 4 failure: skill not in permitted_skills → binding_errors."""
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=["strata-worker", "inspector"])

    _resolved_scope, _resolved_skill, config_errors, binding_errors = _validate_binding(
        fleet,
        scope="g_root",
        skill="unauthorized-skill",
        project_config_found=True,
    )

    assert config_errors == []
    assert binding_errors


def test_skill_not_in_permitted_message_lists_permitted_skills(tmp_path: Path) -> None:
    """Condition 4 message must list the permitted skills for the scope."""
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=["strata-worker", "inspector"])

    _resolved_scope, _resolved_skill, _config_errors, binding_errors = _validate_binding(
        fleet,
        scope="g_root",
        skill="unauthorized-skill",
        project_config_found=True,
    )

    joined = "\n".join(binding_errors)
    assert "strata-worker" in joined
    assert "inspector" in joined


# ---------------------------------------------------------------------------
# Condition 4c: empty permitted_skills → any skill allowed
# ---------------------------------------------------------------------------


def test_empty_permitted_skills_allows_any_skill(tmp_path: Path) -> None:
    """When permitted_skills is empty/None, any skill is accepted (no failure)."""
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=None)

    _resolved_scope, _resolved_skill, config_errors, binding_errors = _validate_binding(
        fleet,
        scope="g_root",
        skill="any-skill-whatsoever",
        project_config_found=True,
    )

    assert config_errors == []
    assert binding_errors == []


# ---------------------------------------------------------------------------
# Happy path: all conditions met → both lists empty
# ---------------------------------------------------------------------------


def test_happy_path_no_errors(tmp_path: Path) -> None:
    """When all four conditions pass, _validate_binding returns empty error lists."""
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=["strata-worker"])

    _resolved_scope, _resolved_skill, config_errors, binding_errors = _validate_binding(
        fleet,
        scope="g_root",
        skill="strata-worker",
        project_config_found=True,
    )

    assert config_errors == []
    assert binding_errors == []


def test_happy_path_with_empty_permitted_skills_no_errors(tmp_path: Path) -> None:
    """Happy path works when scope has no permitted_skills restriction."""
    fleet = _make_fleet_with_skills(tmp_path, permitted_skills=None)

    _resolved_scope, _resolved_skill, config_errors, binding_errors = _validate_binding(
        fleet,
        scope="g_root",
        skill="strata-developer",
        project_config_found=True,
    )

    assert config_errors == []
    assert binding_errors == []


# ---------------------------------------------------------------------------
# Never exits: soft-start's whole point (dated addendum, ADR 0005 D5)
# ---------------------------------------------------------------------------


def test_multiple_failures_never_raise_systemexit(tmp_path: Path) -> None:
    """Soft-start: even with every condition failing at once, _validate_binding
    returns normally — it must never call sys.exit."""
    # Should not raise SystemExit (or anything else).
    _validate_binding(
        None,  # No fleet because no config (mirrors main() behaviour)
        scope="",
        skill="",
        project_config_found=False,
    )


def test_all_failures_reported_across_both_classified_lists(tmp_path: Path) -> None:
    """Per ADR 0005 Decision 5: all validation failures are reported (not
    first-failure-wins), now split across the two classified lists.

    A user with three missing pieces (no config, no scope env, no skill env)
    sees the complete remediation picture in one pass: one config-class
    failure, two binding-class ones.
    """
    _resolved_scope, _resolved_skill, config_errors, binding_errors = _validate_binding(
        None,  # No fleet because no config (mirrors main() behaviour)
        scope="",
        skill="",
        project_config_found=False,
    )

    assert any("strata register" in e for e in config_errors), "missing config remediation"
    assert len(config_errors) == 1

    joined_binding = "\n".join(binding_errors)
    assert "STRATA_AGENT_SCOPE" in joined_binding, "missing scope remediation"
    assert "STRATA_AGENT_SKILL" in joined_binding, "missing skill remediation"
    assert len(binding_errors) == 2


def test_all_failures_also_printed_to_stderr_for_local_dev(tmp_path: Path, capsys) -> None:
    """The aggregated message (both classes combined) is still printed to
    stderr — useful for a human running the server locally who does read it
    — even though it is no longer the only place the failure is visible
    (soft-start addendum)."""
    _validate_binding(
        None,
        scope="",
        skill="",
        project_config_found=False,
    )

    captured = capsys.readouterr()
    assert "3 validation failures" in captured.err
