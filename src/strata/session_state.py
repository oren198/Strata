"""Per-session asymmetry counters and the per-scope staleness metric (issue #110).

Memory-freshness WP1 — the shared substrate for issue #109. Everything here is
**mechanical**: no judge is ever involved, nothing written here enters a scope's
memory, and the derived metric never triggers or gates a judgment. The counters
and the metric only *measure* the read/contribute asymmetry so later work
packages (the read-time nudge #111, the turn-boundary hook #112) have something
specific to say and something cheap to read.

Two pieces live here:

1. **Session state files** — per session, the MCP server records how many
   perspective/summary reads, accepted contribution acts, and explicit declines
   that session has performed. These are NOT memory (no schema change to the
   record or summaries); they live in a runtime area under ``.strata/`` so a
   consumer-side hook (#112) can read one small JSON file cheaply, and so the
   session itself can query its own counts (``strata_session_stats``). The file
   is written atomically (tmp + :func:`os.replace`) because a hook may read it
   concurrently with an MCP write.

   Alongside the flat counters the file keeps a per-scope read receipt
   (``reads_by_scope``: ``count`` + ``last_read_at``). Local Strata has no
   separate read-receipt store — the session state file *is* the read receipt in
   local mode (issue #109 § "what exists today"), so the per-scope substrate the
   staleness metric needs lives here rather than in a new memory write path.

2. **The staleness metric** — for a scope, "N sessions read this scope's
   perspective since its last accepted contribution", bounded by a recency
   window. Derived on demand from the session state files (the receipts) and the
   record (the contributions + judgments); it adds no write path of its own.

3. **The read-time nudge policy** — the thresholds and wording behind the MCP
   server's stateful read-time nudge (issue #111). Engine-owned so every host
   inherits one policy rather than reinventing it: the local MCP server reads it
   here, and a hosted host (strata-web) derives the same counters and applies the
   same policy rather than growing its own. Pure function of the counters; it
   never judges, never writes.

Vocabulary follows CONTEXT.md: scope, perspective, contribution, record,
judgment.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from strata.record_store import RecordStore

# The window (in days) the staleness metric looks back over by default. "Over a
# window" (issue #110 deliverable 2): reads older than this never count toward
# the metric, so a scope that was busy months ago and is quiet now does not read
# as perpetually stale.
DEFAULT_STALENESS_WINDOW_DAYS = 30

# Decisions that count as an *accepted* contribution (the release valve for the
# asymmetry). A ``decline`` verdict is the scope-manager rejecting a proposal —
# it is not an accepted contribution and does not reset the read/contribute gap.
_ACCEPTED_DECISIONS = frozenset({"accept_as_directive", "accept_as_context"})


# ---------------------------------------------------------------------------
# Runtime-area resolution
# ---------------------------------------------------------------------------


def sessions_dir_for(summaries_dir: str | Path) -> Path:
    """Return the per-session state directory for a given summaries directory.

    Session state is runtime state, not memory, so it lives beside the other
    ``.strata/`` runtime artifacts rather than among the scope summaries. The
    summaries directory is the anchor every entry point already resolves through
    the single source of truth (:func:`strata.project_config.resolve_storage_paths`),
    so deriving the sessions directory as a sibling — ``<runtime>/sessions`` next
    to ``<runtime>/summaries`` — keeps it consistent across the CLI and the MCP
    server without touching ``StoragePaths`` or ``.strata/config.toml``.

    For a registered project (``summaries_dir = <root>/.strata/summaries``) this
    resolves to ``<root>/.strata/sessions``; for the env-var dev flow
    (``summaries_dir = ./summaries``) it resolves to ``./sessions``.
    """
    return Path(summaries_dir).parent / "sessions"


# ---------------------------------------------------------------------------
# Session state model
# ---------------------------------------------------------------------------


class ScopeReadReceipt(BaseModel):
    """Per-scope read receipt for one session — the metric's substrate.

    ``last_read_at`` is what the staleness metric compares against a scope's last
    accepted contribution; ``count`` is retained for diagnostics and for a richer
    future nudge. A session that read a scope both before and after its last
    contribution has ``last_read_at`` after it, so it counts — exactly once, per
    session — toward the "N sessions read since" metric.
    """

    count: int = 0
    last_read_at: str


class SessionState(BaseModel):
    """The mechanical asymmetry counters for a single session.

    Persisted as one small JSON file per session. The flat counters
    (``reads`` / ``contributions`` / ``declines``) are what the session and the
    #112 hook read cheaply; ``reads_by_scope`` is the per-scope substrate the
    staleness metric derives from.
    """

    session_id: str
    reads: int = 0
    """Total perspective + summary read acts by this session."""

    contributions: int = 0
    """Accepted contribution acts (accept_as_directive / accept_as_context)."""

    declines: int = 0
    """Explicit "nothing to record" declines. Incremented by the mechanical
    ``strata_session_closeout`` act (WP2, #111) — a decline is not a judged
    contribution but, like one, it resets the read/contribute asymmetry and
    silences the read-time nudge (see :func:`compute_nudge`)."""

    reads_by_scope: dict[str, ScopeReadReceipt] = Field(default_factory=dict)
    """scope_id → the session's read receipt for that scope."""

    updated_at: str = ""
    """ISO 8601 timestamp of the last mutation."""


# ---------------------------------------------------------------------------
# Session state store
# ---------------------------------------------------------------------------


class SessionStateStore:
    """Owns the per-session JSON state files under a sessions directory.

    Each session's state lives at ``<sessions_dir>/<session_id>.json``. Writes are
    atomic (``.json.tmp`` sibling + :func:`os.replace`) so a hook reading the file
    concurrently never observes a partial write — same discipline as
    :class:`strata.summary_store.SummaryStore`.

    The record helpers are read-modify-write: they load the current state (or a
    fresh one), mutate a counter, and atomically rewrite. The MCP server is a
    single process, so no cross-process lock is needed; the atomic rename is only
    to protect a concurrent *reader* (the hook).
    """

    def __init__(self, sessions_dir: str | Path) -> None:
        self._dir = Path(sessions_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def sessions_dir(self) -> Path:
        """Root directory holding the per-session state files."""
        return self._dir

    def path_for(self, session_id: str) -> Path:
        """Return the deterministic path for *session_id*'s state file (no I/O)."""
        return self._dir / f"{session_id}.json"

    def read(self, session_id: str) -> SessionState | None:
        """Return the parsed :class:`SessionState`, or ``None`` if absent/corrupt.

        A corrupt or partially readable file is treated as absent rather than
        raised — this store is a best-effort measurement substrate, never a
        source of truth that a read or contribution should fail on.
        """
        path = self.path_for(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return SessionState.model_validate(data)
        except (json.JSONDecodeError, ValueError, OSError):
            return None

    def all_states(self) -> list[SessionState]:
        """Return every readable session state in the directory.

        Skips ``.tmp`` files and anything that does not parse — the metric is a
        best-effort measurement, so an unreadable file is silently omitted rather
        than aborting the whole computation.
        """
        states: list[SessionState] = []
        for entry in sorted(self._dir.glob("*.json")):
            if entry.name.endswith(".tmp"):
                continue
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
                states.append(SessionState.model_validate(data))
            except (json.JSONDecodeError, ValueError, OSError):
                continue
        return states

    # ------------------------------------------------------------------
    # Mutations (read-modify-write, atomic)
    # ------------------------------------------------------------------

    def record_read(
        self, session_id: str, scope_id: str, *, now: datetime | None = None
    ) -> SessionState:
        """Record one perspective/summary read of *scope_id* by *session_id*.

        Increments the flat ``reads`` counter and the per-scope receipt.
        """
        ts = (now or datetime.now(UTC)).isoformat()
        state = self.read(session_id) or SessionState(session_id=session_id)
        state.reads += 1
        receipt = state.reads_by_scope.get(scope_id)
        if receipt is None:
            state.reads_by_scope[scope_id] = ScopeReadReceipt(count=1, last_read_at=ts)
        else:
            receipt.count += 1
            receipt.last_read_at = ts
        state.updated_at = ts
        self._write(state)
        return state

    def record_contribution(self, session_id: str, *, now: datetime | None = None) -> SessionState:
        """Record one accepted contribution act by *session_id* (the release valve)."""
        ts = (now or datetime.now(UTC)).isoformat()
        state = self.read(session_id) or SessionState(session_id=session_id)
        state.contributions += 1
        state.updated_at = ts
        self._write(state)
        return state

    def record_decline(self, session_id: str, *, now: datetime | None = None) -> SessionState:
        """Record one explicit "nothing to record" decline by *session_id*.

        Unused in WP1 (no closeout tool exists yet); present so the store's
        contract is complete for WP2 (#111).
        """
        ts = (now or datetime.now(UTC)).isoformat()
        state = self.read(session_id) or SessionState(session_id=session_id)
        state.declines += 1
        state.updated_at = ts
        self._write(state)
        return state

    def _write(self, state: SessionState) -> None:
        """Atomically persist *state* (tmp sibling + :func:`os.replace`)."""
        final = self.path_for(state.session_id)
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state.model_dump(), indent=2), encoding="utf-8")
        os.replace(tmp, final)


# ---------------------------------------------------------------------------
# Staleness metric
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeStaleness:
    """The per-scope staleness metric (issue #110 deliverable 2).

    ``reads_since_last_contribution`` is the headline number: how many distinct
    sessions read this scope's perspective/summary since its last accepted
    contribution, within the recency window. A high value means the scope's
    memory is being consumed but not updated — the mechanical signal of drift.
    ``last_accepted_contribution_at`` is ``None`` when the scope has never
    accepted a contribution (every windowed read then counts).
    """

    scope_id: str
    reads_since_last_contribution: int
    last_accepted_contribution_at: str | None
    window_days: int


def _parse_ts(value: str) -> datetime | None:
    """Parse a Strata timestamp into a tz-aware UTC datetime, or ``None``.

    Normalizes the two timestamp shapes Strata produces so they are comparable:
    the record store's ``datetime('now')`` values are naive ``'YYYY-MM-DD
    HH:MM:SS'`` (UTC by construction), while session receipts are timezone-aware
    ISO 8601. A naive value is assumed UTC; an unparseable value yields ``None``.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _last_accepted_contribution_at(
    scope_id: str, *, record_store: RecordStore
) -> tuple[str | None, datetime | None]:
    """Return the (raw, parsed) timestamp of *scope_id*'s last accepted contribution.

    Joins the scope's contributions against their judgments and takes the most
    recent contribution whose verdict accepted it (as a directive or as context).
    Returns ``(None, None)`` when the scope has no accepted contribution.
    """
    contributions = record_store.list_contributions(scope_id=scope_id)
    judgments = {j.contribution_id: j for j in record_store.list_judgments(scope_id=scope_id)}

    latest_raw: str | None = None
    latest_parsed: datetime | None = None
    for contribution in contributions:
        judgment = judgments.get(contribution.id)
        if judgment is None or judgment.decision not in _ACCEPTED_DECISIONS:
            continue
        parsed = _parse_ts(contribution.created_at)
        if parsed is None:
            continue
        if latest_parsed is None or parsed > latest_parsed:
            latest_parsed = parsed
            latest_raw = contribution.created_at
    return latest_raw, latest_parsed


def compute_scope_staleness(
    scope_id: str,
    *,
    record_store: RecordStore,
    session_store: SessionStateStore,
    window_days: int = DEFAULT_STALENESS_WINDOW_DAYS,
    now: datetime | None = None,
) -> ScopeStaleness:
    """Compute the staleness metric for one scope (mechanical, on demand).

    "N sessions read this scope's perspective since its last accepted
    contribution", bounded by *window_days*. A session counts when its most
    recent recorded read of *scope_id* is after the cutoff, where the cutoff is
    the later of the window start and the last accepted contribution — so a read
    that predates the scope's last update, or predates the window, is excluded.

    No write path, no schema change: derived from the session receipts and the
    record alone.
    """
    now = now or datetime.now(UTC)
    window_start = now - timedelta(days=window_days)
    last_raw, last_parsed = _last_accepted_contribution_at(scope_id, record_store=record_store)

    # Reads older than the window never count; reads at/before the last accepted
    # contribution never count. The effective cutoff is whichever is later.
    cutoff = window_start
    if last_parsed is not None and last_parsed > cutoff:
        cutoff = last_parsed

    count = 0
    for state in session_store.all_states():
        receipt = state.reads_by_scope.get(scope_id)
        if receipt is None:
            continue
        read_at = _parse_ts(receipt.last_read_at)
        if read_at is not None and read_at > cutoff:
            count += 1

    return ScopeStaleness(
        scope_id=scope_id,
        reads_since_last_contribution=count,
        last_accepted_contribution_at=last_raw,
        window_days=window_days,
    )


def compute_fleet_staleness(
    scope_ids: list[str],
    *,
    record_store: RecordStore,
    session_store: SessionStateStore,
    window_days: int = DEFAULT_STALENESS_WINDOW_DAYS,
    now: datetime | None = None,
) -> list[ScopeStaleness]:
    """Compute :func:`compute_scope_staleness` for each scope, preserving order.

    The library entry point hosts (strata-web, ADR 0005) render from: they get
    the metric per scope without reaching into the record or session internals.
    """
    now = now or datetime.now(UTC)
    return [
        compute_scope_staleness(
            scope_id,
            record_store=record_store,
            session_store=session_store,
            window_days=window_days,
            now=now,
        )
        for scope_id in scope_ids
    ]


# ---------------------------------------------------------------------------
# Read-time nudge policy (issue #111 — engine-owned thresholds + wording)
# ---------------------------------------------------------------------------

# Reads with zero contributions and zero declines before the nudge fires at all.
# Below this, ``compute_nudge`` returns ``None`` and the read tools append
# nothing (issue #109 direction 2: "append nothing on early reads"). Reads
# happen at session start while contributions belong at the end, so nudging
# from the very first read would be noise; three reads with nothing recorded is
# the point where "this session is consuming memory and giving nothing back" is
# a fair thing to say.
NUDGE_MIN_READS = 3

# At/above this read count (still zero contributions and zero declines) the
# wording escalates in urgency. A single static line becomes wallpaper (#109),
# so the nudge both names the *current* count on every emission and sharpens its
# tone as the gap widens.
NUDGE_ESCALATE_READS = 6


def compute_nudge(state: SessionState | None) -> str | None:
    """Return the read-time nudge line for a session's counters, or ``None``.

    The stateful read-time nudge (issue #111): the MCP server appends this to
    ordinary ``strata_*`` read responses once a session has read enough
    perspectives without recording anything. It is engine-owned policy, computed
    purely from the #110 counters — no judge, no write, no memory.

    Silent (``None``) when:

    - there is no session state yet, or reads are below
      :data:`NUDGE_MIN_READS`; or
    - the session has recorded *any* contribution or decline — the asymmetry's
      release valve (#109): an accepted contribution or a mechanical
      ``strata_session_closeout`` both quiet the nudge for the rest of the
      session.

    When it fires, the line always names the *current* read count (never a
    static string, which would become wallpaper) and escalates in tone once the
    count reaches :data:`NUDGE_ESCALATE_READS`.
    """
    if state is None:
        return None
    # Release valve: a contribution or a mechanical decline silences the nudge.
    if state.contributions > 0 or state.declines > 0:
        return None
    reads = state.reads
    if reads < NUDGE_MIN_READS:
        return None
    if reads >= NUDGE_ESCALATE_READS:
        return (
            f"this session has read fleet memory {reads} times and still contributed "
            "nothing — your scope's memory is going stale while you rely on it. "
            "Contribute your outcomes now with strata_contribute, or call "
            "strata_session_closeout if there is genuinely nothing to record."
        )
    return (
        f"this session has read fleet memory {reads} times and contributed nothing "
        "yet; contribute your outcomes with strata_contribute, or call "
        "strata_session_closeout if there is nothing to record."
    )
