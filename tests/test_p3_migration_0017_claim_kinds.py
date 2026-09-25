"""v1.15 P3, ADR 0017 — migration 0017 widens change_events.kind, nothing else.

Pins: applying 0017 changes `change_events` in exactly one way — the CHECK constraint
on `kind` gains `claim_corrected` and `claim_superseded` — and touches nothing else in
the schema (no new/dropped table, index, or column; no other table moves; existing
rows and the OLD kind values both still work).
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

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
        snapshot = {"tables": tables, "columns": {}, "indexes": {}}
        for table in tables:
            snapshot["columns"][table] = [
                (r[1], r[2], r[3]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            ]  # (name, type, notnull)
            snapshot["indexes"][table] = sorted(
                r[1] for r in conn.execute(f"PRAGMA index_list({table})").fetchall()
            )
        return snapshot
    finally:
        conn.close()


def _migrations_dir_without_0017(tmp_path: Path) -> Path:
    scratch = tmp_path / "migrations_pre_0017"
    scratch.mkdir()
    for f in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        if f.name != "0017_claim_kinds.sql":
            shutil.copy(f, scratch / f.name)
    return scratch


def test_0017_only_widens_the_kind_check_nothing_else_in_the_schema(tmp_path: Path) -> None:
    before_path = str(tmp_path / "before.db")
    run_migrations(before_path, migrations_dir=_migrations_dir_without_0017(tmp_path))
    before = _schema_snapshot(before_path)

    after_path = str(tmp_path / "after.db")
    shutil.copy(before_path, after_path)
    run_migrations(after_path, migrations_dir=_MIGRATIONS_DIR)
    after = _schema_snapshot(after_path)

    assert after["tables"] == before["tables"]
    assert after["indexes"] == before["indexes"]
    for table in before["tables"]:
        assert after["columns"][table] == before["columns"][table], table


def test_existing_change_event_rows_survive_verbatim(tmp_path: Path) -> None:
    before_path = str(tmp_path / "before.db")
    run_migrations(before_path, migrations_dir=_migrations_dir_without_0017(tmp_path))
    conn = sqlite3.connect(before_path)
    conn.execute(
        "INSERT INTO contributions (id, scope_id, content, proposed_classification, "
        "contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts) "
        "VALUES ('c_1', 'g_x', 'x', 'context', 'g_x', 'eng', 's1', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO change_events (id, change_id, contribution_id, scope_id, item_id, kind) "
        "VALUES ('ce_1', 'chg_1', 'c_1', 'g_x', 'c_1', 'published')"
    )
    conn.commit()
    before_row = conn.execute("SELECT * FROM change_events WHERE id = 'ce_1'").fetchone()
    conn.close()

    run_migrations(before_path, migrations_dir=_MIGRATIONS_DIR)
    conn = sqlite3.connect(before_path)
    after_row = conn.execute("SELECT * FROM change_events WHERE id = 'ce_1'").fetchone()
    conn.close()
    assert after_row == before_row


def test_the_old_kind_values_are_all_still_accepted(tmp_path: Path) -> None:
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path, migrations_dir=_MIGRATIONS_DIR)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO contributions (id, scope_id, content, proposed_classification, "
        "contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts) "
        "VALUES ('c_1', 'g_x', 'x', 'context', 'g_x', 'eng', 's1', '2026-01-01')"
    )
    old_kinds = [
        "published",
        "amended",
        "withdrawn",
        "directive_appended",
        "directive_superseded",
        "directive_retired",
        "directive_unspliced",
        "operator_directive_changed",
    ]
    for i, kind in enumerate(old_kinds):
        conn.execute(
            "INSERT INTO change_events (id, change_id, contribution_id, scope_id, item_id, kind) "
            "VALUES (?, ?, 'c_1', 'g_x', 'c_1', ?)",
            (f"ce_{i}", f"chg_{i}", kind),
        )
    conn.commit()
    conn.close()


def test_the_two_new_kind_values_are_accepted_and_unknown_ones_still_rejected(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path, migrations_dir=_MIGRATIONS_DIR)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO contributions (id, scope_id, content, proposed_classification, "
        "contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts) "
        "VALUES ('c_1', 'g_x', 'x', 'context', 'g_x', 'eng', 's1', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO change_events (id, change_id, contribution_id, scope_id, item_id, kind) "
        "VALUES ('ce_c', 'chg_c', 'c_1', 'g_x', 'c_1', 'claim_corrected')"
    )
    conn.execute(
        "INSERT INTO change_events (id, change_id, contribution_id, scope_id, item_id, kind) "
        "VALUES ('ce_s', 'chg_s', 'c_1', 'g_x', 'c_1', 'claim_superseded')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO change_events (id, change_id, contribution_id, scope_id, item_id, kind) "
            "VALUES ('ce_bad', 'chg_bad', 'c_1', 'g_x', 'c_1', 'not_a_real_kind')"
        )
    conn.close()
