"""Tests for :mod:`strata.locks` — the per-scope judgment work queue (ADR 0011 D3).

The queue is what replaced a bare per-scope ``threading.Lock`` on the
contribute path: a lock cannot enumerate or drain its waiters, so N
contributions arriving together cost N judgments. These exercise the primitive
on its own — the choke point's use of it lives in
``test_contribute_choke_point.py``.

Vocabulary follows CONTEXT.md: scope, contribution, record, scope summary.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.locks import (  # noqa: E402
    BATCH_CAP,
    ScopeWorkQueue,
    scope_append_lock,
    scope_lock,
    scope_queue,
)
from strata.settings import Settings  # noqa: E402


def _queue() -> ScopeWorkQueue:
    return ScopeWorkQueue(scope_id="g_root")


# ---------------------------------------------------------------------------
# Registries — one primitive per scope, and the two locks are distinct
# ---------------------------------------------------------------------------


def test_registries_return_one_instance_per_scope() -> None:
    assert scope_queue("g_reg_a") is scope_queue("g_reg_a")
    assert scope_queue("g_reg_a") is not scope_queue("g_reg_b")
    assert scope_append_lock("g_reg_a") is scope_append_lock("g_reg_a")
    assert scope_append_lock("g_reg_a") is not scope_append_lock("g_reg_b")


def test_append_lock_is_not_the_summary_lock() -> None:
    """The append hold must not block on an in-flight judgment (ADR 0011 D3).

    If they were the same lock, a contribution arriving while a judgment ran
    could not queue — there would be nothing to coalesce.
    """
    summary_lock = scope_lock("g_split")
    assert scope_append_lock("g_split") is not summary_lock
    with summary_lock:
        assert scope_append_lock("g_split").acquire(blocking=False) is True
        scope_append_lock("g_split").release()


def test_batch_cap_default_matches_the_setting() -> None:
    """The named constant and the setting agree on the ADR's default of 5."""
    assert BATCH_CAP == 5
    assert Settings().judgment_batch_cap == BATCH_CAP


# ---------------------------------------------------------------------------
# Queue mechanics — arrival order, the cap, abandonment
# ---------------------------------------------------------------------------


def test_take_batch_returns_arrival_order_up_to_the_cap() -> None:
    queue = _queue()
    tickets = [queue.enqueue(f"c_{i}", f"payload {i}") for i in range(7)]

    first = queue.take_batch(5)
    assert [t.key for t in first] == [f"c_{i}" for i in range(5)]
    assert [t.payload for t in first] == [f"payload {i}" for i in range(5)]
    # Taken tickets leave the queue; the rest keep their order.
    assert queue.pending_count() == 2
    second = queue.take_batch(5)
    assert [t.key for t in second] == ["c_5", "c_6"]
    assert second == tickets[5:]
    assert queue.take_batch(5) == []


def test_abandon_removes_a_still_queued_ticket_only() -> None:
    """A caller that gave up must not leave work for a later drain to judge."""
    queue = _queue()
    kept = queue.enqueue("c_kept", "payload")
    given_up = queue.enqueue("c_gone", "payload")

    queue.abandon(given_up)
    assert [t.key for t in queue.take_batch(5)] == ["c_kept"]
    # Abandoning a ticket a drain already took is a no-op, not an error.
    queue.abandon(kept)


# ---------------------------------------------------------------------------
# Drain handoff
# ---------------------------------------------------------------------------


def test_first_caller_drains_and_the_second_parks_until_settled() -> None:
    """One drain at a time; the other caller reads the result the drain published."""
    queue = _queue()
    first = queue.enqueue("c_1", "one")
    second = queue.enqueue("c_2", "two")

    assert queue.await_turn(first, timeout=1.0) == "drain"

    parked: list[str] = []

    def waiter() -> None:
        parked.append(queue.await_turn(second, timeout=5.0))

    thread = threading.Thread(target=waiter)
    thread.start()

    batch = queue.take_batch(BATCH_CAP)
    queue.settle_batch(batch, {"c_1": "verdict one", "c_2": "verdict two"})
    queue.release_drain()

    thread.join(timeout=5.0)
    assert parked == ["settled"]
    assert second.settled is True
    assert second.result == "verdict two"
    assert first.result == "verdict one"


def test_released_drain_role_passes_to_a_waiter() -> None:
    """A drain that judged nothing for a waiter hands the role over, not a verdict."""
    queue = _queue()
    first = queue.enqueue("c_1", "one")
    assert queue.await_turn(first, timeout=1.0) == "drain"

    assert [t.key for t in queue.take_batch(BATCH_CAP)] == ["c_1"]
    # A ticket that arrives after this drain already took its batch — no
    # verdict is coming for it from this drain.
    late = queue.enqueue("c_late", "late")

    turns: list[str] = []
    thread = threading.Thread(target=lambda: turns.append(queue.await_turn(late, timeout=5.0)))
    thread.start()
    queue.release_drain()
    thread.join(timeout=5.0)

    assert turns == ["drain"]
    assert [t.key for t in queue.take_batch(BATCH_CAP)] == ["c_late"]


def test_await_turn_times_out_rather_than_waiting_on_a_wedged_drain() -> None:
    """A wedged drain fails loudly: the wait is bounded and says which scope."""
    queue = _queue()
    holder = queue.enqueue("c_1", "one")
    waiter = queue.enqueue("c_2", "two")
    assert queue.await_turn(holder, timeout=1.0) == "drain"  # never released

    with pytest.raises(TimeoutError) as exc_info:
        queue.await_turn(waiter, timeout=0.05)
    assert "g_root" in str(exc_info.value)
    assert waiter.settled is False
