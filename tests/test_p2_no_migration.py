"""v1.15 P2, ADR 0017 — no migration: standing is derived, never stored.

The plan's own words: "There is no standing column and no score." Pinned two ways: the
shipped migration list is unchanged from P1's, and standing_evidence writes nothing to
the database or the summaries directory over a whole record it reads.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from strata.migrator import run_migrations

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"

#: P1's own applied-migrations list — exactly as pinned in tests/test_migrator.py
#: (four call sites there all expect this same list, through 0016). P2 adds none of
#: its own; a PREFIX check (not exact equality) is what "P2 adds none" actually means
#: — a later cycle (P3, 0017) is free to extend the list without this file going stale.
_P1_APPLIED_MIGRATIONS = [
    "0001_initial.sql",
    "0002_drop_fleet_tables.sql",
    "0003_judgment_attempts.sql",
    "0004_operator.sql",
    "0005_publication.sql",
    "0006_optional_skill.sql",
    "0007_failed_judgment_marker.sql",
    "0008_publication_judgment_attempts.sql",
    "0009_publication_relay.sql",
    "0010_change_events.sql",
    "0011_change_event_kinds.sql",
    "0012_directive_unspliced_kind.sql",
    "0013_judgment_summary_version.sql",
    "0014_self_notice.sql",
    "0015_retirement_circumstance.sql",
    "0016_acted_on.sql",
]


def test_shipped_migrations_include_p1s_list_unchanged_and_in_order(tmp_path: Path) -> None:
    """P2 adds no migration: 0001-0016 are exactly P1's own pin, in order, a prefix of
    whatever a later cycle (P3, 0017) legitimately adds on top."""
    applied = run_migrations(str(tmp_path / "strata.db"), migrations_dir=_MIGRATIONS_DIR)
    assert applied[: len(_P1_APPLIED_MIGRATIONS)] == _P1_APPLIED_MIGRATIONS


def _db_content_snapshot(db_path: str) -> dict[str, tuple[int, str]]:
    """{table: (row_count, sha256 of every row's repr, ordered by rowid)} for every table."""
    conn = sqlite3.connect(db_path)
    try:
        tables = sorted(
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        )
        snapshot = {}
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            count = len(rows)
            digest = hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()
            snapshot[table] = (count, digest)
        return snapshot
    finally:
        conn.close()


def _summaries_snapshot(summaries_dir: Path) -> dict[str, str]:
    if not summaries_dir.exists():
        return {}
    return {
        str(p.relative_to(summaries_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(summaries_dir.rglob("*"))
        if p.is_file()
    }


def test_standing_evidence_writes_nothing_anywhere(tmp_path: Path) -> None:
    from strata.fleet_config import FleetConfig
    from strata.record_store import ContributorRef, RecordStore, standing_evidence

    db_path = str(tmp_path / "strata.db")
    summaries_dir = tmp_path / "summaries"
    summaries_dir.mkdir()
    (summaries_dir / "g_test.md").write_text("some pre-existing summary content\n")
    run_migrations(db_path)

    store = RecordStore(db_path)
    contributor = ContributorRef(
        scope_id="g_test", skill="engineer", session_id="s1", ts="2026-01-01T00:00:00Z"
    )
    target = store.append_contribution(
        scope_id="g_test",
        content="An item worth acting on.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=contributor,
    )
    store.record_judgment(
        contribution_id=target.id, decision="accept_as_context", judged_by="scope-manager"
    )
    outcome = store.append_contribution(
        scope_id="g_test",
        content="Acted on it; it held.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=contributor,
        acted_on=target.id,
    )
    store.record_judgment(
        contribution_id=outcome.id, decision="accept_as_context", judged_by="scope-manager"
    )

    fleet = FleetConfig.model_validate(
        {
            "strata": [{"id": "L0", "name": "root", "ordinal": 0}],
            "scopes": [{"id": "g_test", "name": "Test", "stratum_id": "L0"}],
            "edges": [],
        }
    )

    before_db = _db_content_snapshot(db_path)
    before_files = _summaries_snapshot(summaries_dir)

    evidence = standing_evidence(store, fleet, target.id)
    assert evidence  # sanity: the call actually found something to read

    after_db = _db_content_snapshot(db_path)
    after_files = _summaries_snapshot(summaries_dir)

    assert after_db == before_db
    assert after_files == before_files
