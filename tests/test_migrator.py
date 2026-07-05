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
    migrations_dir = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"
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
    migrations_dir = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"

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
    migrations_dir = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"
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


# ---------------------------------------------------------------------------
# Crash-mid-migration tests (issue #58): the migration script and its
# ``_migrations`` tracking row must commit or roll back together, atomically,
# so a crash at any point during a migration leaves the DB such that a plain
# re-run converges with no manual intervention.
# ---------------------------------------------------------------------------


def test_crash_mid_script_leaves_no_half_schema_and_no_tracking_row(tmp_path: Path) -> None:
    """A migration whose second statement is invalid SQL leaves nothing behind.

    The first statement (a CREATE TABLE) would succeed if run on its own,
    but the whole file now runs in one explicit transaction, so the second
    statement's failure rolls the first one back too — no half-applied
    schema — and the tracking row for this migration is never inserted.
    """
    db_path = str(tmp_path / "crash.db")
    bad_migrations_dir = tmp_path / "migrations_bad"
    bad_migrations_dir.mkdir()
    (bad_migrations_dir / "0001_bad.sql").write_text(
        "CREATE TABLE half_applied (id TEXT PRIMARY KEY);\nTHIS IS NOT VALID SQL;\n"
    )

    with pytest.raises(sqlite3.OperationalError):
        run_migrations(db_path, migrations_dir=bad_migrations_dir)

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        tracked = conn.execute(
            "SELECT name FROM _migrations WHERE name = '0001_bad.sql'"
        ).fetchall()
    finally:
        conn.close()

    # No half-schema: the CREATE TABLE that ran before the crash was rolled back.
    assert "half_applied" not in tables
    # No tracking row: the migration was never recorded as applied.
    assert tracked == []


def test_reapply_after_crash_converges(tmp_path: Path) -> None:
    """After a crashed migration file is fixed, a plain re-run applies it cleanly.

    No manual cleanup is needed: the crash left no tracking row and no
    partial schema (see the test above), so the fixed file is simply picked
    up as pending, exactly as if it had never been attempted.
    """
    db_path = str(tmp_path / "crash.db")
    migrations_dir = tmp_path / "migrations_bad"
    migrations_dir.mkdir()
    bad_file = migrations_dir / "0001_bad.sql"
    bad_file.write_text(
        "CREATE TABLE half_applied (id TEXT PRIMARY KEY);\nTHIS IS NOT VALID SQL;\n"
    )

    with pytest.raises(sqlite3.OperationalError):
        run_migrations(db_path, migrations_dir=migrations_dir)

    # An operator fixes the migration file; nothing else is touched.
    bad_file.write_text("CREATE TABLE half_applied (id TEXT PRIMARY KEY);\n")

    applied = run_migrations(db_path, migrations_dir=migrations_dir)
    assert applied == ["0001_bad.sql"]

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        tracked = conn.execute(
            "SELECT name FROM _migrations WHERE name = '0001_bad.sql'"
        ).fetchall()
    finally:
        conn.close()

    assert "half_applied" in tables
    assert tracked == [("0001_bad.sql",)]


class _CrashOnStatement:
    """Wraps a real ``sqlite3.Connection``; raises the first time *trigger*
    appears in an executed statement.

    Used to simulate a crash at a precise point inside ``run_migrations``'s
    per-file transaction — e.g. exactly at the tracking ``INSERT``, after
    the migration script's own statements have already run but not yet
    committed.
    """

    def __init__(self, real: sqlite3.Connection, trigger: str) -> None:
        self._real = real
        self._trigger = trigger
        self._raised = False

    def execute(self, sql: str, parameters: tuple = ()) -> sqlite3.Cursor:
        if not self._raised and self._trigger in sql:
            self._raised = True
            raise sqlite3.OperationalError("simulated crash")
        return self._real.execute(sql, parameters)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def test_crash_at_tracking_insert_rolls_back_script_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Applied but unrecorded" is impossible: script + tracking row are one transaction.

    Before this fix, ``executescript`` committed the migration's own
    statements (or, for a self-wrapping file like 0002, its BEGIN…COMMIT
    committed) as soon as it returned, and the tracking ``INSERT`` was a
    *separate* transaction — a crash between the two would leave the
    script's schema changes applied with no tracking row. This test
    crashes exactly at the tracking ``INSERT`` and checks that the script's
    own effects are rolled back right along with it, and that a plain
    re-run afterwards converges.
    """
    db_path = str(tmp_path / "atomic.db")
    migrations_dir = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"

    real_connect = sqlite3.connect

    def fake_connect(path: str, *args: object, **kwargs: object) -> _CrashOnStatement:
        real_conn = real_connect(path, *args, **kwargs)
        return _CrashOnStatement(real_conn, trigger="INSERT INTO _migrations")

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    with pytest.raises(sqlite3.OperationalError):
        run_migrations(db_path, migrations_dir=migrations_dir)
    monkeypatch.undo()

    conn = real_connect(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        tracked = conn.execute("SELECT name FROM _migrations").fetchall()
    finally:
        conn.close()

    # 0001's own schema changes were rolled back along with the failed
    # tracking INSERT — no "applied but unrecorded" state.
    assert "strata" not in tables
    assert tracked == []

    # A plain re-run (no monkeypatch) converges cleanly with no manual
    # intervention.
    applied = run_migrations(db_path, migrations_dir=migrations_dir)
    assert applied == ["0001_initial.sql", "0002_drop_fleet_tables.sql"]
