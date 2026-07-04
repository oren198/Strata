"""Tests for the V1 → V1.2 fleet config exporter.

Each test builds a genuine V1 SQLite DB by applying only ``0001_initial.sql``
(isolated from 0002 via a temp migrations directory — the same pattern used in
``test_migrator.py``), seeds rows, and then exercises the exporter.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest
import yaml

from strata.fleet_config import FleetConfig, FleetConfigError
from strata.fleet_export import ExportResult, TablesAbsentError, export_fleet
from strata.migrator import run_migrations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_v1_db(db_path: str, tmp_path: Path) -> None:
    """Apply only 0001_initial.sql to *db_path*, leaving 0002 unapplied."""
    migrations_dir = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"
    only_0001 = tmp_path / "migrations_0001_only"
    only_0001.mkdir()
    (only_0001 / "0001_initial.sql").write_text(
        (migrations_dir / "0001_initial.sql").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    run_migrations(db_path, migrations_dir=only_0001)


def _seed_fleet(db_path: str) -> None:
    """Seed a small but complete V1 fleet with strata, scopes, and edges."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L0', 'Executive', 0)")
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L1', 'Function', 1)")
    conn.execute("INSERT INTO scopes (id, name, stratum_id) VALUES ('s_ceo', 'CEO', 'L0')")
    conn.execute("INSERT INTO scopes (id, name, stratum_id) VALUES ('s_eng', 'Engineering', 'L1')")
    conn.execute(
        "INSERT INTO edges (id, from_scope_id, to_scope_id) VALUES ('e1', 's_ceo', 's_eng')"
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Core export
# ---------------------------------------------------------------------------


def test_export_round_trips_through_fleet_config_load(tmp_path: Path) -> None:
    """Exporter produces a fleet.yaml that FleetConfig.load accepts without error."""
    db_path = str(tmp_path / "v1.db")
    _build_v1_db(db_path, tmp_path)
    _seed_fleet(db_path)

    out = tmp_path / "fleet.yaml"
    result = export_fleet(db_path, out)

    assert isinstance(result, ExportResult)
    assert result.strata_count == 2
    assert result.scopes_count == 2
    assert result.edges_count == 1
    assert result.out_path == out

    config = FleetConfig.load(out)
    assert len(config.strata) == 2
    assert len(config.scopes) == 2
    assert len(config.edges) == 1


def test_export_strata_ordered_by_ordinal(tmp_path: Path) -> None:
    """Strata are emitted in ascending ordinal order."""
    db_path = str(tmp_path / "v1.db")
    _build_v1_db(db_path, tmp_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    # Insert in reverse ordinal order to prove ordering is not insertion order.
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L2', 'Team', 2)")
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L0', 'Executive', 0)")
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L1', 'Function', 1)")
    conn.commit()
    conn.close()

    out = tmp_path / "fleet.yaml"
    export_fleet(db_path, out)

    raw = yaml.safe_load(out.read_text(encoding="utf-8"))
    ordinals = [s["ordinal"] for s in raw["strata"]]
    assert ordinals == sorted(ordinals)


def test_export_edge_key_mapping(tmp_path: Path) -> None:
    """V1 ``from_scope_id``/``to_scope_id`` columns map to ``from``/``to`` in YAML."""
    db_path = str(tmp_path / "v1.db")
    _build_v1_db(db_path, tmp_path)
    _seed_fleet(db_path)

    out = tmp_path / "fleet.yaml"
    export_fleet(db_path, out)

    raw = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert len(raw["edges"]) == 1
    edge = raw["edges"][0]
    assert "from" in edge
    assert "to" in edge
    assert "from_scope_id" not in edge
    assert "to_scope_id" not in edge
    assert edge["from"] == "s_ceo"
    assert edge["to"] == "s_eng"


def test_export_scopes_omit_skill_fields(tmp_path: Path) -> None:
    """No ``status``, ``default_skill``, or ``permitted_skills`` are fabricated."""
    db_path = str(tmp_path / "v1.db")
    _build_v1_db(db_path, tmp_path)
    _seed_fleet(db_path)

    out = tmp_path / "fleet.yaml"
    export_fleet(db_path, out)

    raw = yaml.safe_load(out.read_text(encoding="utf-8"))
    for scope in raw["scopes"]:
        assert "status" not in scope
        assert "default_skill" not in scope
        assert "permitted_skills" not in scope


def test_export_scopes_default_to_active_after_load(tmp_path: Path) -> None:
    """Round-tripped scopes without an explicit status default to ``active``."""
    db_path = str(tmp_path / "v1.db")
    _build_v1_db(db_path, tmp_path)
    _seed_fleet(db_path)

    out = tmp_path / "fleet.yaml"
    export_fleet(db_path, out)

    config = FleetConfig.load(out)
    assert all(s.status == "active" for s in config.scopes)


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------


def test_export_does_not_drop_tables(tmp_path: Path) -> None:
    """After export, the V1 strata/scopes/edges tables still exist in the source DB."""
    db_path = str(tmp_path / "v1.db")
    _build_v1_db(db_path, tmp_path)
    _seed_fleet(db_path)

    out = tmp_path / "fleet.yaml"
    export_fleet(db_path, out)

    conn = sqlite3.connect(db_path)
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    conn.close()

    assert "strata" in tables
    assert "scopes" in tables
    assert "edges" in tables


# ---------------------------------------------------------------------------
# Overwrite protection
# ---------------------------------------------------------------------------


def test_export_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    """Exporter raises FileExistsError when out_path exists and force is False."""
    db_path = str(tmp_path / "v1.db")
    _build_v1_db(db_path, tmp_path)
    _seed_fleet(db_path)

    out = tmp_path / "fleet.yaml"
    out.write_text("existing content", encoding="utf-8")

    with pytest.raises(FileExistsError):
        export_fleet(db_path, out, force=False)

    # The existing file must be unchanged.
    assert out.read_text(encoding="utf-8") == "existing content"


def test_export_force_overwrites_existing_file(tmp_path: Path) -> None:
    """--force overwrites an existing out_path."""
    db_path = str(tmp_path / "v1.db")
    _build_v1_db(db_path, tmp_path)
    _seed_fleet(db_path)

    out = tmp_path / "fleet.yaml"
    out.write_text("old content", encoding="utf-8")

    result = export_fleet(db_path, out, force=True)

    assert result.strata_count == 2
    assert out.read_text(encoding="utf-8") != "old content"
    FleetConfig.load(out)  # must parse cleanly


# ---------------------------------------------------------------------------
# Absent tables
# ---------------------------------------------------------------------------


def test_export_absent_tables_raises_tables_absent_error(tmp_path: Path) -> None:
    """TablesAbsentError when none of the V1 fleet tables exist."""
    db_path = str(tmp_path / "empty.db")
    # Create a DB with no tables at all.
    conn = sqlite3.connect(db_path)
    conn.close()

    out = tmp_path / "fleet.yaml"
    with pytest.raises(TablesAbsentError):
        export_fleet(db_path, out)

    assert not out.exists()


def test_export_absent_tables_no_file_written(tmp_path: Path) -> None:
    """When tables are absent, no partial fleet.yaml is left on disk."""
    db_path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(db_path)
    conn.close()

    out = tmp_path / "fleet.yaml"
    with contextlib.suppress(TablesAbsentError):
        export_fleet(db_path, out)

    assert not out.exists()


def test_export_post_migration_db_raises_tables_absent_error(tmp_path: Path) -> None:
    """A DB that has already had 0002 applied raises TablesAbsentError."""
    db_path = str(tmp_path / "migrated.db")
    migrations_dir = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"
    # Apply both migrations.
    run_migrations(db_path, migrations_dir=migrations_dir)

    out = tmp_path / "fleet.yaml"
    with pytest.raises(TablesAbsentError):
        export_fleet(db_path, out)


# ---------------------------------------------------------------------------
# Validation before write
# ---------------------------------------------------------------------------


def test_export_invalid_data_raises_fleet_config_error_and_no_file(tmp_path: Path) -> None:
    """An edge spanning >1 stratum in V1 data surfaces as FleetConfigError; no file written."""
    db_path = str(tmp_path / "v1.db")
    _build_v1_db(db_path, tmp_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L0', 'Executive', 0)")
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L1', 'Function', 1)")
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L2', 'Team', 2)")
    conn.execute("INSERT INTO scopes (id, name, stratum_id) VALUES ('s_top', 'Top', 'L0')")
    conn.execute("INSERT INTO scopes (id, name, stratum_id) VALUES ('s_bot', 'Bottom', 'L2')")
    # Edge spans 2 strata (ordinals 0 → 2) — violates the ±1 constraint.
    conn.execute(
        "INSERT INTO edges (id, from_scope_id, to_scope_id) VALUES ('e_bad', 's_top', 's_bot')"
    )
    conn.commit()
    conn.close()

    out = tmp_path / "fleet.yaml"
    with pytest.raises(FleetConfigError) as exc_info:
        export_fleet(db_path, out)

    assert exc_info.value.kind == "stratum_distance_violation"
    assert not out.exists()


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_export_fleet_success(tmp_path: Path) -> None:
    """``strata export-fleet`` exits 0 and prints expected output."""
    from strata.__main__ import main

    db_path = str(tmp_path / "v1.db")
    _build_v1_db(db_path, tmp_path)
    _seed_fleet(db_path)

    out = tmp_path / "fleet.yaml"
    rc = main(["export-fleet", "--db", db_path, "--out", str(out)])
    assert rc == 0
    assert out.exists()
    FleetConfig.load(out)


def test_cli_export_fleet_absent_tables(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """``strata export-fleet`` exits 1 with a clear message when tables are absent."""
    from strata.__main__ import main

    db_path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(db_path)
    conn.close()

    out = tmp_path / "fleet.yaml"
    rc = main(["export-fleet", "--db", db_path, "--out", str(out)])
    assert rc == 1

    captured = capsys.readouterr()
    assert "No V1 fleet tables found" in captured.err
    assert not out.exists()


def test_cli_export_fleet_no_overwrite(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """``strata export-fleet`` exits 1 and explains --force when out_path exists."""
    from strata.__main__ import main

    db_path = str(tmp_path / "v1.db")
    _build_v1_db(db_path, tmp_path)
    _seed_fleet(db_path)

    out = tmp_path / "fleet.yaml"
    out.write_text("existing", encoding="utf-8")

    rc = main(["export-fleet", "--db", db_path, "--out", str(out)])
    assert rc == 1

    captured = capsys.readouterr()
    assert "--force" in captured.err
    assert out.read_text(encoding="utf-8") == "existing"


def test_cli_export_fleet_force(tmp_path: Path) -> None:
    """``strata export-fleet --force`` overwrites an existing file and exits 0."""
    from strata.__main__ import main

    db_path = str(tmp_path / "v1.db")
    _build_v1_db(db_path, tmp_path)
    _seed_fleet(db_path)

    out = tmp_path / "fleet.yaml"
    out.write_text("old", encoding="utf-8")

    rc = main(["export-fleet", "--db", db_path, "--out", str(out), "--force"])
    assert rc == 0
    assert out.read_text(encoding="utf-8") != "old"
    FleetConfig.load(out)


def test_cli_export_fleet_invalid_data(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """``strata export-fleet`` exits 1 and names the invariant when data is invalid."""
    from strata.__main__ import main

    db_path = str(tmp_path / "v1.db")
    _build_v1_db(db_path, tmp_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L0', 'Executive', 0)")
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L2', 'Team', 2)")
    conn.execute("INSERT INTO scopes (id, name, stratum_id) VALUES ('s_top', 'Top', 'L0')")
    conn.execute("INSERT INTO scopes (id, name, stratum_id) VALUES ('s_bot', 'Bottom', 'L2')")
    conn.execute(
        "INSERT INTO edges (id, from_scope_id, to_scope_id) VALUES ('e_bad', 's_top', 's_bot')"
    )
    conn.commit()
    conn.close()

    out = tmp_path / "fleet.yaml"
    rc = main(["export-fleet", "--db", db_path, "--out", str(out)])
    assert rc == 1

    captured = capsys.readouterr()
    assert "stratum_distance_violation" in captured.err
    assert not out.exists()
