"""Turn-boundary contribution Stop-hook and background evaluator (issue #112).

Memory-freshness WP3 — the consumer-side turn-boundary hook built on the #110
counters and #111's mechanical decline. Two cooperating pieces live here:

1. **The Stop hook** (:func:`run_stop_hook`). A Claude Code ``Stop`` hook the
   engine ships and ``strata register`` installs. At each turn end it reads the
   session's #110 asymmetry counters and, when the gate opens (reads past the
   threshold, zero contributions, zero declines), it does NOT block the
   interactive loop: it spawns a DETACHED background evaluator and exits 0
   immediately so the user gets their prompt straight back. When the gate is
   closed it exits 0 doing nothing. It degrades silently (always exit 0, never
   noise) when there is no ``.strata`` project, no session state, no API key, or
   on any error — a broken hook must never break the user's session.

   **Strict mode** (opt-in, off by default): when ``STRATA_FRESHNESS_STRICT=1``
   the hook instead BLOCKS the stop once with the contribute-or-decline
   instruction (``{"decision": "block", "reason": ...}``), respecting
   ``stop_hook_active`` so it never loops. No evaluator is spawned in strict
   mode.

2. **The background evaluator** (:func:`run_evaluator`). A headless model run
   that reads the session transcript tail (the hook passes ``transcript_path``
   through), decides whether the session produced a memory-worthy outcome, and
   either (a) sends a contribution through the session's own agent identity —
   the same judged library path the MCP server uses, so the scope-manager gates
   admission exactly as always — or (b) records the mechanical decline via
   :meth:`SessionStateStore.record_decline`, with no judge involvement. The
   model call is injectable (``draft_fn``) so tests never touch the network.

Guards (issue #112 deliverable 3): the gate is checked before spawning (never
an evaluator per turn); at most ONE evaluator is in flight per session (a
lockfile beside the session state file, with a stale-lock TTL); the session
counters reset on the evaluator's outcome (a contribution or a mechanical
decline both close the read/contribute asymmetry, so the gate does not re-open
next turn); and ``stop_hook_active`` is always respected.

Only the decline is ever mechanical. The evaluator's contribution always goes
through the normal judged contribute path — this module never writes memory
without judgment (issue #112 hard guardrail).

Vocabulary follows CONTEXT.md: scope, contribution, scope-manager, record,
directive, context.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TextIO

from strata.session_state import NUDGE_MIN_READS, SessionStateStore, sessions_dir_for

if TYPE_CHECKING:
    from strata.session_state import SessionState

_logger = logging.getLogger("strata.freshness")

# ---------------------------------------------------------------------------
# Policy constants
# ---------------------------------------------------------------------------

#: Default drafting model for the background evaluator. Deliberately cheap — the
#: evaluator runs once per stale session, not per turn (issue #112). Overridable
#: via ``STRATA_EVALUATOR_MODEL``. Distinct from the scope-manager's judge model
#: (``STRATA_MANAGER_MODEL`` / :attr:`Settings.manager_model`): the evaluator
#: only *drafts*; the judge that admits or declines the draft is unchanged.
DEFAULT_EVALUATOR_MODEL = "claude-haiku-4-5-20251001"

#: Env var that opts a project into strict (blocking) mode. Any value other than
#: exactly ``"1"`` leaves the default (non-blocking, background-evaluator) mode.
STRICT_MODE_ENV = "STRATA_FRESHNESS_STRICT"

#: Env var that overrides :data:`DEFAULT_EVALUATOR_MODEL`.
EVALUATOR_MODEL_ENV = "STRATA_EVALUATOR_MODEL"

#: How long (seconds) a one-in-flight evaluator lock is honoured before it is
#: treated as stale and reclaimed. Sized generously above a normal headless
#: evaluator run (one transcript read + one draft call + one judged contribution)
#: so a still-running evaluator is never pre-empted, while a crashed one that
#: left its lock behind is reclaimed on the next turn rather than wedging the
#: session's evaluator forever.
EVALUATOR_LOCK_TTL_SECONDS = 300

#: Bytes of the transcript tail the evaluator reads. The relevant outcomes of a
#: turn live at the end of the transcript; reading a bounded tail keeps the
#: draft call cheap and the token cost predictable.
TRANSCRIPT_TAIL_BYTES = 16_000

#: The instruction the strict-mode block feeds back to the agent. Mirrors the
#: read-time nudge's contribute-or-decline framing (issue #111) so the agent
#: sees one consistent ask.
STRICT_BLOCK_REASON = (
    "This session has read fleet memory but recorded nothing back to it. Before "
    "finishing, contribute the session's outcomes with strata_contribute so the "
    "fleet's memory reflects what happened — or, if there is genuinely nothing "
    "to record, call strata_session_closeout so an empty session stays "
    "distinguishable from a forgotten one."
)


# ---------------------------------------------------------------------------
# Hook stdin
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookInput:
    """The fields the Stop hook consumes from Claude Code's stdin payload.

    ``session_id`` and ``transcript_path`` are Claude Code's own values; the
    Strata session state file is keyed separately by ``STRATA_AGENT_SESSION_ID``
    (resolved from the environment, matching the MCP server), so the two ids may
    differ and only ``transcript_path`` and ``stop_hook_active`` are load-bearing
    here.
    """

    session_id: str
    transcript_path: str
    stop_hook_active: bool


def parse_hook_input(stdin_text: str) -> HookInput | None:
    """Parse the Stop hook's stdin JSON, or ``None`` when it is unusable.

    Silent (``None``) on any malformed input — the hook degrades to a no-op
    rather than raising, so a payload-shape change in the harness can never break
    the user's session.
    """
    try:
        data = json.loads(stdin_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return HookInput(
        session_id=str(data.get("session_id", "")),
        transcript_path=str(data.get("transcript_path", "")),
        stop_hook_active=bool(data.get("stop_hook_active", False)),
    )


# ---------------------------------------------------------------------------
# The gate (reuses the #110 thresholds so hook and read-time nudge never drift)
# ---------------------------------------------------------------------------


def gate_open(state: SessionState | None) -> bool:
    """Return whether the freshness gate is open for a session's counters.

    Open — the session has consumed memory without giving anything back — iff
    the session has read at least :data:`~strata.session_state.NUDGE_MIN_READS`
    times with zero contributions and zero declines. A contribution or a
    mechanical decline (the asymmetry's release valve, issue #109) closes it.

    Identical in shape to the read-time nudge's fire condition
    (:func:`strata.session_state.compute_nudge`), reusing the same threshold
    constant so the turn-boundary hook and the read-time nudge can never fire on
    different criteria.
    """
    if state is None:
        return False
    if state.contributions > 0 or state.declines > 0:
        return False
    return state.reads >= NUDGE_MIN_READS


# ---------------------------------------------------------------------------
# Session-context resolution
# ---------------------------------------------------------------------------


def resolve_session_store(env: dict[str, str]) -> SessionStateStore | None:
    """Resolve the per-session state store for the current project, or ``None``.

    Returns ``None`` (hook degrades to a no-op) when there is no ``.strata``
    project discoverable from the cwd, or when resolution fails for any reason —
    the same best-effort discipline the MCP server's read receipts use.
    """
    try:
        from strata.project_config import resolve_storage_paths  # noqa: PLC0415

        paths = resolve_storage_paths()
        if paths.source != "project":
            # No .strata/config.toml — the env-var dev flow has no stable place a
            # detached hook can find the session file; degrade silently.
            return None
        return SessionStateStore(sessions_dir_for(paths.summaries_dir))
    except Exception as exc:  # noqa: BLE001 — hook must never raise
        _logger.debug("freshness hook: cannot resolve session store: %s", exc)
        return None


def _strata_session_id(env: dict[str, str]) -> str:
    """Return the session id the #110 state file is keyed by.

    The MCP server keys session state by ``STRATA_AGENT_SESSION_ID`` (or a
    generated fallback when unset). The hook runs in the same Claude Code
    process tree and inherits the same environment, so it resolves the id the
    same way — an unset value yields ``""`` and the caller degrades to a no-op
    (a generated MCP fallback id is not knowable from the hook).
    """
    return env.get("STRATA_AGENT_SESSION_ID", "")


# ---------------------------------------------------------------------------
# One-in-flight evaluator lock
# ---------------------------------------------------------------------------


def evaluator_lock_path(store: SessionStateStore, session_id: str) -> Path:
    """Return the lockfile path guarding *session_id*'s evaluator.

    Placed beside the session state file (``<sid>.json.eval.lock``) so it shares
    the session's runtime directory and never collides with the ``*.json`` state
    files :meth:`SessionStateStore.all_states` scans.
    """
    return Path(str(store.path_for(session_id)) + ".eval.lock")


def acquire_evaluator_lock(
    lock_path: Path,
    *,
    now: float | None = None,
    ttl_seconds: int = EVALUATOR_LOCK_TTL_SECONDS,
) -> bool:
    """Atomically acquire the one-in-flight evaluator lock.

    Returns ``True`` when this caller now holds the lock, ``False`` when a fresh
    lock is already held by another evaluator. A lock older than *ttl_seconds*
    is treated as stale (a crashed evaluator that never released it) and
    reclaimed: removed and re-created under this caller.

    Uses ``O_CREAT | O_EXCL`` so exactly one of two concurrent turn-end hooks
    can win the create — the atomicity is the guard against a per-turn evaluator
    storm, not the TTL.
    """
    now = time.time() if now is None else now
    try:
        return _create_lock(lock_path, now)
    except FileExistsError:
        pass

    # A lock already exists — reclaim it only if it is older than the TTL.
    try:
        age = now - lock_path.stat().st_mtime
    except OSError:
        return False
    if age < ttl_seconds:
        return False
    try:
        lock_path.unlink()
    except OSError:
        return False
    try:
        return _create_lock(lock_path, now)
    except FileExistsError:
        # Another turn-end reclaimed it first; treat as held.
        return False


def _create_lock(lock_path: Path, now: float) -> bool:
    """Create *lock_path* exclusively, writing pid+timestamp. Raises on collision."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        os.write(fd, f"{os.getpid()} {now}\n".encode())
    finally:
        os.close(fd)
    return True


def release_evaluator_lock(lock_path: Path) -> None:
    """Release the evaluator lock. Best-effort — a missing lock is not an error."""
    with contextlib.suppress(OSError):
        lock_path.unlink()


# ---------------------------------------------------------------------------
# The Stop hook
# ---------------------------------------------------------------------------

# A spawn function launches the detached evaluator. Injectable so tests can
# assert the gate fires exactly once without launching a real subprocess.
SpawnFn = Callable[[str, str, dict[str, str]], None]


def _default_spawn(session_id: str, transcript_path: str, env: dict[str, str]) -> None:
    """Spawn the background evaluator as a detached process and return at once.

    ``start_new_session=True`` detaches it from the hook's process group so the
    evaluator outlives the hook (which exits immediately); stdio is discarded so
    it can never write to the user's terminal. Invoked via ``sys.executable -m
    strata`` so the child resolves to the same engine running the hook, with no
    PATH assumption.
    """
    import subprocess  # noqa: PLC0415

    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "strata",
            "freshness-evaluator",
            "--session-id",
            session_id,
            "--transcript-path",
            transcript_path,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )


