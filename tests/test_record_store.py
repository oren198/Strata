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
            r.contribution.id: r for r in rs.list_recent_contributions(scope_id="g_ceo", limit=20)
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


# ---------------------------------------------------------------------------
# Scenario 12 — the paged record read and the by-id lookup (issue #130)
# ---------------------------------------------------------------------------


def test_record_page_default_size_matches_the_setting() -> None:
    """The named engine default and the setting's default agree.

    Library callers with no settings in hand get :data:`RECORD_PAGE_SIZE`;
    deployments get ``Settings.record_page_size``. If the two ever drift, the
    same call means two different page sizes depending on which door it came
    through.
    """
    from strata.record_store import RECORD_PAGE_SIZE
    from strata.settings import Settings

    assert RECORD_PAGE_SIZE == 20
    assert Settings().record_page_size == RECORD_PAGE_SIZE


def test_record_page_starts_at_the_newest_contribution(tmp_path: Path) -> None:
    """An unadorned page is the newest slice — the recency bias is the point."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        for i in range(5):
            _contribute(rs, f"contribution {i}")

        page = rs.page_record(scope_id="g_ceo", limit=2)

    assert [c.content for c in page.contributions] == ["contribution 4", "contribution 3"]
    assert page.limit == 2
    assert page.total == 5


def test_record_pages_walk_the_whole_record_newest_to_oldest(tmp_path: Path) -> None:
    """Paging to exhaustion covers the record exactly once, newest first.

    The stability property pagination lives or dies on: at a page boundary an
    unstable order silently drops or repeats a contribution, and the caller
    never learns it happened.
    """
    with _open_store(str(tmp_path / "strata.db")) as rs:
        appended = [_contribute(rs, f"contribution {i}") for i in range(7)]

        walked: list[str] = []
        cursors: list[str | None] = []
        cursor: str | None = None
        while True:
            cursors.append(cursor)
            page = rs.page_record(scope_id="g_ceo", limit=3, before_id=cursor)
            walked.extend(c.id for c in page.contributions)
            cursor = page.next_before_id
            if cursor is None:
                break

    assert walked == list(reversed(appended))
    # 3 + 3 + 1: each cursor is the previous page's last (oldest) row, and the
    # last page reports None rather than leaving the caller to infer the end
    # from a short page.
    assert cursors == [None, appended[4], appended[1]]


def test_a_contribution_appended_mid_walk_never_shifts_a_page(tmp_path: Path) -> None:
    """The test that justifies a cursor over an offset (issue #130).

    Pages descend from the newest row and the record grows at that end. Under
    an OFFSET, a contribution appended mid-walk pushes every later page down by
    one and the boundary silently repeats a row. The cursor anchors on a
    contribution rather than a position, so the new row lands above the walk and
    the walk is untouched: no repeat, no drop, no shift.
    """
    with _open_store(str(tmp_path / "strata.db")) as rs:
        original = [_contribute(rs, f"contribution {i}") for i in range(6)]

        first = rs.page_record(scope_id="g_ceo", limit=2)
        # Newest-first as the record stood when the walk began.
        before_append = [c.id for c in reversed(rs.list_contributions(scope_id="g_ceo"))]
        # A concurrent contributor writes while the walk is between pages.
        appended = _contribute(rs, "appended mid-walk")
        after_append = [c.id for c in reversed(rs.list_contributions(scope_id="g_ceo"))]

        second = rs.page_record(scope_id="g_ceo", limit=2, before_id=first.next_before_id)
        third = rs.page_record(scope_id="g_ceo", limit=2, before_id=second.next_before_id)

    walked = [c.id for c in first.contributions + second.contributions + third.contributions]

    assert walked == list(reversed(original))
    assert len(walked) == len(set(walked)), "a page boundary repeated a contribution"
    assert appended not in walked, "the mid-walk append leaked into a page already passed"
    assert third.next_before_id is None

    # The same three pages walked by OFFSET instead — page 1 taken before the
    # append, pages 2 and 3 after it, which is exactly how a real walk races a
    # concurrent contributor. The failure this proves is not hypothetical.
    offset_walk = before_append[0:2] + after_append[2:4] + after_append[4:6]
    assert len(offset_walk) != len(set(offset_walk)), (
        "the offset walk was expected to repeat a row; if it no longer does, "
        "the cursor's justification needs rechecking"
    )
    # The repeat is concrete: the append shifted every row down one, so the
    # last row of page 1 comes back as the first row of page 2.
    assert offset_walk[1] == offset_walk[2] == original[4]
    # And the offset walk drops the record's oldest row while repeating another.
    assert original[0] not in offset_walk


def test_record_page_wider_than_the_record_exhausts_it(tmp_path: Path) -> None:
    """A page wider than the record is the whole record, and there is no next page."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        for i in range(3):
            _contribute(rs, f"contribution {i}")

        page = rs.page_record(scope_id="g_ceo", limit=100)

    assert len(page.contributions) == 3
    assert page.total == 3
    assert page.next_before_id is None


def test_record_page_exactly_the_size_of_the_record_reports_no_next_page(
    tmp_path: Path,
) -> None:
    """The off-by-one boundary: a full page that exhausts the record is the last one."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        for i in range(3):
            _contribute(rs, f"contribution {i}")

        page = rs.page_record(scope_id="g_ceo", limit=3)

    assert len(page.contributions) == 3
    assert page.next_before_id is None


def test_record_page_of_an_empty_record_is_empty_not_an_error(tmp_path: Path) -> None:
    """An empty record pages to an empty page — the same shape, zero rows."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        page = rs.page_record(scope_id="g_ceo")

    assert page.contributions == []
    assert page.judgments == []
    assert page.judgment_attempts == []
    assert page.contribution_states == []
    assert page.total == 0
    assert page.next_before_id is None


def test_a_cursor_on_the_oldest_row_yields_an_empty_last_page(tmp_path: Path) -> None:
    """Paging past the oldest contribution is exhaustion, not a failure."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        oldest = _contribute(rs, "the oldest")
        _contribute(rs, "the newest")

        page = rs.page_record(scope_id="g_ceo", limit=2, before_id=oldest)

    assert page.contributions == []
    assert page.total == 2
    assert page.next_before_id is None


def test_record_page_rejects_a_limit_below_one_and_a_foreign_cursor(tmp_path: Path) -> None:
    """Out-of-range paging arguments fail loudly instead of silently paging wrong.

    A cursor naming a contribution in another scope's record is the dangerous
    case: silently ignoring it would restart the walk at the newest row and
    duplicate a whole page.
    """
    with _open_store(str(tmp_path / "strata.db")) as rs:
        _contribute(rs, "mine")
        elsewhere = rs.append_contribution(
            scope_id="g_other",
            content="not mine",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_CONTRIBUTOR,
        ).id

        with pytest.raises(ValueError, match="limit must be >= 1"):
            rs.page_record(scope_id="g_ceo", limit=0)
        with pytest.raises(ValueError, match="before_id is not a contribution"):
            rs.page_record(scope_id="g_ceo", before_id="c_does_not_exist")
        with pytest.raises(ValueError, match="before_id is not a contribution"):
            rs.page_record(scope_id="g_ceo", before_id=elsewhere)


def test_record_page_carries_only_its_own_contributions_judgments(tmp_path: Path) -> None:
    """A page is internally complete: nothing on it refers to a row it does not carry."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        older = _contribute(rs, "judged, on the older page")
        newer = _contribute(rs, "judged, on the newest page")
        rs.record_judgment(
            contribution_id=older,
            decision="accept_as_context",
            judged_by="scope-manager",
            notes="older",
        )
        rs.record_judgment(
            contribution_id=newer,
            decision="decline",
            judged_by="scope-manager",
            notes="newer",
        )
        rs.record_judgment_attempt(
            contribution_id=older, error_class="APIError", outcome=JUDGE_FAILED
        )

        page = rs.page_record(scope_id="g_ceo", limit=1)

    assert [c.id for c in page.contributions] == [newer]
    assert [j.contribution_id for j in page.judgments] == [newer]
    assert page.judgment_attempts == []
    assert [s.contribution_id for s in page.contribution_states] == [newer]


def test_record_pages_are_scoped(tmp_path: Path) -> None:
    """One scope's pages never contain another scope's record."""
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

        page = rs.page_record(scope_id="g_ceo")

    assert [c.id for c in page.contributions] == [mine]
    assert page.total == 1


def test_record_page_states_agree_with_the_whole_record_states(tmp_path: Path) -> None:
    """The paged read and the #118 read surface never disagree about a state."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        judged = _contribute(rs, "judged")
        errored = _contribute(rs, "the judge blew up")
        _contribute(rs, "no verdict yet")
        rs.record_judgment(
            contribution_id=judged, decision="decline", judged_by="scope-manager", notes="no"
        )
        rs.record_judgment_attempt(
            contribution_id=errored, error_class="ValueError", outcome=JUDGE_FAILED
        )

        states = {s.contribution_id: s.state for s in rs.list_contribution_states(scope_id="g_ceo")}
        first = rs.page_record(scope_id="g_ceo", limit=2)
        rest = rs.page_record(scope_id="g_ceo", limit=2, before_id=first.next_before_id)
        paged = {s.contribution_id: s.state for s in first.contribution_states} | {
            s.contribution_id: s.state for s in rest.contribution_states
        }

    assert paged == states


def test_record_entry_carries_the_verdict_and_its_notes(tmp_path: Path) -> None:
    """The by-id hit: one contribution, its state, and what the scope-manager said."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        cid = _contribute(rs, "the contribution I submitted")
        rs.record_judgment(
            contribution_id=cid,
            decision="accept_as_directive",
            judged_by="scope-manager",
            notes="Accepted: binds the scope's descendants.",
        )

        entry = rs.get_record_entry(cid)

    assert entry is not None
    assert entry.contribution.content == "the contribution I submitted"
    assert entry.state.state == "judged"
    assert entry.state.decision == "accept_as_directive"
    assert entry.judgment is not None
    assert entry.judgment.notes == "Accepted: binds the scope's descendants."
    assert entry.judgment_attempts == []


def test_record_entry_of_a_failed_judgment_has_no_verdict(tmp_path: Path) -> None:
    """judge_failed and pending both read as "no verdict" — with the failure legible."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        errored = _contribute(rs, "the judge blew up")
        pending = _contribute(rs, "no verdict yet")
        rs.record_judgment_attempt(
            contribution_id=errored,
            error_class="APIError",
            message="upstream timeout",
            outcome=JUDGE_FAILED,
        )

        failed_entry = rs.get_record_entry(errored)
        pending_entry = rs.get_record_entry(pending)

    assert failed_entry is not None
    assert failed_entry.state.state == "judge_failed"
    assert failed_entry.judgment is None
    assert failed_entry.state.error_class == "APIError"
    assert failed_entry.state.error_message == "upstream timeout"
    assert [a.error_class for a in failed_entry.judgment_attempts] == ["APIError"]

    assert pending_entry is not None
    assert pending_entry.state.state == "pending"
    assert pending_entry.judgment is None
    assert pending_entry.judgment_attempts == []


def test_record_entry_of_an_unknown_id_is_none(tmp_path: Path) -> None:
    """The by-id miss: a clean None, matching get_contribution and get_judgment."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        _contribute(rs, "a real contribution")

        assert rs.get_record_entry("c_does_not_exist") is None


# ---------------------------------------------------------------------------
# Scenario — the latest accepted contribution (bounded freshness query)
# ---------------------------------------------------------------------------


def test_latest_accepted_contribution_skips_declines_and_unjudged(tmp_path: Path) -> None:
    """Only a verdict accepting the contribution counts; a newer decline does not."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        accepted = _contribute(rs, "the last thing this scope agreed")
        rs.record_judgment(
            contribution_id=accepted, decision="accept_as_context", judged_by="scope-manager"
        )
        declined = _contribute(rs, "rejected proposal")
        rs.record_judgment(contribution_id=declined, decision="decline", judged_by="scope-manager")
        errored = _contribute(rs, "the judge blew up")
        rs.record_judgment_attempt(
            contribution_id=errored, error_class="APIError", outcome=JUDGE_FAILED
        )
        _contribute(rs, "no verdict yet")

        latest = rs.get_latest_accepted_contribution(scope_id="g_ceo")

    assert latest is not None
    assert latest.id == accepted


def test_latest_accepted_contribution_is_none_without_one(tmp_path: Path) -> None:
    """A scope with no accepted contribution — and a foreign scope's — answers None."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        declined = _contribute(rs, "rejected proposal")
        rs.record_judgment(contribution_id=declined, decision="decline", judged_by="scope-manager")

        assert rs.get_latest_accepted_contribution(scope_id="g_ceo") is None
        assert rs.get_latest_accepted_contribution(scope_id="g_empty") is None


def test_latest_accepted_contribution_breaks_a_same_second_tie_by_rowid(tmp_path: Path) -> None:
    """Same-second acceptances resolve to the row appended last, per the record's order."""
    db_path = str(tmp_path / "strata.db")
    with _open_store(db_path) as rs:
        ids = []
        for content in ("first this second", "second this second"):
            cid = _contribute(rs, content)
            rs.record_judgment(
                contribution_id=cid, decision="accept_as_directive", judged_by="scope-manager"
            )
            ids.append(cid)
        # created_at defaults to datetime('now'), so a tie has to be written.
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE contributions SET created_at = '2026-05-04 10:00:00'")
        conn.commit()
        conn.close()

        latest = rs.get_latest_accepted_contribution(scope_id="g_ceo")

    assert latest is not None
    assert latest.id == ids[-1]


# ---------------------------------------------------------------------------
# Publication judgment attempts + derived act state — the publication-side
# mirror of judgment_attempts / ContributionState (fix: record attempts and
# mark terminal judge failure for publish/withdraw acts, not just
# contributions).
# ---------------------------------------------------------------------------


def _publish(rs: RecordStore, scope_id: str = "g_ceo", *, trigger: str | None = None) -> str:
    """Append a publish act (or, with *trigger*, a mechanically-triggered withdraw act
    removing that same publish act — the withdraw FK needs a real target).
    """
    published = rs.append_publication_act(
        scope_id=scope_id,
        act="publish",
        kind="context",
        content="outward wording",
        subject=None,
        anchors=["subject:x"],
        withdraws=None,
        trigger=None,
        proposer=_CONTRIBUTOR,
    ).id
    if trigger is None:
        return published
    return rs.append_publication_act(
        scope_id=scope_id,
        act="withdraw",
        kind=None,
        content=None,
        subject=None,
        anchors=None,
        withdraws=published,
        trigger=trigger,
        proposer=_CONTRIBUTOR,
    ).id


def test_publication_judge_failed_marker_round_trips_and_rejects_other_values(
    tmp_path: Path,
) -> None:
    """The marker persists on a publication attempt; the column admits nothing else."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        act_id = _publish(rs)

        unmarked = rs.record_publication_judgment_attempt(
            act_id=act_id, error_class="TimeoutError", message="slow"
        )
        marked = rs.record_publication_judgment_attempt(
            act_id=act_id,
            error_class="ValueError",
            message="unparseable payload",
            outcome=JUDGE_FAILED,
        )

        assert unmarked.outcome is None
        assert marked.outcome == JUDGE_FAILED
        assert [a.outcome for a in rs.list_publication_judgment_attempts(scope_id="g_ceo")] == [
            None,
            JUDGE_FAILED,
        ]

        with pytest.raises(sqlite3.IntegrityError):
            rs.record_publication_judgment_attempt(
                act_id=act_id,
                error_class="ValueError",
                message="boom",
                outcome="declined_by_judge",  # type: ignore[arg-type]
            )


def test_publication_judgment_attempt_fk_rejects_unknown_act(tmp_path: Path) -> None:
    with _open_store(str(tmp_path / "strata.db")) as rs, pytest.raises(sqlite3.IntegrityError):
        rs.record_publication_judgment_attempt(act_id="pub_doesnotexist", error_class="ValueError")


def test_publication_act_states_separate_judge_failed_from_pending(tmp_path: Path) -> None:
    """The three states — judged, judge_failed, pending — on a scope's publication acts."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        judged = _publish(rs)
        errored = _publish(rs)
        in_flight = _publish(rs)

        rs.record_publication_judgment(act_id=judged, decision="accept", judged_by="scope-manager")
        rs.record_publication_judgment_attempt(
            act_id=errored,
            error_class="ValueError",
            message="unparseable payload",
            outcome=JUDGE_FAILED,
        )

        states = {s.act_id: s for s in rs.list_publication_act_states(scope_id="g_ceo")}

    assert states[judged].state == "judged"
    assert states[judged].decision == "accept"

    assert states[errored].state == "judge_failed"
    assert states[errored].decision is None
    assert states[errored].error_class == "ValueError"
    assert states[errored].error_message == "unparseable payload"
    assert states[errored].failed_at is not None

    assert states[in_flight].state == "pending"
    assert states[in_flight].failed_attempts == 0


def test_publication_act_state_of_a_stranded_act_stays_pending_with_its_count(
    tmp_path: Path,
) -> None:
    """An unmarked attempt (or one predating the marker) is never claimed terminal."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        act_id = _publish(rs)
        rs.record_publication_judgment_attempt(act_id=act_id, error_class="ValueError")
        rs.record_publication_judgment_attempt(act_id=act_id, error_class="APIError")

        (state,) = rs.list_publication_act_states(scope_id="g_ceo")

    assert state.state == "pending"
    assert state.failed_attempts == 2
    assert state.error_class is None


def test_a_successful_publication_judgment_outranks_an_earlier_marker(tmp_path: Path) -> None:
    """A verdict wins: an act judged after a failure reads as judged, not errored."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        act_id = _publish(rs)
        rs.record_publication_judgment_attempt(
            act_id=act_id, error_class="ValueError", outcome=JUDGE_FAILED
        )
        rs.record_publication_judgment(act_id=act_id, decision="accept", judged_by="scope-manager")

        (state,) = rs.list_publication_act_states(scope_id="g_ceo")

    assert state.state == "judged"
    assert state.decision == "accept"
    assert state.failed_attempts == 1


def test_mechanically_propagated_withdrawal_state_is_never_pending(tmp_path: Path) -> None:
    """A trigger-carrying withdraw act gets no judgment row BY DESIGN (ADR 0007 D3) —
    it must read as its own distinct state, not as indistinguishable from an act
    still awaiting judgment (the exact ambiguity this fix exists to kill).
    """
    with _open_store(str(tmp_path / "strata.db")) as rs:
        mechanical_id = _publish(rs, trigger="c_abc123")

        states = {s.act_id: s for s in rs.list_publication_act_states(scope_id="g_ceo")}

    state = states[mechanical_id]
    assert state.state == "mechanical"
    assert state.decision is None
    assert state.failed_attempts == 0


# ---------------------------------------------------------------------------
# Scenario N — page_declines (UI-only proof surface)
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path):
    """A fresh, migrated RecordStore backed by its own SQLite file."""
    with _open_store(str(tmp_path / "strata.db")) as rs:
        yield rs


