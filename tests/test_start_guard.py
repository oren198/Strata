"""Tests for the V1 → V1.2 upgrade guard in ``strata start``.

The guard runs before ``run_migrations`` in ``cmd_start`` and refuses to
proceed when a V1 fleet config lives in the DB but no ``fleet.yaml`` exists
at the resolved path, and migration ``0002_drop_fleet_tables.sql`` is still
pending.

Seven cases are covered:
1. Triggers refuse (all four conditions true).
2. --skip-upgrade-check bypasses the guard.
3. fleet.yaml present → proceeds.
4. DB file absent (fresh install) → proceeds.
5. 0002 already applied → proceeds.
6. 0002 pending but V1 tables absent → proceeds.
7. Read-only confirmation: source DB is not mutated when guard fires.

Vocabulary follows CONTEXT.md verbatim: scope, stratum, fleet, contribution.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from strata.__main__ import _v1_upgrade_guard_should_refuse, main
from strata.migrator import run_migrations
from strata.project_config import StoragePaths

# ---------------------------------------------------------------------------
# Shared test fixtures and helpers
# ---------------------------------------------------------------------------

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"


def _build_v1_db(db_path: str, tmp_path: Path) -> None:
    """Apply only 0001_initial.sql, leaving 0002_drop_fleet_tables.sql unapplied.

    Uses the same isolated-migrations-dir pattern as ``test_fleet_export.py``
    and ``test_migrator.py`` so that the real 0002 file on disk cannot
    accidentally be applied.
    """
    only_0001 = tmp_path / "migrations_0001_only"
    only_0001.mkdir()
    (only_0001 / "0001_initial.sql").write_text(
        (_MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    run_migrations(db_path, migrations_dir=only_0001)


def _seed_v1_fleet(db_path: str) -> None:
    """Seed minimal V1 fleet rows (strata, scopes, edges)."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L0', 'Executive', 0)")
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L1', 'Function', 1)")
    conn.execute("INSERT INTO scopes (id, name, stratum_id) VALUES ('g_ceo', 'CEO', 'L0')")
    conn.execute("INSERT INTO scopes (id, name, stratum_id) VALUES ('g_eng', 'Engineering', 'L1')")
    conn.execute(
        "INSERT INTO edges (id, from_scope_id, to_scope_id) VALUES ('e1', 'g_ceo', 'g_eng')"
    )
    conn.commit()
    conn.close()


def _apply_both_migrations(db_path: str) -> None:
    """Apply both 0001 and 0002 (both migrations), simulating an already-migrated DB."""
    run_migrations(db_path, migrations_dir=_MIGRATIONS_DIR)


# ---------------------------------------------------------------------------
# Helper: mock context for cmd_start (prevents real uvicorn + run_migrations)
# ---------------------------------------------------------------------------


def _start_patches(db_path: str, fleet_yaml_path: str):
    """Return a context manager that patches run_migrations, uvicorn, and settings."""
    return [
        patch("strata.migrator.run_migrations", return_value=[]),
        patch("uvicorn.run"),
        patch(
            "strata.settings.get_settings",
            return_value=MagicMock(fleet_yaml_path=fleet_yaml_path),
        ),
        patch(
            "strata.__main__._fleet_config_default",
            return_value=fleet_yaml_path,
        ),
    ]


# ---------------------------------------------------------------------------
# Test 1: Guard triggers refuse when all four conditions hold
# ---------------------------------------------------------------------------