def run_stop_hook(
    stdin_text: str,
    *,
    env: dict[str, str] | None = None,
    out: TextIO | None = None,
    spawn_fn: SpawnFn | None = None,
    now: float | None = None,
) -> int:
    """Run the turn-end Stop hook. Always returns ``0`` (never breaks a session).

    Default mode: when the gate is open, spawn a detached background evaluator
    and return immediately; otherwise do nothing. Strict mode
    (``STRATA_FRESHNESS_STRICT=1``): when the gate is open and the stop was not
    already blocked once, emit the block JSON on *out* and return; never spawn.

    Every failure path — no project, no session state, no API key, a spawn
    error, a malformed payload — degrades to a silent ``return 0``.

    Args:
        stdin_text: The raw JSON Claude Code writes to the hook's stdin.
        env: Environment mapping (defaults to ``os.environ``).
        out: Stream the strict-mode block JSON is written to (defaults to
            ``sys.stdout`` — the hook's decision channel).
        spawn_fn: Detached-evaluator launcher (defaults to :func:`_default_spawn`);
            injected in tests.
        now: Monotonic-ish wall-clock seconds for lock TTL (defaults to
            ``time.time()``); injected in tests.
    """
    env = os.environ if env is None else env  # type: ignore[assignment]
    out = sys.stdout if out is None else out
    spawn_fn = _default_spawn if spawn_fn is None else spawn_fn

    hook_input = parse_hook_input(stdin_text)
    if hook_input is None:
        return 0

    store = resolve_session_store(env)  # type: ignore[arg-type]
    if store is None:
        return 0

    session_id = _strata_session_id(env)  # type: ignore[arg-type]
    if not session_id:
        return 0

    state = store.read(session_id)
    if not gate_open(state):
        return 0

    strict = env.get(STRICT_MODE_ENV) == "1"  # type: ignore[union-attr]
    if strict:
        # Strict mode blocks once. If the stop was already blocked by this hook
        # (stop_hook_active), never block again — that would loop forever.
        if hook_input.stop_hook_active:
            return 0
        out.write(json.dumps({"decision": "block", "reason": STRICT_BLOCK_REASON}))
        return 0

    # Default mode: never block. Spawn the detached evaluator behind the gate and
    # the one-in-flight lock, and hand the user their prompt straight back.
    if not _has_api_key(env):  # type: ignore[arg-type]
        # No key → the evaluator cannot draft. Stay silent rather than block.
        return 0

    lock_path = evaluator_lock_path(store, session_id)
    if not acquire_evaluator_lock(lock_path, now=now):
        # Another evaluator is already in flight for this session — do nothing.
        return 0

    try:
        spawn_fn(session_id, hook_input.transcript_path, dict(env))  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — spawn failure must not break the session
        _logger.debug("freshness hook: evaluator spawn failed: %s", exc)
        # The evaluator never started, so release the lock we just took.
        release_evaluator_lock(lock_path)
    return 0


