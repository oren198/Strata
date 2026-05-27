"""Tests for the Strata record store (src/strata/record_store.py).

Under ADR 0002, the record store owns only ``contributions`` and ``judgments``.
Fleet configuration (strata, scopes, edges) is owned by
:class:`~strata.fleet_config.FleetConfig`.

Each test gets its own fresh SQLite database via pytest's ``tmp_path``
fixture — no shared state between tests.  Migrations are applied as the
first step of each test via the ``store`` fixture.

Vocabulary follows CONTEXT.md: scope, stratum, contribution, record, etc.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Make scripts/ importable without installing it as a package.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from run_migrations import run_migrations  # noqa: E402

from strata.record_store import (  # noqa: E402
    ContributorRef,
    RecordStore,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_CONTRIBUTOR = ContributorRef(
    scope_id="g_ext001",
    skill="code-writer",
    session_id="sess_abc",
    ts="2026-05-23T00:00:00Z",
)


def _apply_migrations(db_path: str) -> None:
    run_migrations(db_path)


def _open_store(db_path: str) -> RecordStore:
    """Apply migrations then open a RecordStore against *db_path*."""
    _apply_migrations(db_path)
    return RecordStore(db_path)


# ---------------------------------------------------------------------------
# Scenario 1 — Migration runner idempotency
# ---------------------------------------------------------------------------


def test_migration_runner_idempotent(tmp_path: Path) -> None:
    """Running the migration runner twice on a fresh DB produces no errors and
    no duplicate rows in _migrations."""
    db_path = str(tmp_path / "strata.db")

    run_migrations(db_path)
    run_migrations(db_path)  # second run must be a no-op

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT name FROM _migrations").fetchall()
    conn.close()

    names = [r[0] for r in rows]
    assert names.count("0001_initial.sql") == 1, (
        f"Expected exactly 1 entry for 0001_initial.sql, got: {names}"
    )


# ---------------------------------------------------------------------------
# Scenario 2 — Migration runner picks up only new migrations on second run
# ---------------------------------------------------------------------------


def test_migration_runner_applies_only_new(tmp_path: Path) -> None:
    """After a first run that applied migration N, a second run skips N and only
    applies new files added between runs."""
    db_path = str(tmp_path / "strata.db")

    # First run — applies existing migrations.
    run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    applied_after_first = {r[0] for r in conn.execute("SELECT name FROM _migrations").fetchall()}
    conn.close()

    assert "0001_initial.sql" in applied_after_first

    # Write a temporary migration to the real migrations folder, run, then remove.
    migrations_dir = Path(__file__).parent.parent / "migrations"
    temp_migration = migrations_dir / "0099_temp_test.sql"
    try:
        temp_migration.write_text("-- temporary test migration\n", encoding="utf-8")
        run_migrations(db_path)  # second run

        conn = sqlite3.connect(db_path)
        applied_after_second = {
            r[0] for r in conn.execute("SELECT name FROM _migrations").fetchall()
        }
        conn.close()

        assert "0001_initial.sql" in applied_after_second
        assert "0099_temp_test.sql" in applied_after_second

        conn2 = sqlite3.connect(db_path)
        all_names = [r[0] for r in conn2.execute("SELECT name FROM _migrations").fetchall()]
        conn2.close()
        assert all_names.count("0001_initial.sql") == 1
        assert all_names.count("0099_temp_test.sql") == 1
    finally:
        if temp_migration.exists():
            temp_migration.unlink()


# ---------------------------------------------------------------------------
# Scenario 3 — Append a contribution, list returns it
# ---------------------------------------------------------------------------


def test_append_and_list_contribution(tmp_path: Path) -> None:
    """Appending a contribution to a scope's record and listing returns it."""
    db_path = str(tmp_path / "strata.db")
    with _open_store(db_path) as rs:
        c = rs.append_contribution(
            scope_id="g_ceo",
            content="All new services must default to read-only mode.",
            proposed_classification="directive",
            subject="service defaults",
            supersedes=None,
            contributor=_CONTRIBUTOR,
        )

        contributions = rs.list_contributions(scope_id="g_ceo")

    assert len(contributions) == 1
    assert contributions[0].id == c.id
    assert contributions[0].content == "All new services must default to read-only mode."
    assert contributions[0].proposed_classification == "directive"
    assert contributions[0].subject == "service defaults"
    assert contributions[0].contributor.skill == "code-writer"


# ---------------------------------------------------------------------------
# Scenario 4 — Contributions to two scopes are isolated per list call
# ---------------------------------------------------------------------------


