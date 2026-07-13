"""Process-wide per-scope serialization lock registry (issue #38, extracted for ADR 0008).

Originally lived inline in ``strata.app`` (the ``POST /contribute`` /
``strata_contribute`` choke point). ADR 0008 D4's operator correction
primitives (:mod:`strata.operator` — ``operator_supersede``,
``operator_retire``) must serialize under the *same* per-scope lock the
contribute path uses, so a concurrent contribution and an in-person operator
correction to the same scope can never interleave and leave a scope's
summary unexplainable by its record. ``strata.app`` also needs
``strata.operator`` (to fetch ``operator_memory_binding`` for judge inputs),
so the lock registry moved to this standalone module to avoid an import
cycle between the two.

Vocabulary follows CONTEXT.md verbatim: scope, record, scope summary.
"""

from __future__ import annotations

import threading

# scope_id -> Lock, guarded by one registry lock. Module-level so every code
# path in the process — the contribute choke point and the operator
# correction primitives alike — shares exactly one lock per scope_id.
_scope_locks: dict[str, threading.Lock] = {}
_scope_locks_guard = threading.Lock()


def scope_lock(scope_id: str) -> threading.Lock:
    """Return the process-wide lock serialising writes to *scope_id*.

    Single-process scope only (issue #38). Serialises the
    read-summary -> judge/correct -> record-write -> summary-write sequence
    within one process for BOTH the contribute path
    (:func:`strata.app.run_contribution` / :func:`strata.app.rejudge_contribution`)
    and the operator correction primitives
    (:func:`strata.operator.operator_supersede` / :func:`strata.operator.operator_retire`,
    ADR 0008 D4) — so a scope's summary is always explainable by its record,
    regardless of which of the two write paths touched it most recently.
    Cross-process serialisation is issue #19 and out of scope here.
    """
    with _scope_locks_guard:
        lock = _scope_locks.get(scope_id)
        if lock is None:
            lock = threading.Lock()
            _scope_locks[scope_id] = lock
        return lock