def test_guard_refuses_when_v1_db_and_no_fleet_yaml(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """All four conditions true → returns 1, includes hint, migrations NOT called."""
    db_path = str(tmp_path / "strata.db")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")

    _build_v1_db(db_path, tmp_path)
    _seed_v1_fleet(db_path)
    # fleet.yaml is intentionally absent.

    with (
        patch("strata.migrator.run_migrations", return_value=[]) as mock_migrate,
        patch("uvicorn.run"),
        patch(
            "strata.__main__._storage_paths",
            return_value=StoragePaths(
                db_path=db_path,
                summaries_dir=str(Path(db_path).parent / "summaries"),
                fleet_yaml_path=fleet_yaml_path,
                source="env",
                project_root=None,
            ),
        ),
    ):
        rc = main(["start"])

    assert rc == 1
    mock_migrate.assert_not_called()

    err = capsys.readouterr().err
    assert "strata export-fleet" in err
    assert "--skip-upgrade-check" in err
    assert db_path in err
    assert fleet_yaml_path in err


# ---------------------------------------------------------------------------
# Test 2: --skip-upgrade-check bypasses the guard → proceeds normally
# ---------------------------------------------------------------------------


def test_guard_bypassed_by_skip_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Same V1 setup + --skip-upgrade-check → run_migrations is called, returns 0."""
    db_path = str(tmp_path / "strata.db")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")

    _build_v1_db(db_path, tmp_path)
    _seed_v1_fleet(db_path)
    # fleet.yaml intentionally absent.

    with (
        patch("strata.migrator.run_migrations", return_value=[]) as mock_migrate,
        patch("uvicorn.run"),
        patch(
            "strata.__main__._storage_paths",
            return_value=StoragePaths(
                db_path=db_path,
                summaries_dir=str(Path(db_path).parent / "summaries"),
                fleet_yaml_path=fleet_yaml_path,
                source="env",
                project_root=None,
            ),
        ),
    ):
        rc = main(["start", "--skip-upgrade-check"])

    assert rc == 0
    mock_migrate.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: fleet.yaml present → guard does not refuse
# ---------------------------------------------------------------------------


def test_guard_passes_when_fleet_yaml_exists(
    tmp_path: Path,
) -> None:
    """V1 DB + fleet.yaml present → no refuse (guard condition 4 false)."""
    db_path = str(tmp_path / "strata.db")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")

    _build_v1_db(db_path, tmp_path)
    _seed_v1_fleet(db_path)
    Path(fleet_yaml_path).write_text("# fleet.yaml present\n", encoding="utf-8")

    with (
        patch("strata.migrator.run_migrations", return_value=[]) as mock_migrate,
        patch("uvicorn.run"),
        patch(
            "strata.__main__._storage_paths",
            return_value=StoragePaths(
                db_path=db_path,
                summaries_dir=str(Path(db_path).parent / "summaries"),
                fleet_yaml_path=fleet_yaml_path,
                source="env",
                project_root=None,
            ),
        ),
    ):
        rc = main(["start"])

    assert rc == 0
    mock_migrate.assert_called_once()


# ---------------------------------------------------------------------------
# Test 4: DB file absent (fresh install) → guard does not refuse
# ---------------------------------------------------------------------------


def test_guard_passes_on_fresh_install(
    tmp_path: Path,
) -> None:
    """No DB file → guard condition 1 is false → no refuse."""
    db_path = str(tmp_path / "does_not_exist.db")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")
    # Neither DB nor fleet.yaml exists.

    with (
        patch("strata.migrator.run_migrations", return_value=[]) as mock_migrate,
        patch("uvicorn.run"),
        patch(
            "strata.__main__._storage_paths",
            return_value=StoragePaths(
                db_path=db_path,
                summaries_dir=str(Path(db_path).parent / "summaries"),
                fleet_yaml_path=fleet_yaml_path,
                source="env",
                project_root=None,
            ),
        ),
    ):
        rc = main(["start"])

    assert rc == 0
    mock_migrate.assert_called_once()


# ---------------------------------------------------------------------------
# Test 5: 0002 already applied → guard does not refuse
# ---------------------------------------------------------------------------


def test_guard_passes_when_0002_already_applied(
    tmp_path: Path,
) -> None:
    """Both migrations applied → 0002 is not pending → no refuse."""
    db_path = str(tmp_path / "strata.db")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")

    _apply_both_migrations(db_path)
    # fleet.yaml intentionally absent.

    with (
        patch("strata.migrator.run_migrations", return_value=[]) as mock_migrate,
        patch("uvicorn.run"),
        patch(
            "strata.__main__._storage_paths",
            return_value=StoragePaths(
                db_path=db_path,
                summaries_dir=str(Path(db_path).parent / "summaries"),
                fleet_yaml_path=fleet_yaml_path,
                source="env",
                project_root=None,
            ),
        ),
    ):
        rc = main(["start"])

    assert rc == 0
    mock_migrate.assert_called_once()


# ---------------------------------------------------------------------------
# Test 6: 0002 pending but V1 tables absent → guard does not refuse
# ---------------------------------------------------------------------------


def test_guard_passes_when_v1_tables_absent(
    tmp_path: Path,
) -> None:
    """DB exists, migration pending, but fleet tables are absent → no refuse.

    Edge case: a DB that has a _migrations table (or not) but was never
    a V1 fleet DB, so it has no strata/scopes/edges tables.  The guard
    must not fire in this case.
    """
    db_path = str(tmp_path / "strata.db")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")

    # Create a DB with only the _migrations table, no fleet tables.
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE _migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.execute("INSERT INTO _migrations (name) VALUES ('0001_initial.sql')")
    # 0002 is NOT in _migrations → migration pending.
    conn.commit()
    conn.close()
    # fleet.yaml absent.

    with (
        patch("strata.migrator.run_migrations", return_value=[]) as mock_migrate,
        patch("uvicorn.run"),
        patch(
            "strata.__main__._storage_paths",
            return_value=StoragePaths(
                db_path=db_path,
                summaries_dir=str(Path(db_path).parent / "summaries"),
                fleet_yaml_path=fleet_yaml_path,
                source="env",
                project_root=None,
            ),
        ),
    ):
        rc = main(["start"])

    assert rc == 0
    mock_migrate.assert_called_once()


# ---------------------------------------------------------------------------
# Test 7: Read-only confirmation — source DB is not mutated when guard fires
# ---------------------------------------------------------------------------


def test_guard_does_not_mutate_source_db(tmp_path: Path) -> None:
    """After a refuse, the source DB retains all V1 fleet rows (no mutations)."""
    db_path = str(tmp_path / "strata.db")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")

    _build_v1_db(db_path, tmp_path)
    _seed_v1_fleet(db_path)
    # fleet.yaml intentionally absent → guard will refuse.

    with (
        patch("strata.migrator.run_migrations", return_value=[]),
        patch("uvicorn.run"),
        patch(
            "strata.__main__._storage_paths",
            return_value=StoragePaths(
                db_path=db_path,
                summaries_dir=str(Path(db_path).parent / "summaries"),
                fleet_yaml_path=fleet_yaml_path,
                source="env",
                project_root=None,
            ),
        ),
    ):
        rc = main(["start"])

    assert rc == 1  # guard fired

    # Verify the DB was not mutated: all V1 fleet tables and rows still present.
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "strata" in tables
        assert "scopes" in tables
        assert "edges" in tables

        strata_count = conn.execute("SELECT COUNT(*) FROM strata").fetchone()[0]
        scopes_count = conn.execute("SELECT COUNT(*) FROM scopes").fetchone()[0]
        edges_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    finally:
        conn.close()

    assert strata_count == 2
    assert scopes_count == 2
    assert edges_count == 1


# ---------------------------------------------------------------------------
# Unit-level tests for the helper function itself
# ---------------------------------------------------------------------------


def test_helper_returns_false_when_skip_true(tmp_path: Path) -> None:
    """_v1_upgrade_guard_should_refuse returns False immediately when skip=True."""
    db_path = str(tmp_path / "strata.db")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")

    # Even if we set up the worst-case V1 DB, skip=True must short-circuit.
    _build_v1_db(db_path, tmp_path)
    _seed_v1_fleet(db_path)

    result = _v1_upgrade_guard_should_refuse(db_path, fleet_yaml_path, skip=True)
    assert result is False


def test_helper_returns_true_for_worst_case(tmp_path: Path) -> None:
    """_v1_upgrade_guard_should_refuse returns True when all four conditions hold."""
    db_path = str(tmp_path / "strata.db")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")

    _build_v1_db(db_path, tmp_path)
    _seed_v1_fleet(db_path)
    # fleet.yaml absent.

    result = _v1_upgrade_guard_should_refuse(db_path, fleet_yaml_path, skip=False)
    assert result is True


def test_helper_handles_no_migrations_table(tmp_path: Path) -> None:
    """Guard treats a DB with no _migrations table as 'migration pending'."""
    db_path = str(tmp_path / "bare.db")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")

    # Create a DB with V1 fleet tables but NO _migrations tracking table.
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE strata (id TEXT PRIMARY KEY, name TEXT, ordinal INTEGER)")
    conn.execute("CREATE TABLE scopes (id TEXT PRIMARY KEY, name TEXT, stratum_id TEXT)")
    conn.execute("CREATE TABLE edges (id TEXT PRIMARY KEY, from_scope_id TEXT, to_scope_id TEXT)")
    conn.commit()
    conn.close()
    # fleet.yaml absent.

    result = _v1_upgrade_guard_should_refuse(db_path, fleet_yaml_path, skip=False)
    assert result is True
