"""v1.15 P1, ADR 0017 — migration 0016 adds exactly one column, nothing else.

Pins the plan's survey claim ("the only inter-contribution reference is `supersedes`
today"): applying 0016 must change `contributions` in exactly one way — a new nullable
`acted_on` column with one new FK to `contributions(id)` — and touch nothing else in the
schema (no new/dropped table, index, trigger; no other table's columns move).
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from strata.migrator import run_migrations

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"


def _schema_snapshot(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        tables = sorted(
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name != '_migrations'"
            ).fetchall()
        )
        snapshot = {"tables": tables, "columns": {}, "foreign_keys": {}}
        for table in tables:
            snapshot["columns"][table] = [
                (r[1], r[2], r[3]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            ]  # (name, type, notnull)
            snapshot["foreign_keys"][table] = [
                (r[3], r[2], r[4])
                for r in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            ]  # (from_col, to_table, to_col)
        return snapshot
    finally:
        conn.close()


def _migrations_dir_without_0016(tmp_path: Path) -> Path:
    scratch = tmp_path / "migrations_pre_0016"
    scratch.mkdir()
    for f in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        if f.name != "0016_acted_on.sql":
            shutil.copy(f, scratch / f.name)
    return scratch


def test_0016_adds_exactly_one_nullable_column_and_one_fk(tmp_path: Path) -> None:
    before_path = str(tmp_path / "before.db")
    run_migrations(before_path, migrations_dir=_migrations_dir_without_0016(tmp_path))
    before = _schema_snapshot(before_path)

    after_path = str(tmp_path / "after.db")
    shutil.copy(before_path, after_path)
    run_migrations(after_path, migrations_dir=_MIGRATIONS_DIR)
    after = _schema_snapshot(after_path)

    # No table, other than contributions' columns, changed at all.
    assert after["tables"] == before["tables"]
    for table in before["tables"]:
        if table == "contributions":
            continue
        assert after["columns"][table] == before["columns"][table], table
        assert after["foreign_keys"][table] == before["foreign_keys"][table], table

    before_cols = {c[0] for c in before["columns"]["contributions"]}
    after_cols = {c[0] for c in after["columns"]["contributions"]}
    assert after_cols - before_cols == {"acted_on"}
    assert before_cols - after_cols == set()  # nothing dropped

    acted_on_row = next(c for c in after["columns"]["contributions"] if c[0] == "acted_on")
    _name, col_type, notnull = acted_on_row
    assert notnull == 0, "acted_on must be nullable"

    before_fks = set(before["foreign_keys"]["contributions"])
    after_fks = set(after["foreign_keys"]["contributions"])
    new_fks = after_fks - before_fks
    assert new_fks == {("acted_on", "contributions", "id")}
    # The pin itself: before 0016, `supersedes` was the ONLY inter-contribution reference.
    assert before_fks == {("supersedes", "contributions", "id")}


def test_0016_is_a_pure_addition_no_other_migration_needed_updating(tmp_path: Path) -> None:
    """Applying the full chain twice (idempotent re-run) leaves the schema unchanged."""
    db_path = str(tmp_path / "twice.db")
    run_migrations(db_path, migrations_dir=_MIGRATIONS_DIR)
    once = _schema_snapshot(db_path)
    run_migrations(db_path, migrations_dir=_MIGRATIONS_DIR)
    twice = _schema_snapshot(db_path)
    assert once == twice


def test_acted_on_fk_is_enforced(tmp_path: Path) -> None:
    db_path = str(tmp_path / "fk.db")
    run_migrations(db_path, migrations_dir=_MIGRATIONS_DIR)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    # contributions.scope_id carries no FK (the fleet lives in fleet.yaml, not the DB,
    # since 0002) — any non-null string is a legal scope_id here.
    import sqlite3 as _sqlite3

    try:
        conn.execute(
            """
            INSERT INTO contributions (
                id, scope_id, content, proposed_classification,
                contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts,
                acted_on
            ) VALUES ('c_1', 'g_x', 'x', 'context', 'g_x', 'eng', 's1', '2026-01-01', 'c_missing')
            """
        )
        conn.commit()
    except _sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("acted_on referencing a missing contribution must be rejected")
    finally:
        conn.close()
