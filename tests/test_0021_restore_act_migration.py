"""Migration 0021 (the restore act) on a POPULATED database.

Aron, reproduced against the strata-evals `ol-016-rep0.db` fixture: 0021's
original `publication_acts`/`change_events` rebuild ran under
``PRAGMA foreign_keys = ON`` (the migrator sets it), and `DROP TABLE
publication_acts` performs an implicit DELETE of every row first — which
`publication_judgments`/`publication_judgment_attempts` (both
``act_id REFERENCES publication_acts(id)``) then violate on any database
that actually has rows in them. The empty-chain migration tests never
caught this. Fixed by backing up and dropping both referencing tables
BEFORE the rebuild, recreating and restoring them after — the exact shape
migration 0006 already established for `publication_judgments`, extended
here to `publication_judgment_attempts` (which did not exist yet in 0006).

This file seeds a real, non-empty database through 0020, migrates it
through 0021, and asserts every row survives with zero FK violations.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from strata.migrator import run_migrations

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"


def _migrations_dir_up_to(tmp_path: Path, last_name: str) -> Path:
    scratch = tmp_path / f"migrations_through_{last_name}"
    scratch.mkdir()
    for f in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        (scratch / f.name).write_bytes(f.read_bytes())
        if f.name == last_name:
            break
    return scratch


def _seed_populated_0020_db(db_path: str, tmp_path: Path) -> None:
    """A real contribution, two publish acts, one withdraw act (with a
    judgment row and a failed-attempt row on another act), a claim-corrected
    change event, and a claim_carrier_checks row — one row in every table
    that either references `publication_acts`/`change_events` or will be
    rebuilt by 0021, so the migration has real data to carry across."""
    run_migrations(
        db_path,
        migrations_dir=_migrations_dir_up_to(tmp_path, "0020_claim_carrier_checks.sql"),
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO contributions (id, scope_id, content, proposed_classification, "
            "contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts) "
            "VALUES ('c_1', 'g_x', 'The service listens on port 8443.', 'context', "
            "'g_x', 'eng', 's1', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO publication_acts (id, scope_id, act, kind, content, subject, "
            'anchors, withdraws, "trigger", proposer_scope_id, proposer_skill, '
            "proposer_session_id, proposer_ts) VALUES "
            "('pub_1', 'g_x', 'publish', 'context', 'The service listens on port 8443.', "
            "'port', '[\"subject:port\"]', NULL, NULL, 'g_x', 'eng', 's1', "
            "'2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO publication_judgments (id, act_id, decision, judged_by) "
            "VALUES ('pubj_1', 'pub_1', 'accept', 'scope-manager')"
        )
        conn.execute(
            "INSERT INTO publication_acts (id, scope_id, act, kind, content, subject, "
            'anchors, withdraws, "trigger", proposer_scope_id, proposer_skill, '
            "proposer_session_id, proposer_ts) VALUES "
            "('pub_2', 'g_x', 'withdraw', NULL, NULL, NULL, NULL, 'pub_1', 'c_1', "
            "'g_x', 'eng', 's1', '2026-01-01T00:01:00Z')"
        )
        conn.execute(
            "INSERT INTO publication_judgment_attempts "
            "(id, act_id, error_class, message, outcome) VALUES "
            "('pubja_1', 'pub_1', 'RuntimeError', 'judge is unavailable', 'judge_failed')"
        )
        conn.execute(
            "INSERT INTO change_events (id, change_id, contribution_id, scope_id, "
            "source_scope_id, item_id, kind, before, after, self_notice, shown_at) "
            "VALUES ('ce_1', 'chg_1', 'c_1', 'g_x', 'g_x', 'pub_1', 'claim_corrected', "
            "'The service listens on port 8443.', 'The right port is unknown.', 1, NULL)"
        )
        conn.execute(
            "INSERT INTO claim_carrier_checks (id, change_id, scope_id, "
            "corrected_claim_id, item_id, outcome) VALUES "
            "('ccc_1', 'chg_1', 'g_x', 'c_1', 'pub_1', 'carries')"
        )
        conn.commit()
    finally:
        conn.close()


def test_0021_migrates_a_populated_database_with_no_fk_violations(tmp_path: Path) -> None:
    db_path = str(tmp_path / "populated.db")
    _seed_populated_0020_db(db_path, tmp_path)

    before_counts = _table_counts(
        db_path,
        [
            "contributions",
            "publication_acts",
            "publication_judgments",
            "publication_judgment_attempts",
            "change_events",
            "claim_carrier_checks",
        ],
    )

    applied = run_migrations(db_path, migrations_dir=_MIGRATIONS_DIR)
    assert "0021_restore_act.sql" in applied

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert violations == []
    finally:
        conn.close()

    after_counts = _table_counts(
        db_path,
        [
            "contributions",
            "publication_acts",
            "publication_judgments",
            "publication_judgment_attempts",
            "change_events",
            "claim_carrier_checks",
        ],
    )
    assert after_counts == before_counts

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT restores, restored_by, acknowledged FROM publication_acts WHERE id = 'pub_1'"
        ).fetchone()
        assert row == (None, None, 0)
        claim_id_row = conn.execute(
            "SELECT claim_id FROM change_events WHERE id = 'ce_1'"
        ).fetchone()
        assert claim_id_row == (None,)
        assert conn.execute("SELECT COUNT(*) FROM claim_corrections").fetchone() == (0,)
    finally:
        conn.close()


def _table_counts(db_path: str, tables: list[str]) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}  # noqa: S608
    finally:
        conn.close()
