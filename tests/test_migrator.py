"""End-to-end tests for the SQL migration runner against the bundled
``migrations/`` directory.

The unit-level tests in ``test_cli.py`` mock the runner; this module exercises
real SQL against a fresh SQLite file to catch data-preservation regressions
in the migrations themselves.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from strata.migrator import run_migrations


def _table_names(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def test_full_chain_drops_fleet_tables_and_preserves_record(tmp_path: Path) -> None:
    """0001 + 0002 applied in order: fleet tables gone, contributions + judgments preserved.

    Guards the rebuild-with-temp-backup pattern in 0002 against future regressions
    where dropping ``judgments`` ahead of the contributions rebuild loses data.
    """
    db_path = str(tmp_path / "migrate.db")

    # Apply 0001 first (alone), then seed sample data.
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    only_0001_dir = tmp_path / "migrations_step1"
    only_0001_dir.mkdir()
    (only_0001_dir / "0001_initial.sql").write_text(
        (migrations_dir / "0001_initial.sql").read_text()
    )
    applied = run_migrations(db_path, migrations_dir=only_0001_dir)
    assert applied == ["0001_initial.sql"]

    # Seed a contribution + judgment + supporting fleet rows.
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L0', 'Top', 0)")
    conn.execute("INSERT INTO scopes (id, name, stratum_id) VALUES ('g_a', 'A', 'L0')")
    conn.execute(
        """INSERT INTO contributions
        (id, scope_id, content, proposed_classification, subject, supersedes,
         contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts)
        VALUES ('c_1', 'g_a', 'hello', 'context', NULL, NULL,
                'g_a', 'tester', 's_1', '2026-05-27T00:00:00Z')"""
    )
    # A second contribution superseding the first — exercises the rebuilt
    # contributions.supersedes self-FK during the INSERT ... SELECT under
    # foreign_keys = ON.
    conn.execute(
        """INSERT INTO contributions
        (id, scope_id, content, proposed_classification, subject, supersedes,
         contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts)
        VALUES ('c_2', 'g_a', 'hello v2', 'directive', NULL, 'c_1',
                'g_a', 'tester', 's_1', '2026-05-27T01:00:00Z')"""
    )
    conn.execute(
        """INSERT INTO judgments
        (id, contribution_id, decision, judged_by, notes)
        VALUES ('j_1', 'c_1', 'accept_as_context', 'scope-manager', 'looks fine')"""
    )
    conn.commit()
    conn.close()

    # Now apply 0002 from the real migrations directory (it's pending).
    applied = run_migrations(db_path, migrations_dir=migrations_dir)
    assert applied == ["0002_drop_fleet_tables.sql"]

    # Fleet tables gone.
    tables = _table_names(db_path)
    assert "strata" not in tables
    assert "scopes" not in tables
    assert "edges" not in tables
    assert "judgments_backup" not in tables  # cleanup succeeded
    assert "contributions" in tables
    assert "judgments" in tables

    # Contributions and judgments preserved end-to-end, including the
    # supersedes self-reference (c_2 → c_1) carried through the rebuild.
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        contribs = conn.execute(
            "SELECT id, scope_id, content, supersedes FROM contributions ORDER BY id"
        ).fetchall()
        judgments = conn.execute(
            "SELECT id, contribution_id, decision FROM judgments ORDER BY id"
        ).fetchall()
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()

    assert contribs == [
        ("c_1", "g_a", "hello", None),
        ("c_2", "g_a", "hello v2", "c_1"),
    ]
    assert judgments == [("j_1", "c_1", "accept_as_context")]
    assert fk_violations == []


def test_idempotent_reapply(tmp_path: Path) -> None:
    """Re-running migrations after a full apply is a no-op."""
    db_path = str(tmp_path / "idempotent.db")
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"

    first = run_migrations(db_path, migrations_dir=migrations_dir)
    assert first == ["0001_initial.sql", "0002_drop_fleet_tables.sql"]

    second = run_migrations(db_path, migrations_dir=migrations_dir)
    assert second == []


@pytest.mark.parametrize("decision", ["accept_as_directive", "accept_as_context", "decline"])
def test_judgment_check_constraint_survives_rebuild(tmp_path: Path, decision: str) -> None:
    """The CHECK constraint on ``judgments.decision`` is reinstated after 0002.

    Guards against the rebuild silently widening the schema.
    """
    db_path = str(tmp_path / f"check_{decision}.db")
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    run_migrations(db_path, migrations_dir=migrations_dir)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """INSERT INTO contributions
        (id, scope_id, content, proposed_classification, subject, supersedes,
         contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts)
        VALUES ('c_x', 'g_a', 'hi', 'context', NULL, NULL,
                'g_a', 'tester', 's_1', '2026-05-27T00:00:00Z')"""
    )
    conn.execute(
        "INSERT INTO judgments (id, contribution_id, decision, judged_by) VALUES (?, ?, ?, ?)",
        ("j_x", "c_x", decision, "scope-manager"),
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO judgments (id, contribution_id, decision, judged_by) VALUES (?, ?, ?, ?)",
            ("j_bad", "c_x", "not_a_valid_decision", "scope-manager"),
        )

    conn.close()
