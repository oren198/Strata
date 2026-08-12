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
from pathlib import Path

import pytest

from strata.migrator import run_migrations
from strata.record_store import JUDGE_FAILED, ContributorRef, RecordStore

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
    migrations_dir = Path(__file__).parent.parent / "src" / "strata" / "_migrations"
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
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    conn.close()

    assert "strata" not in tables, "strata table must be absent after 0002 migration"
    assert "scopes" not in tables, "scopes table must be absent after 0002 migration"
    assert "edges" not in tables, "edges table must be absent after 0002 migration"
    assert "contributions" in tables
    assert "judgments" in tables


# ---------------------------------------------------------------------------
# Scenario 9 — the mechanical failed-judgment marker (issue #118)
# ---------------------------------------------------------------------------


def _contribute(rs: RecordStore, content: str) -> str:
    """Append a contribution to g_ceo and return its id."""
    return rs.append_contribution(
        scope_id="g_ceo",
        content=content,
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=_CONTRIBUTOR,
    ).id


def test_judge_failed_marker_round_trips_and_rejects_other_values(tmp_path: Path) -> None:
    """The marker persists on the attempt; the column admits nothing else."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        cid = _contribute(rs, "judged later, maybe")

        unmarked = rs.record_judgment_attempt(
            contribution_id=cid, error_class="TimeoutError", message="slow"
        )
        marked = rs.record_judgment_attempt(
            contribution_id=cid,
            error_class="ValueError",
            message="unparseable payload",
            outcome=JUDGE_FAILED,
        )

        # An attempt says nothing about terminality unless it is marked.
        assert unmarked.outcome is None
        assert marked.outcome == JUDGE_FAILED
        assert [a.outcome for a in rs.list_judgment_attempts(scope_id="g_ceo")] == [
            None,
            JUDGE_FAILED,
        ]

        # The marker is the only value the record's vocabulary admits here.
        with pytest.raises(sqlite3.IntegrityError):
            rs.record_judgment_attempt(
                contribution_id=cid,
                error_class="ValueError",
                message="boom",
                outcome="declined_by_judge",  # type: ignore[arg-type]
            )


def test_contribution_states_separate_judge_failed_from_pending(tmp_path: Path) -> None:
    """The three states issue #118 exists to distinguish, on one scope's record."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        judged = _contribute(rs, "accepted")
        errored = _contribute(rs, "the judge blew up")
        in_flight = _contribute(rs, "no verdict yet")

        rs.record_judgment(
            contribution_id=judged, decision="accept_as_context", judged_by="scope-manager"
        )
        rs.record_judgment_attempt(
            contribution_id=errored,
            error_class="ValueError",
            message="unparseable payload",
            outcome=JUDGE_FAILED,
        )

        states = {s.contribution_id: s for s in rs.list_contribution_states(scope_id="g_ceo")}

    assert states[judged].state == "judged"
    assert states[judged].decision == "accept_as_context"

    # The point of the issue: "attempted, judge errored" is not "pending", and
    # it carries the error so a host can say what went wrong.
    assert states[errored].state == "judge_failed"
    assert states[errored].decision is None
    assert states[errored].error_class == "ValueError"
    assert states[errored].error_message == "unparseable payload"
    assert states[errored].failed_at is not None

    assert states[in_flight].state == "pending"
    assert states[in_flight].failed_attempts == 0


def test_contribution_state_of_a_pre_marker_orphan_stays_pending(tmp_path: Path) -> None:
    """Unmarked attempts (every row written before #118) are never claimed terminal.

    The record is append-only and is not rewritten to make a read surface
    tidier: an orphan keeps reading as pending, with its attempt count intact.
    """
    with _open_store(str(tmp_path / "strata.db")) as rs:
        orphan = _contribute(rs, "stranded on 2026-07-17")
        rs.record_judgment_attempt(contribution_id=orphan, error_class="ValueError")
        rs.record_judgment_attempt(contribution_id=orphan, error_class="APIError")

        (state,) = rs.list_contribution_states(scope_id="g_ceo")

    assert state.state == "pending"
    assert state.failed_attempts == 2
    assert state.error_class is None


