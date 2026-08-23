# ADR 0012 — Cross-Process Per-Scope Lock

**Status:** Accepted (implementation done, issue #19).
**Date:** 2026-08-24
**Related:** Issue #38 / `locks.py` (the in-process per-scope lock this
extends), ADR 0008 D4 (operator correction primitives, which share the same
lock), ADR 0011 D3 (the append-lock / summary-lock split this preserves).

---

## Context

`strata.locks.scope_lock` and `scope_append_lock` are `threading.Lock`
pairs, keyed by scope id. They serialise the contribute path
(`strata.app.run_contribution` / `rejudge_contribution`) and the operator
correction primitives (`operator_supersede`, `operator_retire`) so a scope's
summary is always explainable by its record — but only within **one**
process.

In embedded mode that guarantee breaks. Every Claude Code session that talks
to Strata runs its own `strata-mcp` process; a second terminal, a second
agent, or the optional Console backend is a *different* process with its own
Python interpreter and its own uncontended `threading.Lock`. Two processes
contributing to the same scope can both read the current summary, both call
the judge, and then race their writes — the second write silently clobbers
the first accepted directive. `tests/test_interprocess_locks.py` reproduces
this with two real OS processes hammering one scope: without a cross-process
lock, judgments land in the record that the final summary never reflects.

SQLite's WAL mode plus `busy_timeout` (already configured) rules out *file
corruption* from concurrent writers. It does nothing for this problem —
the race is a lost update at the application layer (read-summary ->
judge -> write-summary), not a storage-layer conflict.

## Decision

**D1 — An `fcntl` flock, taken inside the existing locks, on a file per
scope per lock.** `scope_lock(scope_id)` and `scope_append_lock(scope_id)`
still return the same `threading.Lock`-backed object as before; used as a
context manager, each now also flocks
`<lock_dir>/<scope_id>.summary.lock` or `<lock_dir>/<scope_id>.append.lock`
respectively (`fcntl.flock(fd, LOCK_EX)`, released in the reverse order it
was acquired: threading lock outermost, flock innermost). Two files, not
one, for the same reason the in-process pair is already split (ADR 0011
D3): a shared file would make every other process's record-append stall for
the full length of a judgment call — an LLM round-trip — gutting the
coalescing that split exists for.

**D2 — The lock directory comes from the configured DB path, not a new
setting.** `strata.locks.configure_lock_dir(path)` sets it once per process;
every store-init path that already resolves storage paths from the DB path —
the MCP server's `_init_stores`, the Console backend's `create_app`
lifespan — calls it with `<db_dir>/.locks`. Any process that can open the
same `strata.db` derives the same lock directory, with no additional
configuration to keep in sync and nothing for the operator to set up.

**D3 — No daemon.** A lock coordinator process was considered and rejected:
embedded mode's whole point is that a session works with no backend running
— "several terminals on one machine, no server started" is an explicit
launch-bar scenario. A daemon would mean start-up ordering, a liveness
check, and a new failure mode ("the lock daemon isn't running") in the one
place Strata is supposed to need nothing. A file the OS already knows how to
lock has none of that: it exists the moment a process needs it and needs no
process of its own to arbitrate it.

**D4 — Windows runs unlocked, documented, not emulated.** `fcntl` does not
exist on Windows. `strata.locks` degrades exactly like
`strata.session_state`'s existing cross-process lock (issue #119): the
`threading.Lock` still serialises callers within one process; the flock is
skipped entirely rather than approximated with `msvcrt.locking`, which locks
byte ranges and cannot express "wait for the other process" — emulating an
advisory lock with it would mean a spin-and-retry loop, and a wrong lock is
worse than a documented absence of one. On Windows, two processes racing the
same scope keep the pre-#19 behaviour: a lost update is possible, at most
once per race, and the contribution itself is never lost (it is always in
the record; only the summary's reflection of it can be superseded).

**Known limit — coalescing (ADR 0011 D3) does not cross the process
boundary.** `ScopeWorkQueue` (`scope_queue`, `locks.py`) is a plain
in-memory, per-process registry: several contributions arriving at one
scope in the SAME process can be judged in one call, but a contribution
queued in process A and one queued in process B never share a batch —
there is no cross-process equivalent of the queue, only of the two locks
around it. Concretely: process A's drain takes `scope_lock` and holds it
across its judge call; any contribution process B enqueues meanwhile
serialises behind that flock, and once B acquires it, B runs its own
complete judge call rather than joining A's batch. So two processes
contributing to one scope at the same time still get full correctness (D1
above) but pay one full judgment round-trip **per process**, not the single
coalesced call same-process contributions would get. This is accepted as
the shape of the problem this ADR solves — it fixes the lost-update
correctness bug, not judgment throughput — and is a candidate for a
follow-up if cross-process coalescing turns out to matter in practice (no
issue filed yet).

## Alternatives rejected

- **A lock-coordinator daemon.** Rejected under D3 — it reintroduces the
  always-on process embedded mode exists to avoid.
- **A single lock file per scope** (no split). Rejected under D1 — it would
  serialise appends behind judgments across processes, undoing ADR 0011 D3's
  coalescing the moment a second process is involved.
- **`msvcrt.locking` on Windows.** Rejected under D4 — cannot express a
  blocking wait across processes; a spin loop poorly emulating that is worse
  than the documented degrade.

## Consequences

- `tests/test_interprocess_locks.py` (two real OS processes, one scope)
  passes; the full suite has no regressions.
- Every process that opens a project's DB must call `configure_lock_dir`
  before doing contribute or operator-correction work. All four current
  entry points that can take `scope_lock` do this at store init: the MCP
  server's `_init_stores`, the Console backend's `create_app` lifespan, the
  CLI's `open_embedded_stores` (operator publish/supersede/retire,
  publication bootstrap) and its `_storage_paths` helper, and the
  freshness evaluator's `_submit_judged_contribution`. Nothing currently
  guards a *new* entry point from skipping this call — it is not enforced
  by a type or a runtime check, only by every present call site routing
  through one of those shared store-init functions. A new write-capable
  entry point that opens its own `RecordStore`/`SummaryStore` without going
  through one of them would silently degrade to in-process-only locking
  (the D4 behaviour) rather than fail loudly; `tests/test_cli.py` and
  `tests/test_freshness.py` each assert a lock file appears after one such
  operation as a regression guard on the entry points that exist today.
- Cross-process coalescing does not happen — see "Known limit" above.
- `fcntl.flock(..., LOCK_EX)` has no timeout, unlike the queue's own
  `QUEUE_WAIT_TIMEOUT_S` for same-process waiters. A process that dies or
  hangs while holding a scope's flock (killed mid-judgment, wedged on the
  judge call) stalls every OTHER process's `scope_lock`/`scope_append_lock`
  on that scope indefinitely — there is no equivalent of the queue's
  "fail loudly after 300s" for the cross-process case. Accepted as designed,
  matching the precedent `strata.session_state`'s existing cross-process
  lock already sets (issue #119): the OS releases an `flock` automatically
  when the holding process exits (even a crash), so the practical exposure
  is a hang while the process is still alive, not a permanently stuck lock
  file surviving the process.
- On Windows, cross-process races over one scope remain possible; the README
  documents this the same way it already documents the session-state
  degrade.

---
