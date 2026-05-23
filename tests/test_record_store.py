"""Tests for the Strata record store (src/strata/record_store.py).

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

    # First run — applies 0001_initial.sql.
    run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    applied_after_first = {r[0] for r in conn.execute("SELECT name FROM _migrations").fetchall()}
    conn.close()

    assert "0001_initial.sql" in applied_after_first

    # Simulate a new migration file that was added after the first run.
    migrations_dir = Path(__file__).parent.parent / "migrations"
    new_migration = tmp_path / "0002_test_only.sql"
    new_migration.write_text("-- no-op migration for test\n", encoding="utf-8")

    # Temporarily symlink / write into a tmp migrations dir to avoid polluting
    # the real migrations folder.  We monkey-patch run_migrations' discovery
    # by creating a minimal runner that uses our tmp dir.
    import importlib
    import types

    # Build an isolated runner module pointing at tmp_path as migrations dir.
    src = Path(__file__).parent.parent / "scripts" / "run_migrations.py"
    spec = importlib.util.spec_from_file_location("run_migrations_tmp", src)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert isinstance(mod, types.ModuleType)
    assert spec.loader is not None

    # Load the module (not used further; we take the simpler approach below).
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    # Write the new migration to the real migrations folder temporarily, run,
    # then remove it — safer than patching module internals.
    temp_migration = migrations_dir / "0099_temp_test.sql"
    try:
        temp_migration.write_text("-- temporary test migration\n", encoding="utf-8")
        run_migrations(db_path)  # second run

        conn = sqlite3.connect(db_path)
        applied_after_second = {
            r[0] for r in conn.execute("SELECT name FROM _migrations").fetchall()
        }
        conn.close()

        # Both migrations should now be recorded.
        assert "0001_initial.sql" in applied_after_second
        assert "0099_temp_test.sql" in applied_after_second
        # Still exactly one entry for each.
        conn2 = sqlite3.connect(db_path)
        all_names = [r[0] for r in conn2.execute("SELECT name FROM _migrations").fetchall()]
        conn2.close()
        assert all_names.count("0001_initial.sql") == 1
        assert all_names.count("0099_temp_test.sql") == 1
    finally:
        if temp_migration.exists():
            temp_migration.unlink()


# ---------------------------------------------------------------------------
# Scenario 3 — Create + list strata, scopes, edges round-trips
# ---------------------------------------------------------------------------


def test_create_list_strata_scopes_edges(tmp_path: Path) -> None:
    """Create strata, scopes, and edges; list them back and verify round-trip."""
    db_path = str(tmp_path / "strata.db")
    with _open_store(db_path) as rs:
        exec_stratum = rs.create_stratum(name="executive", ordinal=0)
        func_stratum = rs.create_stratum(name="function", ordinal=1)

        ceo_scope = rs.create_scope(name="ceo", stratum_id=exec_stratum.id)
        arch_scope = rs.create_scope(name="architect", stratum_id=func_stratum.id)

        edge = rs.add_edge(from_scope_id=ceo_scope.id, to_scope_id=arch_scope.id)

        strata = rs.list_strata()
        scopes = rs.list_scopes()
        edges = rs.list_edges()

    assert len(strata) == 2
    assert strata[0].ordinal == 0 and strata[0].name == "executive"
    assert strata[1].ordinal == 1 and strata[1].name == "function"

    scope_ids = {s.id for s in scopes}
    assert ceo_scope.id in scope_ids
    assert arch_scope.id in scope_ids

    assert len(edges) == 1
    assert edges[0].id == edge.id
    assert edges[0].from_scope_id == ceo_scope.id
    assert edges[0].to_scope_id == arch_scope.id


# ---------------------------------------------------------------------------
# Scenario 4 — ±1 stratum constraint enforced
# ---------------------------------------------------------------------------


def test_edge_plus_minus_one_stratum_constraint(tmp_path: Path) -> None:
    """Edges spanning exactly 0 or 1 stratum are allowed; 2+ is rejected."""
    db_path = str(tmp_path / "strata.db")
    with _open_store(db_path) as rs:
        s0 = rs.create_stratum(name="exec", ordinal=0)
        s1 = rs.create_stratum(name="func", ordinal=1)
        s2 = rs.create_stratum(name="team", ordinal=2)

        scope_s0 = rs.create_scope(name="ceo", stratum_id=s0.id)
        scope_s1 = rs.create_scope(name="arch", stratum_id=s1.id)
        scope_s2 = rs.create_scope(name="dev", stratum_id=s2.id)

        # Same stratum — OK (ordinal distance = 0).
        scope_s1b = rs.create_scope(name="sec", stratum_id=s1.id)
        edge_peer = rs.add_edge(from_scope_id=scope_s1.id, to_scope_id=scope_s1b.id)
        assert edge_peer.from_scope_id == scope_s1.id

        # Adjacent stratum — OK (ordinal distance = 1).
        edge_adj = rs.add_edge(from_scope_id=scope_s0.id, to_scope_id=scope_s1.id)
        assert edge_adj.from_scope_id == scope_s0.id

        # Two strata apart — MUST be rejected (ordinal distance = 2).
        with pytest.raises(ValueError, match="more than one stratum"):
            rs.add_edge(from_scope_id=scope_s0.id, to_scope_id=scope_s2.id)


# ---------------------------------------------------------------------------
# Scenario 5 — Self-loop edge rejected
# ---------------------------------------------------------------------------


def test_self_loop_edge_rejected(tmp_path: Path) -> None:
    """An edge from a scope to itself must raise ValueError."""
    db_path = str(tmp_path / "strata.db")
    with _open_store(db_path) as rs:
        s0 = rs.create_stratum(name="exec", ordinal=0)
        scope = rs.create_scope(name="ceo", stratum_id=s0.id)

        with pytest.raises(ValueError, match="Self-loop"):
            rs.add_edge(from_scope_id=scope.id, to_scope_id=scope.id)


# ---------------------------------------------------------------------------
# Scenario 6 — Append a contribution, list returns it
# ---------------------------------------------------------------------------


def test_append_and_list_contribution(tmp_path: Path) -> None:
    """Appending a contribution to a scope's record and listing returns it."""
    db_path = str(tmp_path / "strata.db")
    with _open_store(db_path) as rs:
        s0 = rs.create_stratum(name="exec", ordinal=0)
        scope = rs.create_scope(name="ceo", stratum_id=s0.id)

        c = rs.append_contribution(
            scope_id=scope.id,
            content="All new services must default to read-only mode.",
            proposed_classification="directive",
            subject="service defaults",
            supersedes=None,
            contributor=_CONTRIBUTOR,
        )

        contributions = rs.list_contributions(scope_id=scope.id)

    assert len(contributions) == 1
    assert contributions[0].id == c.id
    assert contributions[0].content == "All new services must default to read-only mode."
    assert contributions[0].proposed_classification == "directive"
    assert contributions[0].subject == "service defaults"
    assert contributions[0].contributor.skill == "code-writer"