def test_a_successful_rejudge_outranks_an_earlier_marker(tmp_path: Path) -> None:
    """A verdict wins: a contribution re-judged after a failure is judged, not errored."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        cid = _contribute(rs, "failed once, then judged")
        rs.record_judgment_attempt(
            contribution_id=cid, error_class="ValueError", outcome=JUDGE_FAILED
        )
        rs.record_judgment(
            contribution_id=cid, decision="accept_as_directive", judged_by="scope-manager"
        )

        (state,) = rs.list_contribution_states(scope_id="g_ceo")

    assert state.state == "judged"
    assert state.decision == "accept_as_directive"
    # The failed attempt is still on the record — the marker is not erased.
    assert state.failed_attempts == 1


# ---------------------------------------------------------------------------
# Scenario 11 — the recency window read (ADR 0011 D2)
# ---------------------------------------------------------------------------


def test_recent_contributions_pair_each_state_with_its_own_notes(tmp_path: Path) -> None:
    """The window's whole reason to exist: state and judgment notes, together.

    A judged row carries its decision and the notes written when it was judged;
    a judge_failed row and a pending row carry neither — and the pending row is
    the contribution currently under judgment, appended to the record before the
    window is read.
    """
    with _open_store(str(tmp_path / "strata.db")) as rs:
        judged = _contribute(rs, "accepted a while ago")
        errored = _contribute(rs, "the judge blew up")
        under_judgment = _contribute(rs, "the contribution being judged right now")

        rs.record_judgment(
            contribution_id=judged,
            decision="accept_as_context",
            judged_by="scope-manager",
            notes="Accepted: adds a fact the context section lacked.",
        )
        rs.record_judgment_attempt(
            contribution_id=errored,
            error_class="ValueError",
            message="unparseable payload",
            outcome=JUDGE_FAILED,
        )

        rows = {
            r.contribution.id: r
            for r in rs.list_recent_contributions(scope_id="g_ceo", limit=20)
        }

    assert rows[judged].state == "judged"
    assert rows[judged].decision == "accept_as_context"
    assert rows[judged].judgment_notes == "Accepted: adds a fact the context section lacked."

    assert rows[errored].state == "judge_failed"
    assert rows[errored].decision is None
    assert rows[errored].judgment_notes is None

    assert rows[under_judgment].state == "pending"
    assert rows[under_judgment].decision is None
    assert rows[under_judgment].judgment_notes is None
    # The contribution's own bytes travel with the row — the digest's content
    # excerpt is cut from these, not from anything the judge wrote.
    assert rows[under_judgment].contribution.content == "the contribution being judged right now"


def test_recent_contributions_window_takes_the_newest_and_returns_oldest_first(
    tmp_path: Path,
) -> None:
    """Newest-first retrieval, oldest-first result — the order the digest renders in.

    Ascending order plus a limit would pin the manager to a permanently stale
    slice; that is why :meth:`list_contributions` retrieves descending too.
    """
    with _open_store(str(tmp_path / "strata.db")) as rs:
        for i in range(8):
            _contribute(rs, f"contribution {i}")

        window = rs.list_recent_contributions(scope_id="g_ceo", limit=3)

    assert [r.contribution.content for r in window] == [
        "contribution 5",
        "contribution 6",
        "contribution 7",
    ]


def test_recent_contributions_are_scoped(tmp_path: Path) -> None:
    """One scope's window never contains another scope's record."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        mine = _contribute(rs, "mine")
        rs.append_contribution(
            scope_id="g_other",
            content="not mine",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_CONTRIBUTOR,
        )

        window = rs.list_recent_contributions(scope_id="g_ceo", limit=20)

    assert [r.contribution.id for r in window] == [mine]


def test_recent_contributions_agree_with_contribution_states(tmp_path: Path) -> None:
    """The windowed read and the #118 read surface never disagree about a state."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        judged = _contribute(rs, "judged")
        orphan = _contribute(rs, "attempted, unmarked")
        rs.record_judgment(
            contribution_id=judged, decision="decline", judged_by="scope-manager", notes="no"
        )
        # Unmarked attempts predate the marker (#118) and are never claimed
        # terminal — the window must reach the same verdict as the read surface.
        rs.record_judgment_attempt(contribution_id=orphan, error_class="APIError")

        states = {s.contribution_id: s.state for s in rs.list_contribution_states(scope_id="g_ceo")}
        window = {
            r.contribution.id: r.state
            for r in rs.list_recent_contributions(scope_id="g_ceo", limit=20)
        }

    assert window == states
    assert window[orphan] == "pending"
