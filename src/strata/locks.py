"""Process-wide per-scope serialization primitives (issue #38, ADR 0008, ADR 0011 D3).

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

ADR 0011 D3 adds two more per-scope primitives beside that lock, so several
contributions arriving at one scope can be judged in ONE call instead of N:

- :func:`scope_append_lock` — the short hold that makes record-append order
  the arrival order. Deliberately NOT :func:`scope_lock`: the drain holds
  ``scope_lock`` across a judgment (an LLM call), and appends that blocked on
  it could never queue up behind an in-flight judgment — there would be
  nothing to coalesce.
- :func:`scope_queue` — the per-scope work queue with a single drain. A
  caller that finds no drain running becomes the drain and judges everything
  queued up to the batch cap; the others park on the queue's condition and
  read their own verdict when the drain publishes results.

Lock ordering, so the three primitives can never deadlock: the append lock
and the summary lock are never held at the same time, and the queue's own
condition is only ever taken *inside* one of them or on its own — never the
other way round. Concretely, the contribute path takes the append lock, then
the queue condition (enqueue), releases both, and only then takes
:func:`scope_lock` for the judgment; the drain publishes results *after*
releasing the summary lock.

Issue #19 / ADR 0012 adds a cross-process flock *inside* both
:func:`scope_lock` and :func:`scope_append_lock`: embedded mode runs one
``strata-mcp`` process per Claude Code session (plus, when it is running, the
Console backend as another process), so the ``threading.Lock`` above only
ever serialises callers inside ONE of those processes — two processes can
still both read a scope's stale summary and race its write. The flock closes
that gap without a daemon (there may be no backend running at all): each
scope gets a lock FILE under ``<db_dir>/.locks/``, one per primitive (mirrors
the in-process split — see :func:`scope_append_lock`), taken with
``fcntl.flock(..., LOCK_EX)`` after the threading lock, so ordering stays
"threading lock, then flock" both to acquire and, in reverse, to release. See
:func:`configure_lock_dir` and the ``fcntl`` import guard for the Windows
degrade.

Vocabulary follows CONTEXT.md verbatim: scope, record, scope summary.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows has no fcntl
    # Windows keeps the pre-#19 behaviour: :func:`scope_lock` and
    # :func:`scope_append_lock` serialise only within one process (the
    # ``threading.Lock`` each already wraps), so two ``strata-mcp`` processes
    # — or a process and the Console backend — on Windows can still race the
    # read-summary -> judge -> write-summary sequence the same way the
    # pre-issue-#38 code did. Deliberately NOT emulated with
    # ``msvcrt.locking`` — that locks byte ranges of an open file and cannot
    # express "wait for the other process", so imitating an advisory lock
    # with it means a spin-and-retry loop, and a wrong lock is worse than a
    # documented absence of one. See README § "Windows: cross-process scope
    # locking is not available" and the identical call in
    # ``strata.session_state``.
    fcntl = None  # type: ignore[assignment]

# The directory holding one lock FILE per scope per primitive, or ``None``
# before :func:`configure_lock_dir` runs. Every process must be configured
# with the same path (derived from the configured DB path) for the flock to
# do anything — see that function's docstring for who calls it and when.
_lock_dir: Path | None = None

# Lock-file names are built from scope ids and, on the operator paths, from
# the ``"operator:<scope_id>"`` key :func:`strata.operator._operator_layer_lock_key`
# builds — neither is guaranteed to be filesystem-safe, so anything outside
# this allowlist is replaced before it becomes part of a path.
_UNSAFE_FOR_FILENAME = re.compile(r"[^A-Za-z0-9_.:-]")


def _lock_file_path(scope_id: str, suffix: str) -> Path:
    """Return ``<lock_dir>/<sanitised scope_id>.<suffix>.lock`` (no I/O)."""
    assert _lock_dir is not None  # noqa: S101 — only called when configured
    safe = _UNSAFE_FOR_FILENAME.sub("_", scope_id)
    return _lock_dir / f"{safe}.{suffix}.lock"


def configure_lock_dir(path: Path) -> None:
    """Set the directory :func:`scope_lock` / :func:`scope_append_lock` flock
    their lock files in (issue #19, ADR 0012).

    Called once per process, at store init — the MCP server's
    ``_init_stores`` and the Console backend's ``create_app`` lifespan both
    call this with ``<db_dir>/.locks``, so every process that can touch a
    given project's DB agrees on the same lock directory no matter which
    entry point started it. Before this runs (and always on Windows — see
    the ``fcntl`` import guard above) :func:`scope_lock` and
    :func:`scope_append_lock` still work exactly as before issue #19: they
    return usable context managers that serialise within the current process
    only, no flock attempted.
    """
    global _lock_dir
    _lock_dir = Path(path)


class _ScopeFileLock:
    """A per-scope :class:`threading.Lock` plus an optional cross-process
    flock (issue #19, ADR 0012).

    Registered once per ``(scope_id, suffix)`` — see :func:`scope_lock` /
    :func:`scope_append_lock` — so every caller in this process shares the
    same instance, exactly as the bare ``threading.Lock`` it replaces did.
    ``acquire`` / ``release`` / ``locked`` delegate straight to the wrapped
    threading lock (unchanged in-process behaviour, and what existing
    single-process tests call directly); the flock is only attempted by the
    context-manager protocol (``with lock:``), which is how every real call
    site — the contribute choke point, the operator corrections — takes
    these locks.

    Acquisition order is threading lock, then flock; release is the exact
    reverse. The threading lock must stay outermost: an flock taken on a
    second file descriptor in the SAME process would still block that
    thread, so nesting it inside the threading lock is what keeps the
    combination reentrant-safe within one process (the threading lock is
    already what serialises same-process callers; the flock only ever needs
    to keep out an fd opened by another process).
    """

    def __init__(self, scope_id: str, suffix: str) -> None:
        self._scope_id = scope_id
        self._suffix = suffix
        self._lock = threading.Lock()
        self._fh: Any = None

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        return self._lock.acquire(*args, **kwargs)

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> _ScopeFileLock:
        self._lock.acquire()
        if fcntl is not None and _lock_dir is not None:
            try:
                path = _lock_file_path(self._scope_id, self._suffix)
                path.parent.mkdir(parents=True, exist_ok=True)
                # "a" creates the lock file without truncating an existing
                # one, so two processes racing to create it both end up
                # holding the same inode — same idiom as
                # ``session_state.SessionStateStore._locked``.
                handle = path.open("a", encoding="utf-8")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except BaseException:
                self._lock.release()
                raise
            self._fh = handle
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        self._lock.release()


# scope_id -> Lock, guarded by one registry lock. Module-level so every code
# path in the process — the contribute choke point and the operator
# correction primitives alike — shares exactly one lock per scope_id.
_scope_locks: dict[str, _ScopeFileLock] = {}
_scope_locks_guard = threading.Lock()

# scope_id -> the short record-append lock (ADR 0011 D3), same registry
# pattern and the same single-process scope.
_append_locks: dict[str, _ScopeFileLock] = {}
_append_locks_guard = threading.Lock()

# scope_id -> the judgment work queue (ADR 0011 D3).
_scope_queues: dict[str, ScopeWorkQueue] = {}
_scope_queues_guard = threading.Lock()

#: How many queued contributions one judgment call may carry (ADR 0011 D3).
#: The engine default behind :attr:`strata.settings.Settings.judgment_batch_cap`
#: — a cap keeps the prompt bounded and keeps a failed call from stranding
#: more than a cap's worth of contributions, each of which still gets its own
#: judgment-attempt row.
BATCH_CAP = 5

#: How long a parked caller waits for its own verdict before giving up, in
#: seconds. Generous by design: a waiter may sit behind an in-flight judgment
#: AND the judgment of the batch it belongs to, each an LLM call with its own
#: corrective re-asks. It exists so a wedged drain fails loudly — with the
#: contribution still recorded and re-judgeable — instead of hanging a caller
#: forever.
QUEUE_WAIT_TIMEOUT_S = 300.0


def scope_lock(scope_id: str) -> _ScopeFileLock:
    """Return the lock serialising SUMMARY writes to *scope_id*, in-process
    AND across processes (issue #38, issue #19, ADR 0012).

    Serialises the read-summary -> judge/correct -> record-write ->
    summary-write sequence for BOTH the contribute path
    (:func:`strata.app.run_contribution` / :func:`strata.app.rejudge_contribution`)
    and the operator correction primitives
    (:func:`strata.operator.operator_supersede` / :func:`strata.operator.operator_retire`,
    ADR 0008 D4) — so a scope's summary is always explainable by its record,
    regardless of which of the two write paths, or which process, touched it
    most recently. Used as a context manager (``with scope_lock(scope_id):``)
    this also takes a per-scope flock once :func:`configure_lock_dir` has
    run (a no-op flock otherwise, and always on Windows — see the ``fcntl``
    import guard).

    Since ADR 0011 D3 the record append no longer runs under this lock — it
    runs under :func:`scope_append_lock`, so contributions can queue while a
    judgment is in flight. Everything that WRITES the summary still holds
    this one.
    """
    with _scope_locks_guard:
        lock = _scope_locks.get(scope_id)
        if lock is None:
            lock = _ScopeFileLock(scope_id, "summary")
            _scope_locks[scope_id] = lock
        return lock


def scope_append_lock(scope_id: str) -> _ScopeFileLock:
    """Return the lock serialising RECORD APPENDS to *scope_id*, in-process
    AND across processes (ADR 0011 D3, issue #19, ADR 0012).

    Appending the contribution to the record and enqueueing it for judgment
    happen under this short hold, so the queue's order is the record's order
    is arrival order. Held for two fast local operations only — never across
    a judgment, which is what lets a contribution arriving mid-judgment join
    the next batch instead of blocking behind the LLM call. Deliberately a
    SEPARATE lock file from :func:`scope_lock`'s, not just a separate
    ``threading.Lock``: one shared file would make every other process's
    appends stall for the full judgment-call duration, which is exactly the
    coalescing ADR 0011 D3 exists to avoid.
    """
    with _append_locks_guard:
        lock = _append_locks.get(scope_id)
        if lock is None:
            lock = _ScopeFileLock(scope_id, "append")
            _append_locks[scope_id] = lock
        return lock


@dataclass
class QueueTicket:
    """One queued contribution and the slot its verdict will be published to.

    ``payload`` is the caller's work item (the appended
    :class:`~strata.record_store.Contribution`); ``result`` is whatever the
    drain publishes for it — an outcome or the caller's own error. ``settled``
    is the only completion signal: the queue's condition is notified when it
    flips, so nothing polls.
    """

    key: str
    payload: Any
    settled: bool = False
    result: Any = None


@dataclass
class ScopeWorkQueue:
    """The per-scope judgment work queue with a single drain (ADR 0011 D3).

    Replaces what a bare :class:`threading.Lock` could not do: enumerate and
    drain the waiters. A caller enqueues its contribution and then either
    becomes the drain worker — taking everything queued, up to the batch cap,
    into ONE judgment — or parks until the batch that includes it publishes
    its verdict.

    Every method that touches the queue's state does so under ``_condition``;
    callers never hold it across a judgment.
    """

    scope_id: str
    _condition: threading.Condition = field(default_factory=threading.Condition)
    _pending: list[QueueTicket] = field(default_factory=list)
    _draining: bool = False

    # -- enqueue / take ------------------------------------------------

    def enqueue(self, key: str, payload: Any) -> QueueTicket:  # noqa: ANN401 — opaque payload
        """Append a ticket for *payload* and return it.

        Called under :func:`scope_append_lock` together with the record
        append, so queue order is arrival order.
        """
        ticket = QueueTicket(key=key, payload=payload)
        with self._condition:
            self._pending.append(ticket)
        return ticket

    def take_batch(self, cap: int) -> list[QueueTicket]:
        """Remove and return the oldest *cap* queued tickets, in arrival order.

        Only the drain worker calls this. An empty list means another drain
        already took this caller's ticket — the caller then waits for it
        rather than judging anything.
        """
        with self._condition:
            batch = self._pending[:cap]
            del self._pending[: len(batch)]
            return batch

    def abandon(self, ticket: QueueTicket) -> None:
        """Drop *ticket* from the queue if it is still waiting to be taken.

        The give-up path: a caller whose wait expired must not leave work
        behind for a later drain to judge on its behalf, since nobody would
        be there to receive the verdict.
        """
        with self._condition:
            if ticket in self._pending:
                self._pending.remove(ticket)

    # -- drain handoff -------------------------------------------------

    def await_turn(self, ticket: QueueTicket, *, timeout: float) -> str:
        """Block until *ticket* has a verdict or this caller may drain.

        Returns ``"settled"`` when the batch that included *ticket* published
        its result, or ``"drain"`` when this caller has taken the drain role
        (which it MUST release with :meth:`release_drain`).

        Raises:
            TimeoutError: *timeout* seconds passed with neither — a wedged
                drain, surfaced loudly rather than waited on forever.
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if ticket.settled:
                    return "settled"
                if not self._draining:
                    self._draining = True
                    return "drain"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out after {timeout:.0f}s waiting for the judgment queue "
                        f"of scope {self.scope_id!r} to reach this contribution."
                    )
                self._condition.wait(remaining)

    def release_drain(self) -> None:
        """Give up the drain role and wake the parked callers.

        Whoever wakes first takes the role and judges whatever is still
        queued — including anything that arrived while this drain ran.
        """
        with self._condition:
            self._draining = False
            self._condition.notify_all()

    def settle_batch(self, batch: list[QueueTicket], results: dict[str, Any]) -> None:
        """Publish *results* onto the tickets of *batch* and wake the waiters.

        Every ticket a drain took must be settled — with its outcome or with
        its own error — before the drain role is released, so a taken ticket
        can never be left waiting on a batch nobody will judge again.
        """
        with self._condition:
            for ticket in batch:
                ticket.result = results.get(ticket.key)
                ticket.settled = True
            self._condition.notify_all()

    # -- introspection (tests / diagnostics) ---------------------------

    def pending_count(self) -> int:
        """Return how many tickets are queued and not yet taken by a drain."""
        with self._condition:
            return len(self._pending)


def scope_queue(scope_id: str) -> ScopeWorkQueue:
    """Return the process-wide judgment work queue for *scope_id* (ADR 0011 D3)."""
    with _scope_queues_guard:
        queue = _scope_queues.get(scope_id)
        if queue is None:
            queue = ScopeWorkQueue(scope_id=scope_id)
            _scope_queues[scope_id] = queue
        return queue
