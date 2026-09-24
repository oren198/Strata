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
   concurrently with an MCP write, and each read-modify-write is serialized
   across processes by an advisory lock on a per-session lock file (issue #119)
   because the MCP server and the detached background evaluator (#112) both
   mutate it.

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
   here, and a remotely-served host derives the same counters and applies the
   same policy rather than growing its own. Pure function of the counters; it
   never judges, never writes.

Vocabulary follows CONTEXT.md: scope, perspective, contribution, record,
judgment.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Iterator

    from strata.record_store import RecordStore

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows has no fcntl
    # Windows keeps the pre-#119 behaviour: the read-modify-write runs unlocked,
    # so two concurrent writers can still lose an increment. Deliberately NOT
    # papered over with ``msvcrt.locking`` — that locks byte ranges of an open
    # file and cannot express "wait for the other process", so emulating an
    # advisory lock with it means a spin-and-retry loop, and a wrong lock is
    # worse than a documented absence of one. These counters are a best-effort
    # mechanical substrate for the read-time nudge (#111) and the turn-boundary
    # hook (#112): nothing judged or memory-bearing depends on them, and the
    # cost of a lost increment is at most one premature or late nudge. See
    # README § "Windows: session-state counters are not cross-process locked".
    fcntl = None  # type: ignore[assignment]

# The window (in days) the staleness metric looks back over by default. "Over a
# window" (issue #110 deliverable 2): reads older than this never count toward
# the metric, so a scope that was busy months ago and is quiet now does not read
# as perpetually stale.
DEFAULT_STALENESS_WINDOW_DAYS = 30

# How long (seconds) a session with no recorded end may sit idle before the
# write-back rate treats it as ended. A killed (SIGKILL) server never stamps
# ``ended_at``; without this window it would count as "open" forever. Overridable
# per run (``strata stats writeback --idle-window``) and via
# ``Settings.session_idle_window_seconds``.
DEFAULT_SESSION_IDLE_WINDOW_SECONDS = 24 * 60 * 60


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
# Session id resolution — shared by the MCP server and the freshness Stop
# hook (issue #112) so both land on the identical session id from an
# identical environment, with no IPC and no env var required.
# ---------------------------------------------------------------------------


def resolve_agent_session_id(env: dict[str, str] | None = None) -> str:
    """Return the session id this process's session state is keyed by.

    ``STRATA_AGENT_SESSION_ID`` set to a non-empty value is returned as-is —
    explicit binding is never touched by this. Empty string counts as unset
    everywhere in Strata (Codex writes literal empty env values into its
    config), so it falls through to the same auto-generated fallback as an
    absent var.

    The fallback — ``sess_auto_<parent pid>`` — is deterministic, not
    random: the MCP server (``strata-mcp``) is spawned by the harness process,
    so ``os.getppid()`` is that process's pid. The freshness Stop hook must land
    on the same id without IPC: when the harness runs the hook as a direct child
    (Codex, verified on codex-cli 0.153.4 for ``codex exec`` and the TUI) its
    ``os.getppid()`` is the same pid. Claude Code does not: it runs the hook as
    ``/bin/sh -c 'sh <script>'`` and the outer shell survives, so the hook's
    parent is that shell. :func:`strata.freshness._strata_session_id` therefore
    walks up the hook's ancestors to the nearest one that has a session record;
    this function is the server side and stays the plain parent pid.

    A caveat shared with any pid-derived id: pids are reused by the OS over
    time, so a stale session-state file from a past process that happened to
    reuse this pid could in principle be picked back up. Session state's
    normal TTL/staleness handling (the same handling that already tolerates
    a crashed evaluator's stale lock — see ``EVALUATOR_LOCK_TTL_SECONDS`` in
    ``strata.freshness``) is the acceptable mitigation; this is not treated
    as a hard collision risk here.
    """
    env = os.environ if env is None else env
    explicit = env.get("STRATA_AGENT_SESSION_ID", "")
    if explicit:
        return explicit
    return f"sess_auto_{os.getppid()}"


#: The harnesses a session's state can name. ``unknown`` is a client that
#: identified itself as something else (or not at all); ``""`` on a
#: :class:`SessionState` means the file predates harness recording.
HARNESS_CLAUDE_CODE = "claude-code"
HARNESS_CODEX = "codex"
HARNESS_UNKNOWN = "unknown"


def classify_harness(client_name: str | None) -> str:
    """Map an MCP client's ``clientInfo.name`` to the harness it belongs to.

    The name comes from the client's own ``initialize`` handshake, so it is
    per-connection evidence rather than configuration: Codex (codex-cli 0.153.4,
    verified live) sends ``codex-mcp-client``; Claude Code sends ``claude-code``.
    Anything else — or no client info — is ``unknown``, never a guess.
    """
    name = (client_name or "").lower()
    if "codex" in name:
        return HARNESS_CODEX
    if "claude" in name:
        return HARNESS_CLAUDE_CODE
    return HARNESS_UNKNOWN


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

    ended_at: str = ""
    """ISO 8601 time this session ended (MCP connection closed, server terminated,
    or rolled over by a newer connection with the same id); ``""`` while open or
    for a killed session (see :data:`DEFAULT_SESSION_IDLE_WINDOW_SECONDS`)."""

    server_pid: int = 0
    """Pid of the MCP server that owns this record; ``0`` for a file written
    before M3. Lets a later connection tell it is a NEW connection reusing the
    id (rollover), and stops an old server's exit from ending a newer session."""

    strict: bool | None = None
    """Whether strict Stop-hook enforcement was on for this session (recorded at
    connect and by the hook); ``None`` when never recorded."""

    strict_blocked_at: str = ""
    """When the strict Stop hook last blocked this session's stop."""

    strict_blocks: int = 0
    """How many times the strict Stop hook blocked this session (hard cap 2: the
    first reminder, then one last reminder only if the agent made no strata call in
    between). A pre-cap-2 file has only ``strict_blocked_at``; it counts as one."""

    tool_calls: int = 0
    """Strata tool calls this session has made (every ``tools/call`` the server
    handled), so the hook can tell whether the agent reacted to a block."""

    tool_calls_at_last_block: int = 0
    """The ``tool_calls`` value when the last block was issued."""

    submitted: int = 0
    """``strata_contribute`` calls this session made, whatever the verdict (a
    decline counts): the write-back numerator. ``contributions`` above stays the
    accepted count. A file written before this field existed has ``0`` here."""

    connected_at: str = ""
    """ISO 8601 time of this session's first MCP connect (written before any tool
    call, so a session that never does anything is still counted). ``""`` for a
    file written before M2."""

    harness: str = ""
    """Which harness this session ran in (``claude-code`` / ``codex`` /
    ``unknown``), from the MCP client's ``initialize`` handshake. ``""`` for a
    file written before harness recording. Set once by the MCP server; a known
    harness is never overwritten (see :func:`_stamp_harness`)."""

    updated_at: str = ""
    """ISO 8601 timestamp of the last mutation."""


def strict_blocks_so_far(state: SessionState) -> int:
    """How many strict blocks *state* has had (a pre-cap-2 file with only
    ``strict_blocked_at`` counts as one)."""
    return max(state.strict_blocks, 1 if state.strict_blocked_at else 0)


# ---------------------------------------------------------------------------
# Session state store
# ---------------------------------------------------------------------------


#: File (in the sessions dir, not ``*.json``) recording that connect-time recording was off.
CONNECT_SEAM_MARKER = ".connect-seam-unavailable"


class SessionStateStore:
    """Owns the per-session JSON state files under a sessions directory.

    Each session's state lives at ``<sessions_dir>/<session_id>.json``. Writes are
    atomic (``.json.tmp`` sibling + :func:`os.replace`) so a hook reading the file
    concurrently never observes a partial write — same discipline as
    :class:`strata.summary_store.SummaryStore`.

    The record helpers are read-modify-write: they load the current state (or a
    fresh one), mutate a counter, and atomically rewrite. Since #112 there are
    TWO writing processes — the MCP server and the detached background evaluator
    — so each read-modify-write runs under an advisory lock on a per-session lock
    file (issue #119). The atomic rename and the lock answer different problems:
    the rename stops a concurrent *reader* (the #112 hook) from seeing a partial
    file, the lock stops a concurrent *writer* from losing the other's increment.
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

    def lock_path_for(self, session_id: str) -> Path:
        """Return the path of *session_id*'s advisory lock file (no I/O).

        A separate file rather than the state file itself: :meth:`_write` replaces
        the state file by rename, so a lock held on the state file's inode would
        stop guarding it the moment a writer swapped a new inode in. The suffix
        keeps it out of :meth:`all_states`' ``*.json`` scan and distinct from the
        evaluator's own ``.json.eval.lock`` (:mod:`strata.freshness`).
        """
        return self._dir / f"{session_id}.json.lock"

    @contextmanager
    def _locked(self, session_id: str) -> Iterator[None]:
        """Hold *session_id*'s advisory write lock for the duration of the block.

        Serializes the read-modify-write across processes (issue #119) so the
        MCP server and the detached evaluator cannot both read the same counters,
        each increment their own copy, and have the second write erase the first.
        The whole load → mutate → tmp-write → :func:`os.replace` sequence must
        run inside the block; only holding it over the write would still lose the
        update.

        Degrades to a no-op where :mod:`fcntl` is unavailable (Windows) — see
        this module's import guard.
        """
        if fcntl is None:
            yield
            return
        path = self.lock_path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # "a" creates the lock file without truncating an existing one, so two
        # processes racing to create it both end up holding the same inode.
        with path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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

    def scan(self) -> tuple[list[SessionState], int]:
        """Return every readable session state and the count of unreadable files.

        Like :meth:`all_states`, but says how many ``*.json`` files could not be
        parsed instead of dropping them silently, so a rate computed over the
        result can report what it left out.
        """
        states: list[SessionState] = []
        unreadable = 0
        for entry in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
                states.append(SessionState.model_validate(data))
            except (json.JSONDecodeError, ValueError, OSError):
                unreadable += 1
        return states, unreadable

    # ------------------------------------------------------------------
    # The "connect seam unavailable" marker (#206)
    # ------------------------------------------------------------------

    @property
    def _seam_marker_path(self) -> Path:
        # Not `*.json`: the marker is not a session and must never be scanned as one.
        return self._dir / CONNECT_SEAM_MARKER

    def record_connect_seam_unavailable(self, reason: str, *, now: datetime | None = None) -> None:
        """Note that a server ran WITHOUT connect-time recording (the MCP SDK seam moved).

        Sessions that connect, read nothing and contribute nothing leave no file at all in
        that case, so the write-back denominator may undercount. Keeps the first time it
        was seen and updates the last.
        """
        stamp = (now or datetime.now(UTC)).isoformat()
        existing = self.connect_seam_unavailable()
        marker = {
            "reason": reason,
            "first_at": (existing or {}).get("first_at", stamp),
            "last_at": stamp,
        }
        tmp = self._seam_marker_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(marker), encoding="utf-8")
        os.replace(tmp, self._seam_marker_path)

    def connect_seam_unavailable(self) -> dict | None:
        """The marker (``reason``, ``first_at``, ``last_at``), or ``None`` if never written."""
        try:
            data = json.loads(self._seam_marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

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

    @staticmethod
    def _stamp_harness(state: SessionState, harness: str | None) -> None:
        """Record *harness* on *state*: fills an empty or ``unknown`` value, and
        never overwrites a known harness (a session runs in one harness)."""
        if harness and state.harness in ("", HARNESS_UNKNOWN):
            state.harness = harness

    def record_read(
        self,
        session_id: str,
        scope_id: str,
        *,
        now: datetime | None = None,
        harness: str | None = None,
    ) -> SessionState:
        """Record one perspective/summary read of *scope_id* by *session_id*.

        Increments the flat ``reads`` counter and the per-scope receipt.
        """
        ts = (now or datetime.now(UTC)).isoformat()
        with self._locked(session_id):
            state = self.read(session_id) or SessionState(session_id=session_id)
            self._stamp_harness(state, harness)
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

    def record_connect(
        self,
        session_id: str,
        *,
        harness: str | None = None,
        now: datetime | None = None,
        pid: int | None = None,
        strict: bool | None = None,
    ) -> SessionState:
        """Record that *session_id* connected — the write-back denominator.

        Creates the state file with zero counters if absent, and stamps
        ``connected_at`` (first connect wins), the owning server *pid* and the
        harness. Idempotent for the same server: never resets counters.

        Rollover: if a record already exists from an EARLIER connection (a
        different server pid) the id is being reused — a deliberate explicit id,
        or a recycled pid. The earlier record is archived as
        ``<id>.rolled-<n>.json`` with ``ended_at`` set to now (it still counts in
        the write-back report as its own session) and a fresh record starts.
        """
        ts = (now or datetime.now(UTC)).isoformat()
        pid = os.getpid() if pid is None else pid
        with self._locked(session_id):
            state = self.read(session_id)
            if state is not None and state.connected_at and state.server_pid != pid:
                if not state.ended_at:
                    state.ended_at = ts
                self._write_to(state, self._next_archive_path(session_id))
                state = None
            if state is None:
                state = SessionState(session_id=session_id)
            if not state.connected_at:
                state.connected_at = ts
            if not state.server_pid:
                state.server_pid = pid
            self._stamp_harness(state, harness)
            if strict is not None:
                state.strict = strict
            state.updated_at = state.updated_at or ts
            self._write(state)
        return state

    def _next_archive_path(self, session_id: str) -> Path:
        """First unused ``<id>.rolled-<n>.json`` path (matched by the ``*.json`` scan)."""
        n = 1
        while (path := self._dir / f"{session_id}.rolled-{n}.json").exists():
            n += 1
        return path

    def record_end(
        self, session_id: str, *, pid: int | None = None, now: datetime | None = None
    ) -> SessionState | None:
        """Stamp ``ended_at`` on *session_id*'s record — only for the owning server.

        A no-op when there is no record, it already ended, or it belongs to a
        different server pid (an old server exiting after a newer connection
        took over the id must not end the newer session).
        """
        ts = (now or datetime.now(UTC)).isoformat()
        pid = os.getpid() if pid is None else pid
        with self._locked(session_id):
            state = self.read(session_id)
            if state is None or state.ended_at or state.server_pid != pid:
                return state
            state.ended_at = ts
            state.updated_at = ts
            self._write(state)
        return state

    def record_strict(self, session_id: str, strict: bool) -> SessionState:
        """Record whether strict Stop-hook enforcement is on for *session_id*."""
        with self._locked(session_id):
            state = self.read(session_id) or SessionState(session_id=session_id)
            state.strict = strict
            self._write(state)
        return state

    def record_strict_block(self, session_id: str, *, now: datetime | None = None) -> SessionState:
        """Record that the strict Stop hook blocked *session_id*'s stop.

        Counts the block and snapshots ``tool_calls`` so a later stop can tell
        whether the agent made any strata call since.
        """
        ts = (now or datetime.now(UTC)).isoformat()
        with self._locked(session_id):
            state = self.read(session_id) or SessionState(session_id=session_id)
            state.strict = True
            state.strict_blocks = strict_blocks_so_far(state) + 1
            state.strict_blocked_at = ts
            state.tool_calls_at_last_block = state.tool_calls
            self._write(state)
        return state

    def record_tool_call(self, session_id: str) -> SessionState:
        """Count one strata tool call by *session_id* (any tool, any outcome)."""
        with self._locked(session_id):
            state = self.read(session_id) or SessionState(session_id=session_id)
            state.tool_calls += 1
            self._write(state)
        return state

    def record_submission(
        self, session_id: str, *, now: datetime | None = None, harness: str | None = None
    ) -> SessionState:
        """Record one ``strata_contribute`` call by *session_id*, whatever its verdict."""
        ts = (now or datetime.now(UTC)).isoformat()
        with self._locked(session_id):
            state = self.read(session_id) or SessionState(session_id=session_id)
            self._stamp_harness(state, harness)
            state.submitted += 1
            state.updated_at = ts
            self._write(state)
        return state

    def record_contribution(
        self, session_id: str, *, now: datetime | None = None, harness: str | None = None
    ) -> SessionState:
        """Record one accepted contribution act by *session_id* (the release valve)."""
        ts = (now or datetime.now(UTC)).isoformat()
        with self._locked(session_id):
            state = self.read(session_id) or SessionState(session_id=session_id)
            self._stamp_harness(state, harness)
            state.contributions += 1
            state.updated_at = ts
            self._write(state)
        return state

    def record_decline(
        self, session_id: str, *, now: datetime | None = None, harness: str | None = None
    ) -> SessionState:
        """Record one explicit "nothing to record" decline by *session_id*.

        Unused in WP1 (no closeout tool exists yet); present so the store's
        contract is complete for WP2 (#111).
        """
        ts = (now or datetime.now(UTC)).isoformat()
        with self._locked(session_id):
            state = self.read(session_id) or SessionState(session_id=session_id)
            self._stamp_harness(state, harness)
            state.declines += 1
            state.updated_at = ts
            self._write(state)
        return state

    def _write(self, state: SessionState) -> None:
        """Atomically persist *state* (tmp sibling + :func:`os.replace`).

        The tmp sibling carries the writing process's pid: where the advisory
        lock is unavailable (Windows) two writers otherwise share one tmp name,
        and the first ``os.replace`` renames the file out from under the second,
        which then fails with ``FileNotFoundError``. A per-writer tmp name keeps
        that degraded path to its documented cost — a lost increment — instead of
        an exception out of an ordinary read.
        """
        self._write_to(state, self.path_for(state.session_id))

    def _write_to(self, state: SessionState, final: Path) -> None:
        """Atomically persist *state* at *final* (tmp sibling + :func:`os.replace`)."""
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_suffix(f".json.{os.getpid()}.tmp")
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

    The record answers this with one bounded query
    (:meth:`~strata.record_store.RecordStore.get_latest_accepted_contribution`)
    rather than a whole-record scan — the metric is derived on demand and the
    record only ever grows. Returns ``(None, None)`` when the scope has no
    accepted contribution.
    """
    contribution = record_store.get_latest_accepted_contribution(scope_id=scope_id)
    if contribution is None:
        return None, None
    return contribution.created_at, _parse_ts(contribution.created_at)


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

    The library entry point hosts render from: they get the metric per scope
    without reaching into the record or session internals.
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
# Refresh-pending (ADR 0014 pin 4) — NOT a judge outage. A `change_events` row
# that has not yet been drained is an input change waiting for its refresh to
# run, not a judge that ran and failed; every surface that would otherwise
# count an unjudged `manager-refresh` contribution as an outage (`doctor`, the
# Console) calls this one helper instead of re-deriving its own query, so the
# distinction is made once, here, and cannot drift between surfaces.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefreshPending:
    """One scope's refresh queue depth (ADR 0014 D5/D6, pin 4).

    ``depth`` is the count of that scope's UNPROCESSED ``change_events`` rows
    — the drain has not yet run a refresh for them, whatever the eventual
    verdict. ``oldest_pending_at`` is the earliest such row's ``created_at``,
    or ``None`` for an empty queue — what ``strata doctor`` reports as the
    oldest pending event's age.
    """

    scope_id: str
    depth: int
    oldest_pending_at: str | None


def compute_refresh_pending(scope_id: str, *, record_store: RecordStore) -> RefreshPending:
    """Compute *scope_id*'s refresh-pending queue depth, mechanically.

    :meth:`~strata.record_store.RecordStore.list_change_events` already
    returns unprocessed events oldest-first (its own ``ORDER BY created_at``),
    so the first entry — if any — names the oldest pending event without a
    second sort here.
    """
    events = record_store.list_change_events(scope_id=scope_id, unprocessed_only=True)
    return RefreshPending(
        scope_id=scope_id,
        depth=len(events),
        oldest_pending_at=events[0].created_at if events else None,
    )


def compute_fleet_refresh_pending(
    scope_ids: list[str], *, record_store: RecordStore
) -> list[RefreshPending]:
    """Compute :func:`compute_refresh_pending` for each scope, preserving order.

    Mirrors :func:`compute_fleet_staleness` — the library entry point hosts
    render from, without reaching into ``change_events`` themselves.
    """
    return [compute_refresh_pending(scope_id, record_store=record_store) for scope_id in scope_ids]


# ---------------------------------------------------------------------------
# Read-time nudge policy (issue #111 — engine-owned thresholds + wording)
# ---------------------------------------------------------------------------

# Reads with zero write-back before the nudge fires at all. One: the write-back
# rate counts every session, so a session that has read fleet memory and written
# nothing back is worth a soft word from its FIRST read (M3; it was 3 before, when
# early reads were treated as noise). The Stop-hook gate reuses this constant, so
# the read-time nudge and the turn-boundary block can never disagree.
NUDGE_MIN_READS = 1

# At/above this read count (still zero contributions and zero declines) the
# wording escalates in urgency. A single static line becomes wallpaper (#109),
# so the nudge both names the *current* count on every emission and sharpens its
# tone as the gap widens.
NUDGE_ESCALATE_READS = 6


#: What the nudge and the strict block both say, so an agent sees one ask: name
#: both exits — write back what was learned, or close out with a reason.
NUDGE_BOTH_EXITS = (
    "contribute what you learned, or call strata_session_closeout(reason) "
    "if nothing is worth keeping"
)

#: ADR 0017 P1 (v1.15): the one line every agent-facing nudge/norm gains, verbatim —
#: an optional reference nobody is told about is never set.
ACTED_ON_NUDGE_LINE = "If you acted on something from memory, say which item and how it went."


def _render_context_items_clause(items: Sequence[dict]) -> str:
    """``"c_id1 (label1) and c_id2 (label2)"`` / ``"..., c_id3 (label3)"`` for 3+.

    ADR 0017 P1b: the join a reader recognises as an ordinary list, whatever its
    length — one item names itself, two are joined by "and", three or more get an
    Oxford-free "a, b and c" (no serial comma; nothing here is prose meant to read as
    a sentence on its own, only this clause of one).
    """
    parts = [f"{item['id']} ({item['label']})" for item in items]
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + f" and {parts[-1]}"


def compute_nudge(
    state: SessionState | None, *, context_items: Sequence[dict] | None = None
) -> str | None:
    """Return the read-time nudge line for a session's counters, or ``None``.

    The stateful read-time nudge (issue #111): the MCP server appends this to
    ordinary ``strata_*`` read responses (and to ``strata_session_stats`` and
    ``strata_bind`` results) once a session has read fleet memory without writing
    anything back. It is engine-owned policy, computed purely from the #110
    counters — no judge, no write, no memory.

    Silent (``None``) when:

    - there is no session state yet, or reads are below
      :data:`NUDGE_MIN_READS`; or
    - the session has written back: any ``strata_contribute`` call (whatever the
      verdict — a declined one is still a write-back), an accepted contribution,
      or a ``strata_session_closeout``.

    When it fires, the line names the *current* read count (never a static
    string, which would become wallpaper) and both exits
    (:data:`NUDGE_BOTH_EXITS`), and escalates in tone once the count reaches
    :data:`NUDGE_ESCALATE_READS`.

    *context_items* (ADR 0017 P1b): the perspective's self-layer ``context_items`` —
    ``{"id", "label"}`` dicts for the accepted context this read just showed the agent,
    still present verbatim (never one condensed away — see
    :func:`strata.perspective._present_context_items`). Only ``strata_read_perspective``
    has these to give; every other caller of this function passes nothing, and gets the
    generic :data:`ACTED_ON_NUDGE_LINE` exactly as before. When given and non-empty, the
    nudge instead NAMES the ids this session was actually shown — "an agent cannot
    reference an item it never saw an id for" is the root cause this closes (the P1
    adoption run came back 0 of 6).
    """
    if state is None:
        return None
    # Release valve: any write-back silences the nudge.
    if state.contributions > 0 or state.submitted > 0 or state.declines > 0:
        return None
    reads = state.reads
    if reads < NUDGE_MIN_READS:
        return None
    times = "1 time" if reads == 1 else f"{reads} times"
    acted_on_clause = ACTED_ON_NUDGE_LINE
    if context_items:
        acted_on_clause = (
            f"This session read {_render_context_items_clause(context_items)}; if you "
            "acted on one, pass acted_on=<that id> and say what happened."
        )
    if reads >= NUDGE_ESCALATE_READS:
        return (
            f"this session has read fleet memory {times} and still written nothing "
            "back — your scope's memory is going stale while you rely on it. Before "
            f"you finish: {NUDGE_BOTH_EXITS}. {acted_on_clause}"
        )
    return (
        f"this session has read fleet memory {times} and written nothing back yet; "
        f"{NUDGE_BOTH_EXITS}. {acted_on_clause}"
    )


# ---------------------------------------------------------------------------
# The write-back rate (M2) — one outcome per session, one aggregation.
# ---------------------------------------------------------------------------

OUTCOME_CONTRIBUTED = "contributed"
OUTCOME_CLOSED_OUT = "closed_out"
OUTCOME_SILENT = "silent"

#: The row label for session files written before harness recording (harness "").
HARNESS_UNRECORDED = "unrecorded"

#: Harness rows every report carries, in display order.
_REPORT_HARNESSES = (HARNESS_CLAUDE_CODE, HARNESS_CODEX, HARNESS_UNKNOWN)


def session_outcome(state: SessionState) -> str:
    """Return the one outcome of a session: contributed, closed_out or silent.

    ``contributed``: at least one ``strata_contribute`` call, whatever the
    verdict (a declined contribution is still a write-back attempt). A pre-M2
    file has no ``submitted`` counter, so an accepted count also counts.
    ``closed_out``: an explicit closeout and no contribute. ``silent``: neither.
    A session that contributed and then closed out is ``contributed``.
    """
    if state.submitted > 0 or state.contributions > 0:
        return OUTCOME_CONTRIBUTED
    if state.declines > 0:
        return OUTCOME_CLOSED_OUT
    return OUTCOME_SILENT


class WritebackRow(BaseModel):
    """One row of the write-back table: raw counts, never just a percentage."""

    harness: str
    n: int = 0
    """Sessions in the row."""
    contributed: int = 0
    """Sessions with at least one contribute call, any verdict (the numerator)."""
    accepted: int = 0
    """Sessions with at least one ACCEPTED contribution."""
    closed_out: int = 0
    silent: int = 0
    strict_on: int = 0
    """Sessions that ran with strict Stop-hook enforcement on (unrecorded counts as off)."""

    @property
    def rate(self) -> float | None:
        """``contributed / n``, or ``None`` when there are no sessions."""
        return self.contributed / self.n if self.n else None

    @property
    def rate_text(self) -> str:
        """The rate as text: ``"no sessions"`` for an empty row, never a percentage."""
        return "no sessions" if self.rate is None else f"{self.rate:.0%}"

    @property
    def accounted(self) -> int:
        """Sessions accounted for: contributed plus closed out (report only; the
        launch bar stays the write-back rate, ``contributed / n``)."""
        return self.contributed + self.closed_out

    @property
    def accounted_rate(self) -> float | None:
        """``accounted / n``, or ``None`` when there are no sessions."""
        return self.accounted / self.n if self.n else None

    @property
    def accounted_rate_text(self) -> str:
        """The accounted-for rate as text (``"no sessions"`` for an empty row)."""
        return "no sessions" if self.accounted_rate is None else f"{self.accounted_rate:.0%}"


class WritebackReport(BaseModel):
    """The write-back rate over the session-state files, with its window."""

    rows: list[WritebackRow]
    overall: WritebackRow
    strict_on: WritebackRow
    """The same counts over only the sessions that ran with strict enforcement on."""
    strict_off: WritebackRow
    """The same counts over sessions with strict off or never recorded."""
    since: str | None = None
    """The ``--since`` bound applied, if any."""
    first_session_at: str | None = None
    last_session_at: str | None = None
    unreadable_files: int = 0
    """Session files that could not be parsed and are NOT in the counts."""
    include_open: bool = False
    """Whether sessions still open are counted (default: ended sessions only)."""
    open_excluded: int = 0
    """Open sessions left out of the counts (0 when ``include_open``)."""
    idle_window_seconds: int = DEFAULT_SESSION_IDLE_WINDOW_SECONDS
    """A session with no recorded end counts as ended once idle longer than this."""
    denominator_may_undercount: bool = False
    """True when a server ran without connect-time recording (#206): a session that
    connected but never read or contributed left no file, so it is not in the counts."""
    denominator_note: str | None = None
    """What to tell the reader when :attr:`denominator_may_undercount` is true."""


def _session_time(state: SessionState) -> str:
    return state.connected_at or state.updated_at


def _since_bound(since: str | datetime | None) -> datetime | None:
    """Parse a ``since`` bound (ISO 8601 string or datetime); reject a bad string."""
    if since is None or isinstance(since, datetime):
        return since
    bound = _parse_ts(since)
    if bound is None:
        raise ValueError(f"invalid --since value {since!r}: expected an ISO 8601 date or time")
    return bound


def _in_window(state: SessionState, bound: datetime | None) -> bool:
    """Whether *state*'s session time is at or after *bound* (no bound: always)."""
    if bound is None:
        return True
    parsed = _parse_ts(_session_time(state))
    return parsed is not None and parsed >= bound


def session_is_ended(state: SessionState, *, now: datetime, idle_window: timedelta) -> bool:
    """Whether *state* counts as an ended session.

    Ended when ``ended_at`` is set, or — for a session that never recorded an end
    (killed, or a pre-M3 file) — when its last activity is older than
    *idle_window*. A file with no parseable timestamp cannot be open, so it counts
    as ended.
    """
    if state.ended_at:
        return True
    stamps = [t for t in (_parse_ts(state.updated_at), _parse_ts(state.connected_at)) if t]
    if not stamps:
        return True
    return now - max(stamps) > idle_window


def _select_sessions(
    store: SessionStateStore,
    *,
    since: str | datetime | None,
    include_open: bool,
    idle_window: timedelta | None,
    now: datetime | None,
) -> tuple[list[SessionState], int, int, timedelta]:
    """The sessions a report covers: ``(states, open_excluded, unreadable, idle_window)``."""
    idle = (
        idle_window
        if idle_window is not None
        else timedelta(seconds=DEFAULT_SESSION_IDLE_WINDOW_SECONDS)
    )
    clock = now or datetime.now(UTC)
    bound = _since_bound(since)
    states, unreadable = store.scan()
    chosen: list[SessionState] = []
    open_excluded = 0
    for state in states:
        if not _in_window(state, bound):
            continue
        if not include_open and not session_is_ended(state, now=clock, idle_window=idle):
            open_excluded += 1
            continue
        chosen.append(state)
    return chosen, open_excluded, unreadable, idle


def compute_writeback_report(
    store: SessionStateStore,
    *,
    since: str | datetime | None = None,
    include_open: bool = False,
    idle_window: timedelta | None = None,
    now: datetime | None = None,
) -> WritebackReport:
    """Aggregate every session's outcome by harness — the one write-back function.

    The CLI and the export both call this (or :func:`writeback_export_rows`);
    nothing re-derives the rate. Rows: claude-code, codex, unknown (always), an
    ``unrecorded`` row for pre-harness files (only when present), and
    ``overall``; ``strict_on``/``strict_off`` repeat the overall counts split by
    the enforcement each session ran under. *since* keeps sessions whose connect
    time (``updated_at`` for pre-M2 files) is at or after it.

    By default only ENDED sessions are counted (see :func:`session_is_ended`);
    ``include_open=True`` restores the all-sessions view, and ``open_excluded``
    says how many open sessions were left out.

    Retention: nothing in Strata deletes session files on a timer — they stay
    until ``strata unregister --purge-data`` or a manual delete — so the window is
    exactly the sessions on disk. The report states its first/last session time
    and the count of unreadable files it could not include.
    """
    chosen, open_excluded, unreadable, idle = _select_sessions(
        store, since=since, include_open=include_open, idle_window=idle_window, now=now
    )

    rows: dict[str, WritebackRow] = {h: WritebackRow(harness=h) for h in _REPORT_HARNESSES}
    overall = WritebackRow(harness="overall")
    strict_on = WritebackRow(harness="strict on")
    strict_off = WritebackRow(harness="strict off")
    times: list[str] = []
    for state in chosen:
        stamp = _session_time(state)
        if stamp:
            times.append(stamp)
        label = state.harness or HARNESS_UNRECORDED
        row = rows.setdefault(label, WritebackRow(harness=label))
        outcome = session_outcome(state)
        split = strict_on if state.strict else strict_off
        for target in (row, overall, split):
            target.n += 1
            if outcome == OUTCOME_CONTRIBUTED:
                target.contributed += 1
            elif outcome == OUTCOME_CLOSED_OUT:
                target.closed_out += 1
            else:
                target.silent += 1
            if state.contributions > 0:
                target.accepted += 1
        if state.strict:
            for target in (row, overall, split):
                target.strict_on += 1

    parsed_times = sorted(t for t in times if _parse_ts(t) is not None)
    marker = store.connect_seam_unavailable()
    note = None
    if marker is not None:
        note = (
            f"denominator may undercount: since {marker.get('first_at', 'an unknown time')} a "
            f"server ran without connect-time recording ({marker.get('reason', 'unknown')}), "
            "so a session that connected but never read or contributed left no record and "
            "is not counted; the write-back rate may read higher than it is."
        )
    return WritebackReport(
        denominator_may_undercount=marker is not None,
        denominator_note=note,
        rows=list(rows.values()),
        overall=overall,
        strict_on=strict_on,
        strict_off=strict_off,
        since=since if isinstance(since, str) else (since.isoformat() if since else None),
        first_session_at=parsed_times[0] if parsed_times else None,
        last_session_at=parsed_times[-1] if parsed_times else None,
        unreadable_files=unreadable,
        include_open=include_open,
        open_excluded=open_excluded,
        idle_window_seconds=int(idle.total_seconds()),
    )


def writeback_export_rows(
    store: SessionStateStore,
    *,
    since: str | datetime | None = None,
    include_open: bool = False,
    idle_window: timedelta | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """One row per session for the strata-evals loader:
    ``{session_id, harness, outcome, accepted_count}`` (same selection as
    :func:`compute_writeback_report`)."""
    chosen, _, _, _ = _select_sessions(
        store, since=since, include_open=include_open, idle_window=idle_window, now=now
    )
    return [
        {
            "session_id": state.session_id,
            "harness": state.harness or HARNESS_UNRECORDED,
            "outcome": session_outcome(state),
            "accepted_count": state.contributions,
        }
        for state in chosen
    ]