def test_list_contributions_isolated_per_scope(tmp_path: Path) -> None:
    """list_contributions for scope A must not return contributions from scope B."""
    db_path = str(tmp_path / "strata.db")
    with _open_store(db_path) as rs:
        rs.append_contribution(
            scope_id="g_scope_a",
            content="Contribution to scope A.",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_CONTRIBUTOR,
        )
        rs.append_contribution(
            scope_id="g_scope_b",
            content="Contribution to scope B.",
            proposed_classification="directive",
            subject=None,
            supersedes=None,
            contributor=_CONTRIBUTOR,
        )

        contributions_a = rs.list_contributions(scope_id="g_scope_a")
        contributions_b = rs.list_contributions(scope_id="g_scope_b")

    assert len(contributions_a) == 1
    assert contributions_a[0].content == "Contribution to scope A."

    assert len(contributions_b) == 1
    assert contributions_b[0].content == "Contribution to scope B."


# ---------------------------------------------------------------------------
# Scenario 5 — record_judgment writes a judgment; second attempt fails
# ---------------------------------------------------------------------------


def test_record_judgment_unique_per_contribution(tmp_path: Path) -> None:
    """A judgment can be recorded once per contribution; a second raises IntegrityError."""
    db_path = str(tmp_path / "strata.db")
    with _open_store(db_path) as rs:
        c = rs.append_contribution(
            scope_id="g_ceo",
            content="Use semantic versioning for all public APIs.",
            proposed_classification="directive",
            subject="versioning",
            supersedes=None,
            contributor=_CONTRIBUTOR,
        )

        j = rs.record_judgment(
            contribution_id=c.id,
            decision="accept_as_directive",
            judged_by="scope-manager/ceo",
        )
        assert j.contribution_id == c.id
        assert j.decision == "accept_as_directive"

        # Second judgment for the same contribution must fail.
        with pytest.raises(sqlite3.IntegrityError):
            rs.record_judgment(
                contribution_id=c.id,
                decision="decline",
                judged_by="scope-manager/ceo",
            )


# ---------------------------------------------------------------------------
# Scenario 6 — record_judgment on non-existent contribution fails (FK)
# ---------------------------------------------------------------------------


def test_record_judgment_nonexistent_contribution_fails(tmp_path: Path) -> None:
    """Judging a contribution_id that does not exist must fail with IntegrityError."""
    db_path = str(tmp_path / "strata.db")
    with _open_store(db_path) as rs, pytest.raises(sqlite3.IntegrityError):
        rs.record_judgment(
            contribution_id="c_does_not_exist",
            decision="decline",
            judged_by="scope-manager/ceo",
        )


# ---------------------------------------------------------------------------
# Scenario 7 — supersedes FK validated
# ---------------------------------------------------------------------------


def test_supersedes_fk_validated(tmp_path: Path) -> None:
    """A contribution whose supersedes field references a non-existent contribution
    must fail with IntegrityError (FK constraint)."""
    db_path = str(tmp_path / "strata.db")
    with _open_store(db_path) as rs:
        # Valid supersession.
        original = rs.append_contribution(
            scope_id="g_ceo",
            content="Original directive.",
            proposed_classification="directive",
            subject="topic",
            supersedes=None,
            contributor=_CONTRIBUTOR,
        )
        superseding = rs.append_contribution(
            scope_id="g_ceo",
            content="Updated directive.",
            proposed_classification="directive",
            subject="topic",
            supersedes=original.id,
            contributor=_CONTRIBUTOR,
        )
        assert superseding.supersedes == original.id

        # Bogus supersedes ID — must fail.
        with pytest.raises(sqlite3.IntegrityError):
            rs.append_contribution(
                scope_id="g_ceo",
                content="Invalid supersession.",
                proposed_classification="directive",
                subject="topic",
                supersedes="c_nonexistent",
                contributor=_CONTRIBUTOR,
            )


# ---------------------------------------------------------------------------
# Scenario 8 — fleet tables (strata, scopes, edges) are absent after migration
# ---------------------------------------------------------------------------


def test_fleet_tables_absent_after_migration(tmp_path: Path) -> None:
    """After applying all migrations, the strata/scopes/edges tables must not exist."""
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()

    assert "strata" not in tables, "strata table must be absent after 0002 migration"
    assert "scopes" not in tables, "scopes table must be absent after 0002 migration"
    assert "edges" not in tables, "edges table must be absent after 0002 migration"
    assert "contributions" in tables
    assert "judgments" in tables
