"""Tests for the session-state substrate and the staleness metric (issue #110).

Covers the library layer directly (no MCP server): the atomic per-session state
file, the counter mutations, the cross-process write lock (issue #119), and the
mechanical per-scope staleness metric derived from a constructed record +
receipt fixture.
"""

from __future__ import annotations

import multiprocessing
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from strata.migrator import run_migrations
from strata.record_store import ContributorRef, RecordStore
from strata.session_state import (
    DEFAULT_STALENESS_WINDOW_DAYS,
    SessionStateStore,
    _last_accepted_contribution_at,
    _parse_ts,
    compute_fleet_refresh_pending,
    compute_fleet_staleness,
    compute_refresh_pending,
    compute_scope_staleness,
    resolve_agent_session_id,
    sessions_dir_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable

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
# resolve_agent_session_id — shared deterministic fallback (issue #112 gap:
# the MCP server and the freshness Stop hook must land on the identical
# session id from an identical environment, with no IPC).
# ---------------------------------------------------------------------------


def test_resolve_agent_session_id_explicit_value_used_verbatim() -> None:
    """An explicit STRATA_AGENT_SESSION_ID is returned unchanged."""
    env = {"STRATA_AGENT_SESSION_ID": "my-session"}
    assert resolve_agent_session_id(env) == "my-session"


def test_resolve_agent_session_id_falls_back_when_unset(monkeypatch) -> None:
    """Unset STRATA_AGENT_SESSION_ID resolves to the deterministic
    sess_auto_<parent pid> fallback, not an empty string."""
    monkeypatch.setattr("os.getppid", lambda: 4242)
    resolved = resolve_agent_session_id({})
    assert resolved == "sess_auto_4242"


def test_resolve_agent_session_id_treats_empty_string_as_unset(monkeypatch) -> None:
    """Empty string counts as unset everywhere (Codex writes literal empty
    env values into its config) — falls through to the same fallback as an
    absent var, never an empty-string session id."""
    monkeypatch.setattr("os.getppid", lambda: 4242)
    env = {"STRATA_AGENT_SESSION_ID": ""}
    resolved = resolve_agent_session_id(env)
    assert resolved == "sess_auto_4242"
    assert resolved != ""


def test_resolve_agent_session_id_deterministic_for_same_ppid(monkeypatch) -> None:
    """Two independent calls (standing in for the MCP server process and the
    freshness Stop hook process) with the same parent pid land on the exact
    same fallback id — the pairing the whole design depends on."""
    monkeypatch.setattr("os.getppid", lambda: 9999)
    mcp_side = resolve_agent_session_id({})
    hook_side = resolve_agent_session_id({})
    assert mcp_side == hook_side == "sess_auto_9999"


def test_resolve_agent_session_id_defaults_to_os_environ(monkeypatch) -> None:
    """With no env argument, os.environ is read (the real-process default)."""
    monkeypatch.setenv("STRATA_AGENT_SESSION_ID", "from-environ")
    assert resolve_agent_session_id() == "from-environ"


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


# ---------------------------------------------------------------------------
# Last accepted contribution — bounded query vs. the whole-record scan
# ---------------------------------------------------------------------------


def _append(rs: RecordStore, scope_id: str, content: str) -> str:
    """Append an unjudged contribution to *scope_id*; return its id."""
    return rs.append_contribution(
        scope_id=scope_id,
        content=content,
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=_contributor(),
    ).id


def _set_created_at(db_path: str, contribution_id: str, created_at: str) -> None:
    """Force a contribution's record timestamp.

    ``contributions.created_at`` defaults to ``datetime('now')``, so appends
    inside one test are seconds apart at best — a fixture that needs a chosen
    order, or a deliberate same-second tie, has to write the column.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE contributions SET created_at = ? WHERE id = ?", (created_at, contribution_id)
    )
    conn.commit()
    conn.close()


def _legacy_last_accepted_contribution_at(
    scope_id: str, *, record_store: RecordStore
) -> tuple[str | None, datetime | None]:
    """The superseded whole-record scan, kept as the equivalence oracle.

    Pulls every contribution and every judgment for the scope and picks the
    newest accepted one in Python — what
    :func:`strata.session_state._last_accepted_contribution_at` did before the
    record grew a bounded query for it.
    """
    accepted = frozenset({"accept_as_directive", "accept_as_context"})
    contributions = record_store.list_contributions(scope_id=scope_id)
    judgments = {j.contribution_id: j for j in record_store.list_judgments(scope_id=scope_id)}

    latest_raw: str | None = None
    latest_parsed: datetime | None = None
    for contribution in contributions:
        judgment = judgments.get(contribution.id)
        if judgment is None or judgment.decision not in accepted:
            continue
        parsed = _parse_ts(contribution.created_at)
        if parsed is None:
            continue
        if latest_parsed is None or parsed > latest_parsed:
            latest_parsed = parsed
            latest_raw = contribution.created_at
    return latest_raw, latest_parsed


def _case_no_contributions(rs: RecordStore, db_path: str) -> str | None:
    """A scope whose record is empty."""
    return None


def _case_no_judgments(rs: RecordStore, db_path: str) -> str | None:
    """Contributions that carry no verdict at all — pending, not accepted."""
    _append(rs, "g_x", "awaiting judgment")
    _append(rs, "g_x", "also awaiting judgment")
    return None


def _case_declined_only(rs: RecordStore, db_path: str) -> str | None:
    """Every contribution declined — a decline is not an acceptance."""
    for content in ("junk", "more junk"):
        cid = _append(rs, "g_x", content)
        rs.record_judgment(contribution_id=cid, decision="decline", judged_by="scope-manager")
    return None


def _case_accepted_then_declined(rs: RecordStore, db_path: str) -> str | None:
    """The newest contribution is declined; the answer is the older accepted one."""
    accepted = _append(rs, "g_x", "accepted")
    _set_created_at(db_path, accepted, "2026-05-01 10:00:00")
    rs.record_judgment(
        contribution_id=accepted, decision="accept_as_directive", judged_by="scope-manager"
    )

    declined = _append(rs, "g_x", "declined later")
    _set_created_at(db_path, declined, "2026-05-02 10:00:00")
    rs.record_judgment(contribution_id=declined, decision="decline", judged_by="scope-manager")

    pending = _append(rs, "g_x", "unjudged, later still")
    _set_created_at(db_path, pending, "2026-05-03 10:00:00")
    return "2026-05-01 10:00:00"


def _case_several_accepted_newest_wins(rs: RecordStore, db_path: str) -> str | None:
    """Several acceptances out of insertion order — the newest one wins."""
    for content, created_at in (
        ("middle", "2026-05-02 10:00:00"),
        ("newest", "2026-05-03 10:00:00"),
        ("oldest", "2026-05-01 10:00:00"),
    ):
        cid = _append(rs, "g_x", content)
        _set_created_at(db_path, cid, created_at)
        rs.record_judgment(
            contribution_id=cid, decision="accept_as_context", judged_by="scope-manager"
        )
    return "2026-05-03 10:00:00"


def _case_same_second_tie(rs: RecordStore, db_path: str) -> str | None:
    """Two acceptances sharing one second — the rowid tie-break decides."""
    for content in ("first this second", "second this second"):
        cid = _append(rs, "g_x", content)
        _set_created_at(db_path, cid, "2026-05-04 10:00:00")
        rs.record_judgment(
            contribution_id=cid, decision="accept_as_directive", judged_by="scope-manager"
        )
    return "2026-05-04 10:00:00"


def _case_other_scope_only(rs: RecordStore, db_path: str) -> str | None:
    """An acceptance in a different scope never answers for this one."""
    cid = _append(rs, "g_other", "not this scope")
    rs.record_judgment(contribution_id=cid, decision="accept_as_context", judged_by="scope-manager")
    return None


@pytest.mark.parametrize(
    "build_case",
    [
        _case_no_contributions,
        _case_no_judgments,
        _case_declined_only,
        _case_accepted_then_declined,
        _case_several_accepted_newest_wins,
        _case_same_second_tie,
        _case_other_scope_only,
    ],
    ids=lambda fn: fn.__name__.removeprefix("_case_"),
)
def test_last_accepted_matches_the_whole_record_scan(
    tmp_path: Path, build_case: Callable[[RecordStore, str], str | None]
) -> None:
    """The bounded query answers exactly what the whole-record scan answered.

    The scan is the oracle (:func:`_legacy_last_accepted_contribution_at`); the
    expected timestamp each case names pins the answer down absolutely, so a
    matching pair of wrong implementations still fails.
    """
    db_path = str(tmp_path / "strata.db")
    rs = _record_store(tmp_path)
    expected_raw = build_case(rs, db_path)

    got = _last_accepted_contribution_at("g_x", record_store=rs)

    assert got == _legacy_last_accepted_contribution_at("g_x", record_store=rs)
    assert got[0] == expected_raw
    assert got[1] == (None if expected_raw is None else _parse(expected_raw))
    rs.close()


# ---------------------------------------------------------------------------
# ADR 0014 pin 4 — refresh-pending is NOT a judge outage. compute_refresh_pending
# / compute_fleet_refresh_pending are the one library helper every surface that
# needs the distinction (doctor, the Console) calls, rather than each
# reinventing its own change_events query.
# ---------------------------------------------------------------------------


def _manager_refresh_contribution(rs: RecordStore, scope_id: str) -> str:
    """Append a subject='manager-refresh' contribution and return its id.

    Mirrors ADR 0014 D5: the change event's notice IS this contribution — a
    row must exist before append_change_event's contribution_id FK accepts it.
    """
    c = rs.append_contribution(
        scope_id=scope_id,
        content="input changed",
        proposed_classification="context",
        subject="manager-refresh",
        supersedes=None,
        contributor=_contributor(),
    )
    return c.id


def test_refresh_pending_counts_first_unprocessed_change_events_pin4(tmp_path: Path) -> None:
    """A pin-4 vacuous-pass guard: before asserting anything is NOT an outage,
    first assert the refresh-pending count this scope shows is >= 1 (pin 10)."""
    rs = _record_store(tmp_path)
    contribution_id = _manager_refresh_contribution(rs, "g_x")
    rs.append_change_event(
        change_id="wave_1",
        contribution_id=contribution_id,
        scope_id="g_x",
        item_id="pub_1",
        kind="withdrawn",
    )

    pending = compute_refresh_pending("g_x", record_store=rs)

    assert pending.depth >= 1
    assert pending.depth == 1
    assert pending.oldest_pending_at is not None
    rs.close()


def test_refresh_pending_ignores_other_scopes(tmp_path: Path) -> None:
    """A change event queued for a different scope never inflates this scope's count."""
    rs = _record_store(tmp_path)
    contribution_id = _manager_refresh_contribution(rs, "g_other")
    rs.append_change_event(
        change_id="wave_1",
        contribution_id=contribution_id,
        scope_id="g_other",
        item_id="pub_1",
        kind="withdrawn",
    )

    pending = compute_refresh_pending("g_x", record_store=rs)

    assert pending.depth == 0
    assert pending.oldest_pending_at is None
    rs.close()


def test_refresh_pending_excludes_processed_events(tmp_path: Path) -> None:
    """A processed change event (whatever the refresh's verdict) drops out of the count."""
    rs = _record_store(tmp_path)
    contribution_id = _manager_refresh_contribution(rs, "g_x")
    event = rs.append_change_event(
        change_id="wave_1",
        contribution_id=contribution_id,
        scope_id="g_x",
        item_id="pub_1",
        kind="withdrawn",
    )
    rs.mark_change_event_processed(event.id)

    pending = compute_refresh_pending("g_x", record_store=rs)

    assert pending.depth == 0
    assert pending.oldest_pending_at is None
    rs.close()


def test_refresh_pending_oldest_pending_at_is_the_earliest_unprocessed(tmp_path: Path) -> None:
    """Two unprocessed events for one scope: oldest_pending_at names the earliest."""
    rs = _record_store(tmp_path)
    contribution_id = _manager_refresh_contribution(rs, "g_x")
    first = rs.append_change_event(
        change_id="wave_1",
        contribution_id=contribution_id,
        scope_id="g_x",
        item_id="pub_1",
        kind="withdrawn",
    )
    rs.append_change_event(
        change_id="wave_2",
        contribution_id=contribution_id,
        scope_id="g_x",
        item_id="pub_2",
        kind="published",
    )

    pending = compute_refresh_pending("g_x", record_store=rs)

    assert pending.depth == 2
    assert pending.oldest_pending_at == first.created_at
    rs.close()


def test_refresh_pending_no_events_is_honestly_empty(tmp_path: Path) -> None:
    """A scope with no change events at all: depth 0, oldest_pending_at None."""
    rs = _record_store(tmp_path)

    pending = compute_refresh_pending("g_x", record_store=rs)

    assert pending.depth == 0
    assert pending.oldest_pending_at is None
    rs.close()


def test_compute_fleet_refresh_pending_preserves_order(tmp_path: Path) -> None:
    """compute_fleet_refresh_pending maps compute_refresh_pending over scope_ids in order."""
    rs = _record_store(tmp_path)
    contribution_id = _manager_refresh_contribution(rs, "g_a")
    rs.append_change_event(
        change_id="wave_1",
        contribution_id=contribution_id,
        scope_id="g_a",
        item_id="pub_1",
        kind="withdrawn",
    )

    results = compute_fleet_refresh_pending(["g_a", "g_b"], record_store=rs)

    assert [r.scope_id for r in results] == ["g_a", "g_b"]
    assert results[0].depth == 1
    assert results[1].depth == 0
    rs.close()