# ---------------------------------------------------------------------------
# Scenario 7 — Contributions to two scopes are isolated per list call
# ---------------------------------------------------------------------------


def test_list_contributions_isolated_per_scope(tmp_path: Path) -> None:
    """list_contributions for scope A must not return contributions from scope B."""
    db_path = str(tmp_path / "strata.db")
    with _open_store(db_path) as rs:
        s0 = rs.create_stratum(name="exec", ordinal=0)
        scope_a = rs.create_scope(name="ceo", stratum_id=s0.id)
        scope_b = rs.create_scope(name="cto", stratum_id=s0.id)

        rs.append_contribution(
            scope_id=scope_a.id,
            content="Contribution to scope A.",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_CONTRIBUTOR,
        )
        rs.append_contribution(
            scope_id=scope_b.id,
            content="Contribution to scope B.",
            proposed_classification="directive",
            subject=None,
            supersedes=None,
            contributor=_CONTRIBUTOR,
        )

        contributions_a = rs.list_contributions(scope_id=scope_a.id)
        contributions_b = rs.list_contributions(scope_id=scope_b.id)

    assert len(contributions_a) == 1
    assert contributions_a[0].content == "Contribution to scope A."

    assert len(contributions_b) == 1
    assert contributions_b[0].content == "Contribution to scope B."


# ---------------------------------------------------------------------------
# Scenario 8 — record_judgment writes a judgment; second attempt fails
# ---------------------------------------------------------------------------


def test_record_judgment_unique_per_contribution(tmp_path: Path) -> None:
    """A judgment can be recorded once per contribution; a second raises IntegrityError."""
    db_path = str(tmp_path / "strata.db")
    with _open_store(db_path) as rs:
        s0 = rs.create_stratum(name="exec", ordinal=0)
        scope = rs.create_scope(name="ceo", stratum_id=s0.id)

        c = rs.append_contribution(
            scope_id=scope.id,
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
# Scenario 9 — record_judgment on non-existent contribution fails (FK)
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
# Scenario 10 — supersedes FK validated
# ---------------------------------------------------------------------------


def test_supersedes_fk_validated(tmp_path: Path) -> None:
    """A contribution whose supersedes field references a non-existent contribution
    must fail with IntegrityError (FK constraint)."""
    db_path = str(tmp_path / "strata.db")
    with _open_store(db_path) as rs:
        s0 = rs.create_stratum(name="exec", ordinal=0)
        scope = rs.create_scope(name="ceo", stratum_id=s0.id)

        # First verify that a valid supersession works.
        original = rs.append_contribution(
            scope_id=scope.id,
            content="Original directive.",
            proposed_classification="directive",
            subject="topic",
            supersedes=None,
            contributor=_CONTRIBUTOR,
        )
        superseding = rs.append_contribution(
            scope_id=scope.id,
            content="Updated directive.",
            proposed_classification="directive",
            subject="topic",
            supersedes=original.id,
            contributor=_CONTRIBUTOR,
        )
        assert superseding.supersedes == original.id

        # Now try with a bogus supersedes ID — must fail.
        with pytest.raises(sqlite3.IntegrityError):
            rs.append_contribution(
                scope_id=scope.id,
                content="Invalid supersession.",
                proposed_classification="directive",
                subject="topic",
                supersedes="c_nonexistent",
                contributor=_CONTRIBUTOR,
            )