def _has_api_key(env: dict[str, str]) -> bool:
    """Return whether an Anthropic API key is available for the evaluator."""
    return bool(env.get("STRATA_ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# The background evaluator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluatorDraft:
    """A memory-worthy outcome the evaluator drafts for the judged contribute path.

    The draft is always submitted to the agent's OWN bound scope
    (``STRATA_AGENT_SCOPE``) through the normal scope-manager judgment — the
    evaluator proposes content and a classification hint, never a target scope
    or a verdict.
    """

    content: str
    classification: Literal["directive", "context"]
    subject: str | None = None


# A draft function inspects the transcript tail and returns a draft to contribute
# or ``None`` when the session produced nothing memory-worthy. Injectable so
# tests exercise both outcomes without a model call.
DraftFn = Callable[[str], "EvaluatorDraft | None"]

# The outcome tags run_evaluator returns, for logging and tests.
EvaluatorOutcome = Literal["contributed", "declined", "skipped", "error"]


def read_transcript_tail(transcript_path: str, *, max_bytes: int = TRANSCRIPT_TAIL_BYTES) -> str:
    """Return a best-effort text tail of the session transcript.

    Reads the last *max_bytes* of the transcript file and, when it parses as
    JSONL (Claude Code's transcript format), flattens each entry's text content
    into a readable transcript; otherwise returns the raw tail. Any error yields
    ``""`` — the evaluator then finds nothing to draft and records a mechanical
    decline, which is the safe outcome.
    """
    try:
        path = Path(transcript_path)
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            raw = fh.read()
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")

    lines: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # A partial first line from the byte-seek, or a non-JSONL transcript.
            lines.append(line)
            continue
        rendered = _render_transcript_entry(entry)
        if rendered:
            lines.append(rendered)
    return "\n".join(lines).strip()


def _render_transcript_entry(entry: object) -> str:
    """Flatten one parsed transcript entry into ``role: text``, best-effort."""
    if not isinstance(entry, dict):
        return ""
    message = entry.get("message", entry)
    if not isinstance(message, dict):
        return ""
    role = str(message.get("role") or entry.get("type") or "")
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = " ".join(p for p in parts if p)
    else:
        text = ""
    text = text.strip()
    if not text:
        return ""
    return f"{role}: {text}" if role else text


def run_evaluator(
    *,
    session_id: str,
    transcript_path: str,
    env: dict[str, str] | None = None,
    draft_fn: DraftFn | None = None,
    now: float | None = None,
    release_lock: bool = True,
) -> EvaluatorOutcome:
    """Evaluate a stale session and either contribute (judged) or decline (mechanical).

    Re-checks the gate (the agent may have contributed between spawn and run),
    reads the transcript tail, and asks *draft_fn* whether the session produced a
    memory-worthy outcome:

    - a draft → submitted to the bound scope through the SAME judged contribute
      path the MCP server uses (:func:`strata.app.run_contribution`); the
      scope-manager accepts or declines it. Either way the session's asymmetry
      counters are reset exactly once (an accepted verdict via
      :meth:`SessionStateStore.record_contribution`, otherwise a mechanical
      :meth:`~SessionStateStore.record_decline`) so the gate does not re-open
      next turn.
    - ``None`` → a mechanical :meth:`~SessionStateStore.record_decline`, with no
      judge ever constructed.

    Only the decline is mechanical; a memory write only ever happens through the
    judged path (issue #112 hard guardrail). Always releases the one-in-flight
    lock on the way out.
    """
    env = os.environ if env is None else env  # type: ignore[assignment]
    store = resolve_session_store(env)  # type: ignore[arg-type]
    if store is None:
        return "skipped"
    lock_path = evaluator_lock_path(store, session_id)
    try:
        state = store.read(session_id)
        if not gate_open(state):
            # The agent contributed (or declined) between spawn and run — nothing
            # to do. The gate is already closed.
            return "skipped"

        tail = read_transcript_tail(transcript_path)
        draft_fn = _resolve_draft_fn(env, draft_fn)  # type: ignore[arg-type]
        draft = draft_fn(tail)

        if draft is None:
            # Nothing memory-worthy — mechanical decline, no judge constructed.
            store.record_decline(session_id, now=_as_dt(now))
            return "declined"

        decision = _submit_judged_contribution(draft, env=env)  # type: ignore[arg-type]
        if decision in ("accept_as_directive", "accept_as_context"):
            store.record_contribution(session_id, now=_as_dt(now))
            return "contributed"
        # The judge declined the draft: nothing entered memory. Reset the
        # asymmetry mechanically so the gate does not re-open and re-spawn.
        store.record_decline(session_id, now=_as_dt(now))
        return "declined"
    except Exception as exc:  # noqa: BLE001 — a background run must never crash loudly
        _logger.warning("freshness evaluator failed for session %r: %s", session_id, exc)
        # Close the gate so a persistent failure does not re-spawn every turn; the
        # agent can still contribute or closeout by hand. A pending contribution
        # left by a judge outage is recoverable via strata_rejudge.
        with contextlib.suppress(Exception):
            store.record_decline(session_id, now=_as_dt(now))
        return "error"
    finally:
        if release_lock:
            release_evaluator_lock(lock_path)


def _as_dt(now: float | None):
    """Convert injected wall-clock seconds to a UTC datetime for the store, or None."""
    if now is None:
        return None
    from datetime import UTC, datetime  # noqa: PLC0415

    return datetime.fromtimestamp(now, tz=UTC)


def _resolve_draft_fn(env: dict[str, str], draft_fn: DraftFn | None) -> DraftFn:
    """Return *draft_fn* or the default model-backed drafter bound to env settings."""
    if draft_fn is not None:
        return draft_fn
    import functools  # noqa: PLC0415

    model = env.get(EVALUATOR_MODEL_ENV) or DEFAULT_EVALUATOR_MODEL
    api_key = env.get("STRATA_ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY")
    return functools.partial(_default_draft_fn, api_key=api_key, model=model)


def _submit_judged_contribution(draft: EvaluatorDraft, *, env: dict[str, str]) -> str:
    """Submit *draft* to the bound scope through the judged contribute path.

    Reuses :func:`strata.app.run_contribution` — the exact same
    append→judge→record→summary choke point the MCP ``strata_contribute`` tool
    uses — under the agent's own provenance (``STRATA_AGENT_*``). Returns the
    scope-manager's decision string.

    Raises whatever the judged path raises (e.g. ``JudgeUnavailable``); the
    caller maps it to a mechanical decline so the gate still closes.
    """
    import anthropic  # noqa: PLC0415

    from strata.app import run_contribution  # noqa: PLC0415
    from strata.fleet_config import FleetConfig  # noqa: PLC0415
    from strata.project_config import resolve_storage_paths  # noqa: PLC0415
    from strata.record_store import ContributorRef, RecordStore  # noqa: PLC0415
    from strata.scope_manager import ScopeManager  # noqa: PLC0415
    from strata.settings import get_settings  # noqa: PLC0415
    from strata.summary_store import SummaryStore  # noqa: PLC0415

    scope_id = env.get("STRATA_AGENT_SCOPE", "")
    skill = env.get("STRATA_AGENT_SKILL", "")
    session = env.get("STRATA_AGENT_SESSION_ID", "")

    settings = get_settings()
    paths = resolve_storage_paths(settings)
    fleet = FleetConfig.load(Path(paths.fleet_yaml_path))

    scope = fleet.get_scope(scope_id)
    if scope is None or scope.status == "archived":
        raise RuntimeError(f"evaluator: bound scope {scope_id!r} is not contributable")
    stratum = next((s for s in fleet.strata if s.id == scope.stratum_id), None)
    if stratum is None:
        raise RuntimeError(f"evaluator: stratum {scope.stratum_id!r} not found")

    from datetime import UTC, datetime  # noqa: PLC0415

    contributor = ContributorRef(
        scope_id=scope_id,
        skill=skill,
        session_id=session,
        ts=datetime.now(UTC).isoformat(),
    )
    manager = ScopeManager(
        client=anthropic.Anthropic(api_key=settings.anthropic_api_key),
        model=settings.manager_model,
    )
    with RecordStore(paths.db_path) as record_store:
        summary_store = SummaryStore(paths.summaries_dir)
        outcome = run_contribution(
            scope=scope,
            stratum=stratum,
            content=draft.content,
            proposed_classification=draft.classification,
            subject=draft.subject,
            supersedes=None,
            contributor=contributor,
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=manager,
            summary_max_words=settings.summary_max_words,
        )
    return outcome.decision


# Tool the default drafter forces the model to call — a structured verdict on
# whether the session produced something worth remembering.
_DRAFT_TOOL = {
    "name": "record_freshness_verdict",
    "description": (
        "Decide whether this agent session produced a memory-worthy outcome "
        "worth contributing to the fleet's shared memory."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "memory_worthy": {
                "type": "boolean",
                "description": (
                    "True only if the session reached a decision, resolved a "
                    "question, or observed something the team should remember. "
                    "False for read-only investigation with no outcome."
                ),
            },
            "classification": {
                "type": "string",
                "enum": ["directive", "context"],
                "description": "directive for a binding decision, context otherwise.",
            },
            "content": {
                "type": "string",
                "description": "The memory to record, if memory_worthy is true.",
            },
            "subject": {
                "type": "string",
                "description": "Optional short subject tag.",
            },
        },
        "required": ["memory_worthy"],
    },
}

_DRAFT_SYSTEM = (
    "You are Strata's freshness evaluator. A session read the fleet's shared "
    "memory but recorded nothing back. From the transcript tail, judge whether "
    "the session produced a memory-worthy outcome — a decision made, a question "
    "resolved, a durable observation. If so, draft a concise contribution and "
    "call record_freshness_verdict with memory_worthy=true. If the session was "
    "read-only or produced nothing durable, call it with memory_worthy=false. "
    "You only draft; a scope-manager judges whether the draft is admitted."
)


def _default_draft_fn(
    transcript_tail: str,
    *,
    api_key: str | None,
    model: str,
) -> EvaluatorDraft | None:
    """Model-backed drafter: ask the evaluator model for a structured verdict.

    Best-effort — any failure (no key, API error, malformed tool call, empty
    transcript) returns ``None``, which the caller turns into a mechanical
    decline. The evaluator never writes memory on its own; only a returned draft
    reaches the judged contribute path.
    """
    if not api_key or not transcript_tail.strip():
        return None
    try:
        import anthropic  # noqa: PLC0415

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_DRAFT_SYSTEM,
            tools=[_DRAFT_TOOL],
            tool_choice={"type": "tool", "name": "record_freshness_verdict"},
            messages=[
                {
                    "role": "user",
                    "content": f"Session transcript tail:\n\n{transcript_tail}",
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 — drafting is best-effort
        _logger.debug("freshness evaluator draft call failed: %s", exc)
        return None

    verdict = _extract_tool_input(response)
    if not verdict or not verdict.get("memory_worthy"):
        return None
    content = str(verdict.get("content") or "").strip()
    if not content:
        return None
    classification = verdict.get("classification")
    if classification not in ("directive", "context"):
        classification = "context"
    subject = verdict.get("subject")
    return EvaluatorDraft(
        content=content,
        classification=classification,  # type: ignore[arg-type]
        subject=str(subject).strip() if subject else None,
    )


def _extract_tool_input(response: object) -> dict | None:
    """Pull the forced tool-call input out of an Anthropic response, or ``None``."""
    content = getattr(response, "content", None)
    if not isinstance(content, list):
        return None
    for block in content:
        if getattr(block, "type", None) == "tool_use":
            tool_input = getattr(block, "input", None)
            if isinstance(tool_input, dict):
                return tool_input
    return None
