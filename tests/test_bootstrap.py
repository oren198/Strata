"""Tests for src/strata/bootstrap.py (V1.2 behaviour).

Under ADR 0002, ``strata bootstrap`` validates ``fleet.yaml`` and prepares
the in-memory :class:`FleetConfig` mirror — no DB writes.

Vocabulary follows CONTEXT.md: stratum, scope, edge.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from strata.bootstrap import load_fleet_config
from strata.fleet_config import FleetConfig, FleetConfigError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXAMPLE_YAML = Path(__file__).parent.parent / "fleet.example.yaml"


def _write_yaml(tmp_path: Path, content: str, name: str = "fleet.yaml") -> Path:
    """Write *content* to a YAML file in *tmp_path* and return its path."""
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


_MINIMAL_YAML = """
    strata:
      - id: L0
        name: Executive
        ordinal: 0
      - id: L1
        name: Function
        ordinal: 1
      - id: L2
        name: Team
        ordinal: 2

    scopes:
      - id: g_ceo
        name: CEO
        stratum_id: L0
      - id: g_eng
        name: Engineering
        stratum_id: L1
      - id: g_arch
        name: Architect
        stratum_id: L1
      - id: g_backend
        name: Backend Dev
        stratum_id: L2

    edges:
      - from: g_backend
        to: g_eng
      - from: g_arch
        to: g_eng
      - from: g_eng
        to: g_ceo
      - from: g_backend
        to: g_arch
"""


# ---------------------------------------------------------------------------
# Test 1 — load a valid YAML returns FleetConfig with correct counts
# ---------------------------------------------------------------------------


def test_load_fleet_config_valid_yaml(tmp_path: Path) -> None:
    """load_fleet_config returns a FleetConfig with the correct counts."""
    yaml_path = _write_yaml(tmp_path, _MINIMAL_YAML)
    config = load_fleet_config(yaml_path)

    assert isinstance(config, FleetConfig)
    assert len(config.strata) == 3
    assert len(config.scopes) == 4
    assert len(config.edges) == 4

    assert config.strata[0].id == "L0"
    assert config.strata[0].name == "Executive"
    assert config.strata[0].ordinal == 0

    assert config.scopes[0].id == "g_ceo"
    assert config.scopes[0].stratum_id == "L0"

    assert config.edges[0].from_ == "g_backend"
    assert config.edges[0].to == "g_eng"


# ---------------------------------------------------------------------------
# Test 2 — no DB writes happen
# ---------------------------------------------------------------------------


def test_bootstrap_does_not_write_db(tmp_path: Path) -> None:
    """load_fleet_config must not create or touch any DB file."""
    yaml_path = _write_yaml(tmp_path, _MINIMAL_YAML)
    load_fleet_config(yaml_path)

    # No SQLite DB should have appeared in tmp_path.
    db_files = list(tmp_path.glob("*.db"))
    assert db_files == [], f"bootstrap must not create DB files; found: {db_files}"


# ---------------------------------------------------------------------------
# Test 3 — fleet.example.yaml loads cleanly
# ---------------------------------------------------------------------------


def test_example_yaml_loads(tmp_path: Path) -> None:
    """fleet.example.yaml (repo root) loads and validates without error."""
    assert _EXAMPLE_YAML.exists(), f"fleet.example.yaml not found at {_EXAMPLE_YAML}"
    config = load_fleet_config(_EXAMPLE_YAML)
    assert len(config.strata) >= 3
    assert len(config.scopes) >= 4


# ---------------------------------------------------------------------------
# Test 4 — missing stratum reference raises FleetConfigError
# ---------------------------------------------------------------------------


def test_missing_stratum_reference_raises(tmp_path: Path) -> None:
    """A scope that references an undefined stratum raises FleetConfigError."""
    bad_yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0

        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: LX    # LX not defined above

        edges: []
    """
    yaml_path = _write_yaml(tmp_path, bad_yaml)
    with pytest.raises(FleetConfigError) as exc_info:
        load_fleet_config(yaml_path)
    assert exc_info.value.kind == "unknown_stratum_ref"
    assert "LX" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test 5 — missing scope reference in edge raises FleetConfigError
# ---------------------------------------------------------------------------


def test_missing_scope_reference_in_edge_raises(tmp_path: Path) -> None:
    """An edge referencing an undefined scope raises FleetConfigError."""
    bad_yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
          - id: L1
            name: Function
            ordinal: 1

        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0

        edges:
          - from: g_ceo
            to: g_nonexistent
    """
    yaml_path = _write_yaml(tmp_path, bad_yaml)
    with pytest.raises(FleetConfigError) as exc_info:
        load_fleet_config(yaml_path)
    assert exc_info.value.kind == "unknown_scope_ref"
    assert "g_nonexistent" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test 6 — ±1 stratum constraint violation raises FleetConfigError
# ---------------------------------------------------------------------------


def test_edge_stratum_distance_constraint_raises(tmp_path: Path) -> None:
    """An edge spanning more than one stratum raises FleetConfigError."""
    bad_yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
          - id: L1
            name: Function
            ordinal: 1
          - id: L2
            name: Team
            ordinal: 2

        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
          - id: g_backend
            name: Backend Dev
            stratum_id: L2

        edges:
          - from: g_backend
            to: g_ceo
    """
    yaml_path = _write_yaml(tmp_path, bad_yaml)
    with pytest.raises(FleetConfigError) as exc_info:
        load_fleet_config(yaml_path)
    assert exc_info.value.kind == "stratum_distance_violation"


# ---------------------------------------------------------------------------
# Test 7 — FleetConfig returned with path set (mutation API ready)
# ---------------------------------------------------------------------------


def test_loaded_config_has_path(tmp_path: Path) -> None:
    """The returned FleetConfig has its _path attribute set for mutations."""
    yaml_path = _write_yaml(tmp_path, _MINIMAL_YAML)
    config = load_fleet_config(yaml_path)
    # _path is set as a private attribute
    assert config._path == yaml_path


# ---------------------------------------------------------------------------
# Test 8 — active scopes are returned, archived are excluded
# ---------------------------------------------------------------------------


def test_active_scopes_excludes_archived(tmp_path: Path) -> None:
    """active_scopes() returns only status=active scopes."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0

        scopes:
          - id: g_active
            name: Active
            stratum_id: L0
            status: active
          - id: g_retired
            name: Retired
            stratum_id: L0
            status: archived

        edges: []
    """
    yaml_path = _write_yaml(tmp_path, yaml)
    config = load_fleet_config(yaml_path)
    active = config.active_scopes()
    assert len(active) == 1
    assert active[0].id == "g_active"