class TestPageDeclines:
    """RecordStore.page_declines — the UI-only declined-with-reasons read."""

    def _decline(self, store, scope_id, content, reason):
        c = store.append_contribution(
            scope_id=scope_id,
            content=content,
            proposed_classification="directive",
            subject=None,
            supersedes=None,
            contributor=ContributorRef(
                scope_id=scope_id,
                skill="architect",
                session_id="sess_1",
                ts="2026-08-20T10:00:00+00:00",
            ),
        )
        store.record_judgment(
            contribution_id=c.id,
            decision="decline",
            judged_by="scope-manager",
            notes=reason,
        )
        return c

    def test_returns_only_declines_newest_first_with_reasons(self, store):
        self._decline(store, "g_a", "first bad idea", "Contradicts the gRPC directive.")
        self._decline(store, "g_a", "second bad idea", "Duplicates an existing directive.")
        accepted = store.append_contribution(
            scope_id="g_a",
            content="good idea",
            proposed_classification="directive",
            subject=None,
            supersedes=None,
            contributor=ContributorRef(
                scope_id="g_a", skill="architect", session_id="s", ts="2026-08-20T10:00:00+00:00"
            ),
        )
        store.record_judgment(
            contribution_id=accepted.id,
            decision="accept_as_directive",
            judged_by="scope-manager",
            notes="Fine.",
        )

        page = store.page_declines(scope_id="g_a")

        assert [e.contribution.content for e in page.declines] == [
            "second bad idea",
            "first bad idea",
        ]
        assert page.declines[0].judgment.notes == "Duplicates an existing directive."
        assert page.total == 2
        assert page.next_before_id is None

    def test_other_scopes_declines_are_not_returned(self, store):
        self._decline(store, "g_a", "mine", "no")
        self._decline(store, "g_b", "theirs", "no")
        page = store.page_declines(scope_id="g_a")
        assert len(page.declines) == 1
        assert page.declines[0].contribution.scope_id == "g_a"

    def test_pages_by_cursor_until_exhausted(self, store):
        for i in range(5):
            self._decline(store, "g_a", f"idea {i}", f"reason {i}")
        first = store.page_declines(scope_id="g_a", limit=2)
        assert len(first.declines) == 2
        assert first.next_before_id == first.declines[-1].contribution.id
        second = store.page_declines(scope_id="g_a", limit=2, before_id=first.next_before_id)
        assert len(second.declines) == 2
        third = store.page_declines(scope_id="g_a", limit=2, before_id=second.next_before_id)
        assert len(third.declines) == 1
        assert third.next_before_id is None

    def test_limit_below_one_raises(self, store):
        with pytest.raises(ValueError):
            store.page_declines(scope_id="g_a", limit=0)

    def test_foreign_cursor_raises(self, store):
        self._decline(store, "g_a", "mine", "no")
        with pytest.raises(ValueError):
            store.page_declines(scope_id="g_a", before_id="c_doesnotexist")
