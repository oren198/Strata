"""Tests for the session-state substrate and the staleness metric (issue #110).

Covers the library layer directly (no MCP server): the atomic per-session state
file, the counter mutations, the cross-process write lock (issue #119), and the
mechanical per-scope staleness metric derived from a constructed record +
receipt fixture.
"""

from __future__ import annotations

import multiprocessing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from strata.migrator import run_migrations
from strata.record_store import ContributorRef, RecordStore
from strata.session_state import (
    DEFAULT_STALENESS_WINDOW_DAYS,
    SessionStateStore,
    compute_fleet_staleness,
    compute_scope_staleness,
    sessions_dir_for,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _contributor() -> ContributorRef:
    return ContributorRef(
        scope_id="g_backend",
        skill="strata-developer",
        session_id="sess_x",
        ts="2026-05-30T00:00:00+00:00",
    )


def _record_store(tmp_path: Path) -> RecordStore:
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    return RecordStore(db_path)


def _accept(rs: RecordStore, scope_id: str, content: str) -> str:
    """Append an accepted contribution to *scope_id*; return its created_at."""
    c = rs.append_contribution(
        scope_id=scope_id,
        content=content,
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=_contributor(),
    )
    rs.record_judgment(
        contribution_id=c.id, decision="accept_as_context", judged_by="scope-manager"
    )
    return c.created_at


def _parse(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# sessions_dir_for
# ---------------------------------------------------------------------------


def test_sessions_dir_is_sibling_of_summaries() -> None:
    """The sessions dir lands beside the summaries dir under the runtime area."""
    assert sessions_dir_for("/proj/.strata/summaries") == Path("/proj/.strata/sessions")
    assert sessions_dir_for("./summaries") == Path("sessions")


# ---------------------------------------------------------------------------
# SessionStateStore — counters + atomic write
# ---------------------------------------------------------------------------


def test_record_read_increments_and_persists(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path / "sessions")
    store.record_read("s1", "g_arch")
    store.record_read("s1", "g_arch")
    store.record_read("s1", "g_backend")

    state = store.read("s1")
    assert state is not None
    assert state.reads == 3
    assert state.reads_by_scope["g_arch"].count == 2
    assert state.reads_by_scope["g_backend"].count == 1
    assert state.updated_at != ""


def test_record_contribution_and_decline(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path / "sessions")
    store.record_contribution("s1")
    store.record_decline("s1")
    store.record_decline("s1")

    state = store.read("s1")
    assert state is not None
    assert state.contributions == 1
    assert state.declines == 2
    assert state.reads == 0


def test_write_is_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    """After a write only the final JSON file (+ its lock file) exists; no .tmp."""
    sessions = tmp_path / "sessions"
    store = SessionStateStore(sessions)
    store.record_read("s1", "g_arch")

    files = sorted(p.name for p in sessions.iterdir())
    # The advisory lock file (issue #119) is expected alongside the state file;
    # the .tmp sibling of the atomic write is not.
    assert not any(name.endswith(".tmp") for name in files)
    assert "s1.json" in files
    # The file is valid JSON that round-trips through the model.
    assert store.read("s1") is not None


def test_lock_file_is_excluded_from_the_state_scan(tmp_path: Path) -> None:
    """The lock file must not be mistaken for a session state file."""
    store = SessionStateStore(tmp_path / "sessions")
    store.record_read("s1", "g_arch")

    assert store.lock_path_for("s1").name == "s1.json.lock"
    assert [s.session_id for s in store.all_states()] == ["s1"]


def test_read_missing_or_corrupt_returns_none(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path / "sessions")
    assert store.read("nope") is None

    corrupt = store.path_for("bad")
    corrupt.write_text("{not json", encoding="utf-8")
    assert store.read("bad") is None
    # A corrupt file is also skipped by the bulk scan rather than raising.
    assert store.all_states() == []


# ---------------------------------------------------------------------------
# Cross-process write lock (issue #119)
# ---------------------------------------------------------------------------

# Sized so a lost update is near-certain without the lock: each iteration is two
# full read-modify-write cycles on the same file, so four processes spend almost
# all of their time inside the window the lock closes.
_HAMMER_PROCESSES = 4
_HAMMER_ITERATIONS = 60


def _hammer_session_state(sessions_dir: str, session_id: str, iterations: int) -> None:
    """Increment ``reads`` and ``declines`` *iterations* times from this process.

    Module-level (not a closure) so it survives being handed to a child process,
    and it builds its own store: each writer is a separate process with its own
    interpreter state, exactly like the MCP server and the detached evaluator.
    """
    store = SessionStateStore(sessions_dir)
    for _ in range(iterations):
        store.record_read(session_id, "g_arch")
        store.record_decline(session_id)


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="needs fork to run the writers as real processes; the lock is a no-op there anyway",
)
def test_concurrent_processes_lose_no_increments(tmp_path: Path) -> None:
    """Two writing PROCESSES end with the exact sum of their increments (issue #119).

    The regression this pins: ``SessionStateStore`` read-modify-writes the whole
    file, and since #112 the MCP server and the detached background evaluator do
    it concurrently. ``os.replace`` makes each write atomic, so the file is never
    torn — but without a lock the loser of a race writes a state it read *before*
    the winner's increment, and that increment is gone. Threads would not prove
    it: the GIL is not what is missing, cross-process serialization is.
    """
    sessions = tmp_path / "sessions"
    ctx = multiprocessing.get_context("fork")
    workers = [
        ctx.Process(
            target=_hammer_session_state,
            args=(str(sessions), "s_race", _HAMMER_ITERATIONS),
        )
        for _ in range(_HAMMER_PROCESSES)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)

    assert [w.exitcode for w in workers] == [0] * _HAMMER_PROCESSES

    expected = _HAMMER_PROCESSES * _HAMMER_ITERATIONS
    state = SessionStateStore(sessions).read("s_race")
    assert state is not None
    assert state.reads == expected
    assert state.declines == expected
    assert state.reads_by_scope["g_arch"].count == expected


# ---------------------------------------------------------------------------
# Staleness metric
# ---------------------------------------------------------------------------


def test_metric_counts_sessions_reading_after_last_contribution(tmp_path: Path) -> None:
    """N sessions that read a scope after its last accepted contribution are counted.

    Also exercises the timestamp normalization: the contribution's created_at is
    the naive ``datetime('now')`` DB format while the receipts are tz-aware ISO.
    """
    rs = _record_store(tmp_path)
    contrib_at = _parse(_accept(rs, "g_x", "v1"))

    store = SessionStateStore(tmp_path / "sessions")
    # Three sessions read AFTER the contribution; one read BEFORE it.
    store.record_read("after1", "g_x", now=contrib_at + timedelta(hours=1))
    store.record_read("after2", "g_x", now=contrib_at + timedelta(hours=2))
    store.record_read("after3", "g_x", now=contrib_at + timedelta(hours=3))
    store.record_read("before", "g_x", now=contrib_at - timedelta(hours=1))

    metric = compute_scope_staleness(
        "g_x",
        record_store=rs,
        session_store=store,
        now=contrib_at + timedelta(hours=4),
    )
    assert metric.reads_since_last_contribution == 3
    assert metric.last_accepted_contribution_at is not None
    rs.close()


def test_metric_no_accepted_contribution_counts_windowed_reads(tmp_path: Path) -> None:
    """With no accepted contribution, every read within the window counts."""
    rs = _record_store(tmp_path)
    # A DECLINED contribution must NOT count as the last accepted one.
    c = rs.append_contribution(
        scope_id="g_x",
        content="junk",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=_contributor(),
    )
    rs.record_judgment(contribution_id=c.id, decision="decline", judged_by="scope-manager")

    now = datetime.now(UTC)
    store = SessionStateStore(tmp_path / "sessions")
    store.record_read("in_window", "g_x", now=now - timedelta(days=1))
    store.record_read("out_of_window", "g_x", now=now - timedelta(days=90))

    metric = compute_scope_staleness(
        "g_x", record_store=rs, session_store=store, window_days=30, now=now
    )
    assert metric.last_accepted_contribution_at is None
    assert metric.reads_since_last_contribution == 1  # the 90-day-old read is excluded
    rs.close()


def test_metric_window_bounds_reads_even_after_contribution(tmp_path: Path) -> None:
    """A read after the last contribution but outside the window is excluded."""
    rs = _record_store(tmp_path)
    contrib_at = _parse(_accept(rs, "g_x", "v1"))

    store = SessionStateStore(tmp_path / "sessions")
    # Read is after the contribution but 100 days before 'now' — outside a 30d window.
    now = contrib_at + timedelta(days=100)
    store.record_read("stale", "g_x", now=contrib_at + timedelta(hours=1))

    metric = compute_scope_staleness(
        "g_x", record_store=rs, session_store=store, window_days=30, now=now
    )
    assert metric.reads_since_last_contribution == 0
    rs.close()


def test_compute_fleet_staleness_preserves_order_and_default_window(tmp_path: Path) -> None:
    rs = _record_store(tmp_path)
    store = SessionStateStore(tmp_path / "sessions")
    store.record_read("s1", "g_a")

    metrics = compute_fleet_staleness(["g_a", "g_b"], record_store=rs, session_store=store)
    assert [m.scope_id for m in metrics] == ["g_a", "g_b"]
    assert metrics[0].window_days == DEFAULT_STALENESS_WINDOW_DAYS
    assert metrics[0].reads_since_last_contribution == 1
    assert metrics[1].reads_since_last_contribution == 0
    rs.close()
