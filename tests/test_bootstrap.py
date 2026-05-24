"""Tests for src/strata/bootstrap.py.

Each test uses a fresh SQLite database via pytest's ``tmp_path`` fixture.
Migrations are applied before opening a :class:`RecordStore`, following the
pattern established in test_record_store.py.

Vocabulary follows CONTEXT.md: stratum, scope, edge — never level, group,
relation.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

# Make scripts/ importable so we can call run_migrations directly.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from run_migrations import run_migrations  # noqa: E402

from strata.bootstrap import (  # noqa: E402
    BootstrapResult,
    FleetConfig,
    apply_fleet_config,
    load_fleet_config,
)
from strata.record_store import RecordStore  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXAMPLE_YAML = Path(__file__).parent.parent / "fleet.example.yaml"


def _open_store(tmp_path: Path) -> RecordStore:
    """Apply migrations and return an open RecordStore backed by a temp DB."""
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    return RecordStore(db_path)


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
# Test 1 — load a valid YAML
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

    # Verify EdgeDef alias: ``from_`` ↔ YAML key ``from``.
    assert config.edges[0].from_ == "g_backend"
    assert config.edges[0].to == "g_eng"


# ---------------------------------------------------------------------------
# Test 2 — apply to a fresh DB
# ---------------------------------------------------------------------------


def test_apply_to_fresh_db(tmp_path: Path) -> None:
    """Applying the config to a fresh DB creates all strata, scopes, edges."""
    yaml_path = _write_yaml(tmp_path, _MINIMAL_YAML)
    config = load_fleet_config(yaml_path)

    with _open_store(tmp_path) as store:
        result = apply_fleet_config(store, config)

    assert isinstance(result, BootstrapResult)
    assert result.strata_created == ["L0", "L1", "L2"]
    assert result.strata_existing == []
    assert set(result.scopes_created) == {"g_ceo", "g_eng", "g_arch", "g_backend"}
    assert result.scopes_existing == []
    assert len(result.edges_created) == 4
    assert result.edges_existing == []


# ---------------------------------------------------------------------------
# Test 3 — idempotency (run twice)
# ---------------------------------------------------------------------------


def test_idempotent_second_run(tmp_path: Path) -> None:
    """A second apply creates nothing; all items land in _existing lists."""
    yaml_path = _write_yaml(tmp_path, _MINIMAL_YAML)
    config = load_fleet_config(yaml_path)

    with _open_store(tmp_path) as store:
        apply_fleet_config(store, config)
        result2 = apply_fleet_config(store, config)

    assert result2.strata_created == []
    assert set(result2.strata_existing) == {"L0", "L1", "L2"}
    assert result2.scopes_created == []
    assert set(result2.scopes_existing) == {"g_ceo", "g_eng", "g_arch", "g_backend"}
    assert result2.edges_created == []
    assert len(result2.edges_existing) == 4


# ---------------------------------------------------------------------------
# Test 4 — drift on stratum name
# ---------------------------------------------------------------------------


def test_stratum_name_drift_raises(tmp_path: Path) -> None:
    """Pre-existing stratum with a different name raises ValueError mentioning the id."""
    yaml_path = _write_yaml(tmp_path, _MINIMAL_YAML)
    config = load_fleet_config(yaml_path)

    with _open_store(tmp_path) as store:
        # Insert L0 with a DIFFERENT name before applying config.
        store._conn.execute(
            "INSERT INTO strata (id, name, ordinal) VALUES (?, ?, ?)",
            ("L0", "WRONG_NAME", 0),
        )
        store._conn.commit()

        with pytest.raises(ValueError, match="L0"):
            apply_fleet_config(store, config)


# ---------------------------------------------------------------------------
# Test 5 — drift on scope stratum
# ---------------------------------------------------------------------------


def test_scope_stratum_drift_raises(tmp_path: Path) -> None:
    """Pre-existing scope with a different stratum_id raises ValueError mentioning the id."""
    yaml_path = _write_yaml(tmp_path, _MINIMAL_YAML)
    config = load_fleet_config(yaml_path)

    with _open_store(tmp_path) as store:
        # Insert the strata first (needed for FK), then g_ceo under wrong stratum.
        store.create_stratum(name="Executive", ordinal=0)
        store.create_stratum(name="Function", ordinal=1)
        store.create_stratum(name="Team", ordinal=2)

        store._conn.execute(
            "INSERT INTO scopes (id, name, stratum_id) VALUES (?, ?, ?)",
            ("g_ceo", "CEO", "L1"),  # Wrong stratum — should be L0
        )
        store._conn.commit()

        with pytest.raises(ValueError, match="g_ceo"):
            apply_fleet_config(store, config)


# ---------------------------------------------------------------------------
# Test 6 — missing stratum reference in scope
# ---------------------------------------------------------------------------


def test_missing_stratum_reference_raises(tmp_path: Path) -> None:
    """A scope that references an undefined stratum raises a clear error."""
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
    config = load_fleet_config(yaml_path)

    with _open_store(tmp_path) as store, pytest.raises(ValueError, match="LX"):
        apply_fleet_config(store, config)


# ---------------------------------------------------------------------------
# Test 7 — missing scope reference in edge
# ---------------------------------------------------------------------------


def test_missing_scope_reference_in_edge_raises(tmp_path: Path) -> None:
    """An edge that references an undefined scope raises a clear error."""
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
            to: g_nonexistent    # not defined above
    """
    yaml_path = _write_yaml(tmp_path, bad_yaml)
    config = load_fleet_config(yaml_path)

    with _open_store(tmp_path) as store, pytest.raises(ValueError, match="g_nonexistent"):
        apply_fleet_config(store, config)


# ---------------------------------------------------------------------------
# Test 8 — ±1 stratum constraint propagates from record_store
# ---------------------------------------------------------------------------


def test_edge_stratum_distance_constraint_propagates(tmp_path: Path) -> None:
    """An edge spanning more than one stratum raises (record_store enforces this)."""
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
            to: g_ceo    # spans 2 strata (L2 → L0), forbidden
    """
    yaml_path = _write_yaml(tmp_path, bad_yaml)
    config = load_fleet_config(yaml_path)

    with _open_store(tmp_path) as store, pytest.raises(ValueError, match="stratum"):
        apply_fleet_config(store, config)


# ---------------------------------------------------------------------------
# Test 9 — fleet.example.yaml loads and applies to a fresh DB
# ---------------------------------------------------------------------------


def test_example_yaml_loads_and_applies(tmp_path: Path) -> None:
    """fleet.example.yaml loads cleanly and applies to a fresh DB without error."""
    assert _EXAMPLE_YAML.exists(), f"fleet.example.yaml not found at {_EXAMPLE_YAML}"

    config = load_fleet_config(_EXAMPLE_YAML)

    assert len(config.strata) >= 3
    assert len(config.scopes) >= 4
    assert len(config.edges) >= 3

    with _open_store(tmp_path) as store:
        result = apply_fleet_config(store, config)

    assert len(result.strata_created) == len(config.strata)
    assert len(result.scopes_created) == len(config.scopes)
    assert len(result.edges_created) == len(config.edges)
    assert result.strata_existing == []
    assert result.scopes_existing == []
    assert result.edges_existing == []
