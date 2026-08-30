"""Strata MCP server — stdio transport, embedded mode.

Operates directly on RecordStore, SummaryStore, and FleetConfig in-process.
No HTTP proxy — the FastAPI backend is the Console UI layer only.
(ADR 0004 Decision 1 — embedded mode.)

Vocabulary follows CONTEXT.md verbatim: scope, stratum, directive, context,
contribution, scope summary, perspective, record, provenance, operator.

No agent-facing operator surface exists here (ADR 0008 D1 — agents are never
the operator); ``strata_read_perspective`` composes operator layers into an
agent's own perspective like any other layer (ADR 0008 D2), and that is the
only place operator memory reaches an agent through this server.

Environment variables
---------------------
STRATA_DB_PATH
    Path to the SQLite record store.  Default: ``./strata.db``
STRATA_SUMMARIES_DIR
    Directory for per-scope markdown summary files.  Default: ``./summaries``
STRATA_FLEET_CONFIG
    Path to the fleet YAML file.  Default: ``./fleet.yaml``
STRATA_AGENT_SCOPE
    The scope this agent is bound to (e.g. ``g_backend``).
    Recorded in contribution provenance.  Required when the fleet has 2+
    scopes. When the fleet has exactly one scope, an unset (or empty-string)
    value auto-binds to it. Otherwise (soft-start, ADR 0005 Decision 5,
    dated addendum) the server still starts with an unset value — every
    memory tool returns an actionable error until the session is bound via
    ``strata_bind`` (or the process is restarted with this set).
STRATA_AGENT_SKILL
    The skill this agent is running (e.g. ``strata-developer``).
    Recorded in contribution provenance.  Required for a scope that declares
    skills, unless the scope was auto-bound (single-scope fleet), in which
    case its ``default_skill`` is used when this is unset.
STRATA_AGENT_SESSION_ID
    Unique identifier for this session.
    Recorded in contribution provenance.  Optional (empty string counts as
    unset) — defaults to ``sess_auto_<parent pid>`` when absent, the same
    deterministic fallback the freshness Stop hook computes independently
    (see ``strata.session_state.resolve_agent_session_id``).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from mcp.server.elicitation import AcceptedElicitation, CancelledElicitation, DeclinedElicitation
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from mcp.shared.message import ServerMessageMetadata
from mcp.types import (
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    RequestId,
    ServerRequest,
)
from pydantic import BaseModel, Field

from strata.fleet_config import FleetConfig, FleetConfigError, Scope
from strata.fleet_reload import FleetReloader
from strata.locks import configure_lock_dir
from strata.migrator import run_migrations
from strata.operator import read_operator_layer
from strata.perspective import compose_perspective
from strata.project_config import (
    ProjectConfigError,
    StoragePaths,
    load_project_config,
)
from strata.publication import PublishedItem, propose_publish, propose_withdraw, read_publication
from strata.record_store import ContributorRef, RecordStore
from strata.session_state import (
    SessionState,
    SessionStateStore,
    compute_nudge,
    resolve_agent_session_id,
    sessions_dir_for,
)
from strata.settings import get_settings
from strata.summary_store import ScopeSummary, SummaryStore

# ---------------------------------------------------------------------------
# Module-level runtime state — populated by _init_runtime() from main(),
# AFTER binding validation (issue #46). Nothing at import time touches the
# filesystem, so any storage failure surfaces inside the refuse-to-start
# report instead of as a raw traceback. Tests populate these globals
# directly after importing the module.
#
# Storage paths prefer .strata/config.toml (per-project config, ADR 0005
# Decision 2) via resolve_storage_paths() — the same single source of truth
# used by the CLI and the FastAPI backend (issue #44).
# ---------------------------------------------------------------------------

_settings = get_settings()

# Module-level logger. This is an MCP stdio server — stdout is the JSON-RPC
# channel, so nothing may ever be print()'d there. Python's logging module
# defaults to stderr when no handler is configured, which is exactly what we
# want; we deliberately do not call logging.basicConfig() or attach a stdout
# handler here.
_logger = logging.getLogger("strata.mcp")

_db_path: str = ""
_summaries_dir: str = ""
_fleet_yaml_path: str = ""
_sessions_dir: str = ""
_record_store: RecordStore | None = None
_summary_store: SummaryStore | None = None
_session_store: SessionStateStore | None = None


def _set_paths(paths: StoragePaths) -> None:
    """Publish resolved storage paths to the module globals (no I/O)."""
    global _db_path, _summaries_dir, _fleet_yaml_path, _sessions_dir
    _db_path = paths.db_path
    _summaries_dir = paths.summaries_dir
    _fleet_yaml_path = paths.fleet_yaml_path
    # Session state is runtime state, not memory — derived from the summaries
    # dir (the shared runtime anchor) so it lands beside it under .strata/
    # (issue #110; see strata.session_state.sessions_dir_for).
    _sessions_dir = str(sessions_dir_for(paths.summaries_dir))


def _init_stores() -> None:
    """Initialise storage-backed singletons — called AFTER binding validation.

    Applies pending migrations so the DB is ready before the first tool call.
    Also configures the cross-process scope lock directory (issue #19, ADR
    0012) as ``<db_dir>/.locks`` — deriving it from ``_db_path`` here, rather
    than a separate setting, is what makes every ``strata-mcp`` process (and
    the Console backend, via ``create_app``) that touches the same project's
    DB agree on the same lock files.
    """
    global _record_store, _summary_store, _session_store
    run_migrations(_db_path)
    configure_lock_dir(Path(_db_path).parent / ".locks")
    _record_store = RecordStore(_db_path)
    _summary_store = SummaryStore(_summaries_dir)
    _session_store = SessionStateStore(_sessions_dir)


def _record_read(scope_id: str) -> None:
    """Record one perspective/summary read for this session (best-effort, #110).

    The per-session asymmetry counters and per-scope read receipts are a
    mechanical measurement substrate: a failure to update them must never break
    the read the agent actually asked for, so any storage error is logged and
    swallowed rather than raised.
    """
    if _session_store is None:
        return
    try:
        _session_store.record_read(_AGENT_SESSION_ID, scope_id)
    except OSError as exc:  # pragma: no cover - defensive; disk failure only
        _logger.warning("failed to record read receipt for session %r: %s", _AGENT_SESSION_ID, exc)


def _record_accepted_contribution(decision: str) -> None:
    """Record an accepted contribution act for this session (best-effort, #110).

    Only an accepted verdict (directive or context) is the asymmetry's release
    valve; a decline is the scope-manager rejecting the proposal, not the session
    contributing. Same best-effort discipline as :func:`_record_read`.
    """
    if _session_store is None or decision not in ("accept_as_directive", "accept_as_context"):
        return
    try:
        _session_store.record_contribution(_AGENT_SESSION_ID)
    except OSError as exc:  # pragma: no cover - defensive; disk failure only
        _logger.warning(
            "failed to record contribution counter for session %r: %s", _AGENT_SESSION_ID, exc
        )


def _record_decline() -> SessionState | None:
    """Record one explicit mechanical "nothing to record" decline (#111).

    The decline path is purely a session-state write — the asymmetry's release
    valve, exactly like a read receipt (issue #109): no judge, no admission
    decision, nothing enters memory. Best-effort like the other counters
    (:func:`_record_read`): a disk failure is logged and swallowed rather than
    failing the closeout the agent asked for. Returns the updated state (or
    ``None`` when the store is unavailable or the write failed) so the caller can
    report the now-reset counters.
    """
    if _session_store is None:
        return None
    try:
        return _session_store.record_decline(_AGENT_SESSION_ID)
    except OSError as exc:  # pragma: no cover - defensive; disk failure only
        _logger.warning("failed to record decline for session %r: %s", _AGENT_SESSION_ID, exc)
        return None


def _attach_nudge(result: dict) -> dict:
    """Attach the stateful read-time nudge to a read tool's response (#111).

    The nudge rides in a dedicated ``"nudge"`` key that is present ONLY when the
    session's asymmetry (the #110 counters) warrants one — additive, so the base
    response shape consumers already parse is never disturbed. The policy
    (thresholds, wording) is engine-owned in :func:`strata.session_state.compute_nudge`;
    this only reads the current counters and, when a line comes back, tacks it on.

    Best-effort: a missing or unreadable session store simply yields no nudge,
    never an error on the read the agent actually asked for.

    Also attaches the fleet reload notice (Feature A) via
    :func:`_attach_fleet_notice` — every read tool routes its response
    through here, so this is the one place both additive notices are tacked
    on for reads.
    """
    if _session_store is not None:
        nudge = compute_nudge(_session_store.read(_AGENT_SESSION_ID))
        if nudge is not None:
            result["nudge"] = nudge
    return _attach_fleet_notice(result)


def _build_scope_manager():
    """Construct a :class:`ScopeManager` bound to the configured model.

    Imports the scope-manager lazily (it pulls in the Anthropic SDK, which
    may not be configured in every env) and is only needed when a
    contribution or re-judge actually invokes the judge.
    """
    from strata.scope_manager import ScopeManager  # noqa: PLC0415

    return ScopeManager(
        client=_settings.build_judge_client(),
        model=_settings.manager_model,
    )


# Agent provenance — recorded on every contribution.
# STRATA_AGENT_SCOPE has no default; _validate_binding() enforces it is set
# before mcp.run(). STRATA_AGENT_SKILL is optional (issue #121) — an
# unrestricted scope may bind skill-less, so an unset/empty value maps to
# None (no skill in provenance), never a placeholder. STRATA_AGENT_SESSION_ID
# is optional (empty string counts as unset); resolved once here, at import
# time, via the same deterministic fallback the freshness Stop hook computes
# independently (strata.session_state.resolve_agent_session_id — see its
# docstring for why the two land on the same id with no IPC), and held
# stable for this process's lifetime rather than recomputed per call.
_AGENT_SCOPE: str = os.environ.get("STRATA_AGENT_SCOPE", "")
_AGENT_SKILL: str | None = os.environ.get("STRATA_AGENT_SKILL") or None
_AGENT_SESSION_ID: str = resolve_agent_session_id()

# Soft-start state (dated addendum to ADR 0005 Decision 5 — see
# docs/adr/0005-brownfield-install.md). A harness that swallows stderr (the
# motivating incident: Codex and Claude Code both do) never shows a human
# the refuse-to-start message, so the same aggregated failure lists that used
# to go to sys.exit(1) are instead captured here and handed back as the
# result of every memory tool call until the session is bound — see
# _require_bound_or_elicit(), below. Set once by main() before mcp.run();
# left at their defaults (resolved, no errors) for every test that never
# calls main(), which is what keeps the rest of this module's existing tests
# behaving exactly as before.
#
# Split into two classes (review follow-up — see _validate_binding's
# docstring for the incident this closes): _STARTUP_ERRORS_CONFIG holds
# failures strata_bind/elicitation can NEVER clear (a broken or missing
# .strata/config.toml, or a broken fleet.yaml) — the server may be running
# against the wrong storage source entirely, and the fix only takes effect
# on the next restart. _STARTUP_ERRORS_BINDING holds failures they exist to
# clear (which scope/skill this session acts as). _UNRESOLVED is kept as an
# explicit, cheaply-checked bool — true whenever EITHER list is non-empty —
# recomputed at every mutation site (main(), strata_bind, _attempt_elicit_bind)
# rather than derived on every read.
_UNRESOLVED: bool = False
_STARTUP_ERRORS_CONFIG: list[str] = []
_STARTUP_ERRORS_BINDING: list[str] = []

# Change 2 (elicitation) memo: a client whose elicitation attempt comes back
# anything other than an explicit accept (a protocol-level decline/cancel,
# or a timeout) should not be re-prompted (or, for a timeout, re-hung) on
# its very next tool call in the same process — "one elicitation attempt per
# tool call at most" (spec) reads as "don't nag" across a session's worth of
# calls too.
#
# Live Codex-replay finding: this flag must NEVER be read as "the user
# declined." Codex 0.150.1 was observed declaring the elicitation
# capability and auto-responding decline/cancel with no dialog ever shown to
# a human — a protocol-level non-accept is indistinguishable from a real
# human decline, so attributing it to "the user said no" is a misattribution
# we can never safely make. This flag means only "elicitation appears
# non-functional for this client right now — stop trying it and rely on the
# plain text fallback (the aggregated startup-failure error, which already
# tells an agent to ask the user and call strata_bind)." It never changes
# what that fallback says.
#
# A capability-absent client is never latched here: there is nothing to
# retry differently next time, so the (silent, free) capability check runs
# again rather than being remembered as unavailable. Cleared by a successful
# bind, whether via strata_bind or an accepted elicitation — a fresh binding
# means a fresh session as far as elicitation eligibility goes, so a client
# that came back non-accept once isn't locked out of elicitation forever by
# that.
_ELICIT_UNAVAILABLE: bool = False


@dataclass
class _PendingSwitch:
    """An announced-but-not-yet-confirmed scope switch (self-bind guard
    hardening).

    ``requested_at`` is a :func:`time.monotonic` reading, deliberately NOT
    wall-clock — an NTP jump (backward or forward) must never stretch or
    shrink the confirmation window; monotonic time only ever moves forward
    at a steady rate, so the window is exactly ``_PENDING_SWITCH_WINDOW_
    SECONDS`` of real elapsed time regardless of what the system clock does
    in between.
    """

    target_scope_id: str
    requested_at: float


# Pending-switch state (self-bind guard hardening — live-test finding: a
# bare confirm=True is a PROMISE from whoever is calling, not a GATE. An
# agent could self-supply confirm=True on the very first strata_bind call
# for a switch, indistinguishable from a genuinely user-confirmed one. A
# switch is now honored only when the SAME target scope was announced
# first, on an earlier call, and confirm=True arrives on a FOLLOW-UP call
# naming that same target within _PENDING_SWITCH_WINDOW_SECONDS. One
# outstanding pending switch at a time (this server binds one session at a
# time) — either None, or a _PendingSwitch. Elicitation-accepted switches
# bypass this entirely (the user answered directly, right there — no
# announce-then-confirm dance needed). Cleared on ANY successful bind
# (initial, same-scope no-op, or a confirmed/elicited switch) — an
# announced target shouldn't stay confirmable after unrelated bind
# activity moved the session on. Read and written only under
# _binding_lock, for the same reason every other binding-state write is:
# no torn read of the (target, timestamp) pair.
_PENDING_SWITCH: _PendingSwitch | None = None

# How long an announced-but-unconfirmed switch stays live. A confirm=True
# call naming a DIFFERENT target, or arriving after this window, is cold —
# a fresh announcement (which replaces/restarts the pending record), not a
# confirmation of the old one.
_PENDING_SWITCH_WINDOW_SECONDS = 5 * 60.0

# Guards ONLY the (scope, skill) WRITE in strata_bind (Feature B), so a
# concurrent strata_bind call can't interleave its two assignments with
# another's (no torn write of the pair). It does NOT guard reads: every
# other tool still reads _AGENT_SCOPE/_AGENT_SKILL as plain global lookups,
# with no lock — today's single-request-at-a-time stdio server makes that
# safe in practice, but it means this lock does not, by itself, rule out a
# reader observing a half-updated pair if the server ever becomes
# concurrent. What DOES rule that out today is strata_contribute/
# strata_publish/strata_withdraw each snapshotting the binding into locals
# in one step at the top of the call and using those locals throughout
# (see their comments) — the lock and the snapshot are two different
# defenses for two different halves of the problem: the lock serializes
# strata_bind's own write, the snapshot keeps one call's authorize-then-
# stamp sequence internally consistent even if a rebind lands between two
# separate reads elsewhere. _AGENT_SESSION_ID is never touched by this lock
# — binding to a new scope is the same session working differently, not a
# new session (contributions already carry per-call provenance, so the
# record stays truthful either way).
_binding_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Fleet config helper — lazy reload-on-read (ADR 0004 Decision 1 + ADR 0002
# addendum): every call that needs fleet info stats fleet.yaml first via a
# shared FleetReloader (strata.fleet_reload — the same class the FastAPI
# backend uses on app.state.fleet_reloader). Unchanged file → cached config,
# no re-parse. Changed file → reload through the normal 8+ load-time
# invariants. An invalid file at reload time keeps serving the last good
# fleet and records a warning on the reloader, surfaced to callers via
# _attach_fleet_notice() rather than breaking every subsequent tool call.
# ---------------------------------------------------------------------------

_fleet_reloader: FleetReloader | None = None

# Set by _load_fleet(), from the SAME get_with_warning() call that produced
# the fleet it returned — never re-queried from the reloader separately
# afterward. A get()-then-.warning two-step would race a concurrent reload
# between the two reads; capturing the atomic pair here and having
# _fleet_notice() read only this snapshot avoids that.
_fleet_last_warning: str | None = None


def _load_fleet() -> FleetConfig:
    """Return the current fleet config, reloading fleet.yaml only if it changed.

    Uses the effective fleet YAML path resolved at startup: project config
    takes precedence over env-var settings (ADR 0005 Decision 2). The
    reloader is (re)created if the resolved path has changed since the last
    call — this only happens in tests, which reassign ``_fleet_yaml_path``
    directly after import.
    """
    global _fleet_reloader, _fleet_last_warning
    fleet_path = Path(_fleet_yaml_path)
    if _fleet_reloader is None or _fleet_reloader.path != fleet_path:
        _fleet_reloader = FleetReloader(fleet_path)
    fleet, warning = _fleet_reloader.get_with_warning()
    _fleet_last_warning = warning
    return fleet


def _fleet_notice() -> str | None:
    """Return the fleet reload warning captured by the most recent :func:`_load_fleet` call."""
    return _fleet_last_warning


def _attach_fleet_notice(result: dict) -> dict:
    """Attach a plain-language ``fleet_notice`` to a tool response when the last
    fleet.yaml reload attempt failed (Feature A).

    Additive, like :func:`_attach_nudge`: the key is present only when there
    is something to say, so callers that don't check for it see no shape
    change.
    """
    notice = _fleet_notice()
    if notice is not None:
        result["fleet_notice"] = notice
    return result


def _attach_unbound_notice(result: dict) -> dict:
    """Attach a one-line ``unbound_notice`` when the session is still
    unresolved (soft-start, Change 1) — for the pure fleet-topology tools
    (currently only strata_list_scopes) that are deliberately NOT gated by
    _require_bound_or_elicit: topology is not scoped memory, so an agent
    helping the user bind can still see it, but the unbound state should
    stay visible rather than silently look identical to a normal call.

    Additive, like :func:`_attach_fleet_notice`: the key is present only
    when there is something to say.
    """
    if _UNRESOLVED:
        result["unbound_notice"] = (
            "STOP — do not answer the user's request yet if shared memory would "
            "help. This session is not bound. Ask the user now: which scope "
            "should this session act as (see the scopes above for the choices)? "
            "Then call strata_bind(scope_id=...) with their answer. Do not pick "
            "a scope yourself — binding decides whose memory this session reads "
            "and writes."
        )
    return result


def _publication_item_dict(item: PublishedItem) -> dict:
    """Verbatim item dict for a published item, as returned across MCP tool surfaces."""
    return {
        "id": item.id,
        "kind": item.kind,
        "content": item.content,
        "subject": item.subject,
        "anchors": list(item.anchors),
        "published_at": item.published_at,
    }


# ---------------------------------------------------------------------------
# Entitlement surfaces (ADR 0006 — one model, shared computation, distinct
# capacities). The write surface (ADR 0006 D1, below) is derived from the
# ancestor-chain computation alone and never grows. The read side now splits
# in two (ADR 0006 D3/D4, shipped): a chain-only surface for records and
# perspective targets, and a wider context surface — chain plus
# chain-referenced scopes (one hop, via reference edges at any stratum
# distance — ADR 0010 D2) — for scope summary reads and perspective reference
# layers. Sideways knowledge flow still never extends to writes: it stays
# gated behind ratification or a context-only reference edge, never a direct
# write.
# ---------------------------------------------------------------------------


def _binding_surface_scope_ids(fleet: FleetConfig, agent_scope: str) -> set[str]:
    """Return *agent_scope* plus its inter-stratum ancestor chain.

    This is the shared computation behind the chain-only entitlement surfaces
    (_entitled_scope_ids for records/perspective targets,
    _entitled_write_scope_ids for writes). Referenced scopes are never
    included here — a referenced scope's memory binds this agent only if a
    common ancestor's scope-manager ratifies it into a directive. (They still
    reach this agent through the wider *context* surface — see
    _context_surface_scope_ids — but never through this binding surface.)

    *agent_scope* is taken as an explicit parameter rather than read from the
    ``_AGENT_SCOPE`` module global: a caller that both authorizes against the
    binding and later stamps provenance with it (e.g. strata_contribute)
    snapshots ``_AGENT_SCOPE`` into a local ONCE and threads that same value
    through both, so a concurrent strata_bind rebind between the two can
    never produce "authorized against scope X, stamped as scope Y."
    """
    ancestors = fleet.inter_stratum_ancestors(agent_scope)
    return {agent_scope, *(s.id for s in ancestors)}


# ---------------------------------------------------------------------------
# Entitled chain-only surface (issue #48 — agent-facing reads are scope-
# entitled, not fleet-wide). Decision, recorded on the issue: "Why do we need
# cross scope reads? Isn't it breaking the Strata philosophy?" — it was.
# This surface — the bound scope plus its inter-stratum ancestor chain — now
# gates strata_read_scope_record and the strata_read_perspective *target*
# (ADR 0006 D4): records audit the authority that binds you, and you compose
# perspectives for your own chain, not a peer's. Scope summary reads use the
# wider context surface instead (_check_entitled_context, below).
# ---------------------------------------------------------------------------


def _entitled_scope_ids(fleet: FleetConfig, agent_scope: str) -> set[str]:
    """Return the scope ids entitled for records and perspective targets.

    The chain-only surface is *agent_scope* plus its inter-stratum ancestor
    chain. Intra-stratum peers are deliberately excluded here even when
    chain-referenced — a peer's record is its own, and a peer is never a
    valid perspective target (ADR 0006 D4). Chain-referenced peers are
    readable via the wider context surface (_check_entitled_context) and
    appear as non-binding layers inside a perspective, never as the
    perspective's own target or record.
    """
    return _binding_surface_scope_ids(fleet, agent_scope)


def _check_entitled(fleet: FleetConfig, agent_scope: str, scope_id: str) -> None:
    """Raise RuntimeError if *scope_id* is outside *agent_scope*'s chain-only entitled surface.

    Used by strata_read_scope_record and the strata_read_perspective target
    (ADR 0006 D4) — both stay chain-only even after D3's context surface
    widened scope summary reads.
    """
    if fleet.get_scope(agent_scope) is None:
        # Binding was valid at startup but the bound scope has since vanished
        # from fleet.yaml (rename/removal). Without this check the entitled
        # surface silently collapses and every read gets a misleading
        # peer-entitlement error.
        raise RuntimeError(
            f"your bound scope {agent_scope!r} no longer exists in the fleet "
            "config — fleet.yaml changed since this session started. Restore "
            "the scope in fleet.yaml or relaunch with a valid binding."
        )
    if scope_id not in _entitled_scope_ids(fleet, agent_scope):
        raise RuntimeError(
            f"scope {scope_id!r} is outside your entitled surface "
            f"(your scope {agent_scope!r} plus its inter-stratum ancestors). "
            "Records and perspective targets stay chain-only: a record "
            "audits the authority that binds you, and a perspective is "
            "composed for your own chain, not a peer's. A scope reachable "
            "only through a reference edge informs you via "
            "strata_read_scope_summary and as a non-binding peer_reference "
            "layer inside your own perspective (ADR 0006 D3/D4) — never as "
            "its own record or perspective target."
        )


# ---------------------------------------------------------------------------
# Entitled context surface (ADR 0006 D3/D4 — scope summary reads widen beyond
# the chain-only surface). A chain-referenced peer's summary is composed into
# this agent's perspective anyway (as a non-binding peer_reference layer), so
# refusing the direct summary read is empty ceremony — reuses
# FleetConfig.entitlement_view rather than re-deriving peer logic.
# ---------------------------------------------------------------------------


def _context_surface_scope_ids(fleet: FleetConfig, agent_scope: str) -> set[str]:
    """Return the scope ids entitled for scope summary reads.

    The context surface is *agent_scope*'s chain-only surface plus every
    scope referenced (one hop, via a reference edge at any stratum distance)
    by a scope on that chain — computed via ``fleet.entitlement_view(agent_scope)``
    so reference logic lives in exactly one place.
    """
    view = fleet.entitlement_view(agent_scope)
    return {s.id for s in view.chain} | {s.id for s in view.referenced_peers}


def _check_entitled_context(fleet: FleetConfig, agent_scope: str, scope_id: str) -> None:
    """Raise RuntimeError if *scope_id* is outside *agent_scope*'s entitled context surface."""
    if fleet.get_scope(agent_scope) is None:
        # Same stale-binding hazard as the chain-only check.
        raise RuntimeError(
            f"your bound scope {agent_scope!r} no longer exists in the fleet "
            "config — fleet.yaml changed since this session started. Restore "
            "the scope in fleet.yaml or relaunch with a valid binding."
        )
    if scope_id not in _context_surface_scope_ids(fleet, agent_scope):
        raise RuntimeError(
            f"scope {scope_id!r} is outside your entitled context surface "
            f"(your scope {agent_scope!r}, its inter-stratum ancestors, and "
            "any scope referenced by a scope on that chain via a reference "
            "edge). Unreferenced scopes and descendants are not directly "
            "readable — legitimizing a knowledge flow between scopes is a "
            "reviewed reference edge in fleet.yaml, not a workaround here."
        )


# ---------------------------------------------------------------------------
# Entitled write surface (ADR 0006 Decision D1 — agent-facing contributions
# are target-entitled, mirroring the #48 read surface). `strata_contribute`
# refuses any target outside the bound scope plus its inter-stratum
# ancestors: contributing to your own scope or proposing upward to an
# ancestor is the mechanism of legitimate upward influence (evidence +
# ratification); a direct write into a peer or descendant scope is refused
# structurally, before any judging or recording happens. Unlike the read
# surface, this surface is never extended by reference edges (ADR 0006 D3/
# D4) — sideways flow always requires ratification through a shared
# ancestor. A refusal here is an error, not a scope-manager decline: no
# record row is appended, and the refusal is logged for auditing (grill
# decision, ADR 0006 D1).
# ---------------------------------------------------------------------------


def _entitled_write_scope_ids(fleet: FleetConfig, agent_scope: str) -> set[str]:
    """Return the scope ids *agent_scope* is entitled to contribute to directly.

    The entitled write surface is *agent_scope* plus its inter-stratum
    ancestor chain — identical in shape to the read surface today, computed
    via the same shared helper, but named separately because the two are
    expected to diverge (ADR 0006 D1/D3/D4).
    """
    return _binding_surface_scope_ids(fleet, agent_scope)


def _check_entitled_write(
    fleet: FleetConfig,
    agent_scope: str,
    scope_id: str,
    *,
    agent_skill: str | None = None,
    agent_session_id: str | None = None,
) -> None:
    """Raise RuntimeError if *scope_id* is outside *agent_scope*'s entitled write surface.

    *agent_scope* (and, for the audit log line, *agent_skill*/
    *agent_session_id*) are explicit parameters rather than reads of the
    ``_AGENT_*`` module globals: a caller that goes on to stamp a
    ``ContributorRef`` after this check (strata_contribute) snapshots the
    binding into locals ONCE and passes the SAME values here, so the
    authorization decision and the stamped provenance can never diverge even
    if a concurrent ``strata_bind`` rebinds the session in between. Callers
    that never stamp new provenance (strata_rejudge) may omit
    *agent_skill*/*agent_session_id* — they only affect the log line.
    """
    if fleet.get_scope(agent_scope) is None:
        # Same stale-binding hazard as the read surface: without this check,
        # a bound scope removed from fleet.yaml mid-session would silently
        # collapse the write surface and every write would get a misleading
        # entitlement error instead of a rebind error.
        raise RuntimeError(
            f"your bound scope {agent_scope!r} no longer exists in the fleet "
            "config — fleet.yaml changed since this session started. Restore "
            "the scope in fleet.yaml or relaunch with a valid binding."
        )
    if scope_id not in _entitled_write_scope_ids(fleet, agent_scope):
        _logger.warning(
            "contribution refused: contributor scope=%r skill=%r session=%r "
            "target scope=%r is outside the entitled write surface",
            agent_scope,
            agent_skill,
            agent_session_id,
            scope_id,
        )
        raise RuntimeError(
            f"scope {scope_id!r} is outside your entitled write surface "
            f"(your scope {agent_scope!r} plus its inter-stratum ancestors). "
            "Contribute to your own scope, or propose upward to an ancestor "
            "scope — that is how memory legitimately moves toward broader "
            "authority. Sideways flow to a peer scope happens only through "
            "ratification into a shared ancestor scope, or a context-only "
            "reference edge — never a direct write."
        )


# ---------------------------------------------------------------------------
# Shared scope/skill checks — used by BOTH startup binding validation
# (_validate_binding, below) and strata_bind (Feature B, a rebind at
# runtime). One set of rules answers "is this scope real and is this skill
# allowed for it" no matter which entry point is asking, so the two can never
# drift into disagreeing about what a valid binding is.
# ---------------------------------------------------------------------------


def _scope_waives_skill(scope_obj: Scope) -> bool:
    """True when *scope_obj* declares no skills at all.

    Such an unrestricted scope (no ``default_skill``, no
    ``permitted_skills``) may bind skill-less (issue #121) — a missing skill
    is waived rather than refused.
    """
    return not (scope_obj.default_skill or scope_obj.permitted_skills)


def _resolve_skill_default(scope_obj: Scope, skill: str | None) -> str | None:
    """Return *skill*, or *scope_obj*'s ``default_skill`` when *skill* is unset.

    The companion rule to single-scope auto-bind (startup, check 0 in
    ``_validate_binding``): a scope that declares a ``default_skill`` may be
    bound without naming one explicitly. Shared by startup auto-bind and
    strata_bind so "what skill do I fall back to when none was given" has one
    answer — a scope with a default_skill-only declaration (no
    permitted_skills) never has to make an agent state the obvious.
    """
    return skill or scope_obj.default_skill


def _check_scope_exists(
    fleet: FleetConfig, scope_id: str, *, require_active: bool = False
) -> tuple[Scope | None, str | None]:
    """Return ``(scope_obj, None)`` if *scope_id* is a known — and, when
    *require_active* is set, ``active`` — scope in *fleet*; else
    ``(None, error)`` naming the available scope IDs.

    *require_active* is the shared "an archived scope cannot be bound" rule:
    used by both startup binding validation and strata_bind (Feature B) so
    the two entry points agree on what a bindable scope is, not just on what
    a valid scope id is.
    """
    scope_obj = fleet.get_scope(scope_id)
    if scope_obj is None:
        available = [s.id for s in fleet.active_scopes()]
        available_str = ", ".join(available) if available else "(none — fleet.yaml may be empty)"
        return None, (
            f"scope {scope_id!r} not found in fleet config.\n"
            f"  Available scope IDs: {available_str}\n"
            f"  Update STRATA_AGENT_SCOPE to one of the above, or add scope "
            f"{scope_id!r} to your fleet.yaml."
        )
    if require_active and scope_obj.status != "active":
        available = [s.id for s in fleet.active_scopes()]
        available_str = ", ".join(available) if available else "(none — fleet.yaml may be empty)"
        return None, (
            f"scope {scope_id!r} is archived and cannot be bound.\n"
            f"  Available active scope IDs: {available_str}"
        )
    return scope_obj, None


def _check_skill_permitted(scope_obj: Scope, scope_id: str, skill: str) -> str | None:
    """Return an error string when *skill* is set but not in *scope_obj*'s
    ``permitted_skills``. Empty/absent ``permitted_skills`` means any skill
    is allowed, so this returns ``None`` in that case.
    """
    permitted = scope_obj.permitted_skills or []
    if permitted and skill not in permitted:
        return (
            f"skill {skill!r} is not in the permitted skills for scope "
            f"{scope_id!r}.\n"
            f"  Permitted skills for {scope_id!r}: {', '.join(permitted)}\n"
            f"  Update STRATA_AGENT_SKILL to one of the above, or update permitted_skills in "
            f"fleet.yaml."
        )
    return None


# ---------------------------------------------------------------------------
# Startup validation (ADR 0005 Decision 5; soft-start dated addendum)
# ---------------------------------------------------------------------------


def _validate_binding(
    fleet: FleetConfig | None,
    scope: str,
    skill: str | None,
    *,
    project_config_found: bool = False,
    searched_paths: list[str] | None = None,
    extra_errors: list[str] | None = None,
) -> tuple[str, str | None, list[str], list[str]]:
    """Validate agent binding before starting the MCP server.

    Runs all checks independently and returns every failure, split into two
    classes (per the review follow-up below), rather than first-failure-wins
    — a user with multiple missing pieces sees the complete remediation list
    in one pass.

    Soft-start (dated addendum, docs/adr/0005-brownfield-install.md): this
    function no longer exits the process. A harness that swallows stderr
    (Codex, Claude Code) never surfaces a sys.exit(1) message to the human
    behind it — the motivating incident this addendum fixes. The caller
    (main()) now always proceeds to mcp.run(); when either returned list is
    non-empty it stores both and marks the session unresolved, and every
    memory tool (except strata_bind, the recovery path) returns the relevant
    list as its error result until the session is bound — see
    _require_bound_or_elicit(), below.

    Two failure classes (review follow-up — the incident: strata_bind used to
    clear EVERY startup failure, including ones it never actually fixed; with
    a broken .strata/config.toml the server had already opened storage at the
    env-fallback location, and a "successful" bind afterward would silently
    commit memory there instead of the project's real store):

    - **config-class** (``config_errors``, second-to-last returned list):
      ``.strata/config.toml`` missing/invalid, or a broken fleet.yaml passed
      in as ``extra_errors``. These mean the server may be running against
      the WRONG storage source (or none at all) — nothing a scope pick can
      fix, since the fix (creating/editing ``.strata/config.toml`` or
      ``fleet.yaml``... the config.toml half, anyway) only takes effect on
      the NEXT process start (it's read once, at import time — never
      reload-on-read the way fleet.yaml is). strata_bind and elicitation
      never clear these.
    - **binding-class** (``binding_errors``, last returned list): which
      scope/skill this session acts as. Checks 0, 2, 3, 4, 5 below. Live-
      fixable without a restart via strata_bind (fleet.yaml IS reload-on-
      read) or an accepted elicitation — clearing these is the whole point
      of both.

    Checks (in order, outermost setup gap → innermost binding mismatch):

    0. Single-scope auto-bind: an unset/empty ``STRATA_AGENT_SCOPE`` against a
       fleet with exactly one active scope binds to that scope automatically
       — a one-line notice on stderr names it (operator directive: a fresh
       install must work with minimum friction). When the scope was
       auto-bound and ``STRATA_AGENT_SKILL`` is also unset/empty, the scope's
       ``default_skill`` (if any) is used the same way. An explicitly set
       ``STRATA_AGENT_SCOPE`` is never touched by this — it behaves exactly
       as before. Empty string counts as unset (Codex writes literal empty
       env values into its config).
    1. ``.strata/config.toml`` resolvable via walk-up. **config-class.**
    2. ``STRATA_AGENT_SCOPE`` env var set (after the auto-bind attempt above)
       — unset/empty against a fleet with zero or 2+ active scopes is still
       the actionable error, now naming the available scope IDs when there
       are any. binding-class.
    3. Scope exists in fleet config. binding-class.
    4. ``STRATA_AGENT_SKILL`` env var set — required only when the scope
       *declares* skills (``default_skill`` or ``permitted_skills``). An
       unrestricted scope may bind skill-less (issue #121); a scope that
       expresses skill expectations keeps today's semantics. binding-class.
    5. ``STRATA_AGENT_SKILL`` is in the scope's ``permitted_skills`` (when
       that list is non-empty and a skill is set). binding-class.

    Args:
        fleet:                 The loaded FleetConfig, or ``None`` if check 1
                               failed (no config → no fleet to validate
                               against). Checks 0, 3 + 5 are skipped when
                               fleet is None.
        scope:                 Value of ``STRATA_AGENT_SCOPE`` (may be empty).
        skill:                 Value of ``STRATA_AGENT_SKILL`` (may be empty
                               or None — optional per issue #121).
        project_config_found:  True when ``.strata/config.toml`` was located.
        searched_paths:        Paths that were searched (for the error
                               message when config not found).
        extra_errors:          Config-class startup failures collected before
                               binding validation (malformed config.toml or
                               fleet.yaml, issue #46) — reported alongside
                               check 1 in ``config_errors``, never cleared by
                               strata_bind/elicitation.

    Returns:
        ``(resolved_scope, resolved_skill, config_errors, binding_errors)``
        — the values the caller should bind to (identical to
        ``(scope, skill)`` unless auto-binding applied), and the two
        classified failure lists (each empty on success in that class).
        Always returns — soft-start never exits the process.
    """
    config_errors: list[str] = list(extra_errors) if extra_errors else []
    binding_errors: list[str] = []

    # 1. .strata/config.toml must be resolvable. config-class: fixing this
    #    (creating/editing the file) only takes effect on the next restart.
    if not project_config_found:
        paths_str = (
            "\n  ".join(searched_paths)
            if searched_paths
            else "(no paths — walk-up search from CWD found nothing)"
        )
        config_errors.append(
            ".strata/config.toml not found.\n"
            "  Strata looked for .strata/config.toml walking up from the current directory:\n"
            f"    {paths_str}\n"
            "  Run `strata register` from your project root, then restart the server "
            "(this is read once, at process start — a strata_bind call cannot pick it up)."
        )

    # 0. Single-scope auto-bind — before check 2 sees the scope as missing.
    resolved_scope = scope
    resolved_skill = skill
    if not resolved_scope and fleet is not None:
        sole = fleet.auto_bind_scope()
        if sole is not None:
            resolved_scope = sole.id
            notice = (
                f"Strata: STRATA_AGENT_SCOPE not set — auto-bound to {resolved_scope!r} "
                "(the fleet's only scope)."
            )
            new_skill = _resolve_skill_default(sole, resolved_skill)
            if new_skill != resolved_skill:
                notice += f" Using its default skill {new_skill!r}."
            resolved_skill = new_skill
            print(notice, file=sys.stderr)

    # 2. STRATA_AGENT_SCOPE must be set (after the auto-bind attempt above).
    #    binding-class.
    if not resolved_scope:
        available_line = ""
        if fleet is not None:
            available = [s.id for s in fleet.active_scopes()]
            if available:
                available_line = f"  Available scopes: {', '.join(available)}.\n"
        binding_errors.append(
            "STRATA_AGENT_SCOPE is not set.\n"
            f"{available_line}"
            "  Ask the user which scope this session should act as, then call "
            "strata_bind with their choice — do not pick one yourself; binding "
            "decides whose memory this session reads and writes. (Alternatively "
            "the server can be restarted with STRATA_AGENT_SCOPE/STRATA_AGENT_SKILL "
            "set in its environment — read once, at process start.)\n"
            "  See README.md § 'Quick Start for an existing project' for the full setup."
        )

    # 3. Scope must exist in fleet config and be active (skip when fleet not
    #    loaded or scope unset). require_active=True is the same "archived
    #    scopes cannot be bound" rule strata_bind enforces (Feature B) — one
    #    shared check, not two independently-maintained rules. binding-class.
    scope_obj = None
    if fleet is not None and resolved_scope:
        scope_obj, exists_error = _check_scope_exists(fleet, resolved_scope, require_active=True)
        if exists_error is not None:
            binding_errors.append(exists_error)

    # 4. STRATA_AGENT_SKILL must be set — waived only when the scope is
    #    positively confirmed to declare no skills (no default_skill, no
    #    permitted_skills). Such an unrestricted scope may bind skill-less
    #    (issue #121). A scope that declares skills keeps today's "skill
    #    required" semantics, and an unknown/unresolved scope still surfaces
    #    the skill remediation alongside the scope one (all still reported in
    #    one pass, just classified). binding-class.
    scope_waives_skill = scope_obj is not None and _scope_waives_skill(scope_obj)
    if not resolved_skill and not scope_waives_skill:
        binding_errors.append(
            "STRATA_AGENT_SKILL is not set.\n"
            "  Ask the user which scope (and skill, if they have one in mind) this "
            "session should act as, then call strata_bind with their choice — do "
            "not pick one yourself. (Alternatively the server can be restarted "
            "with STRATA_AGENT_SCOPE/STRATA_AGENT_SKILL set in its environment — "
            "read once, at process start.)\n"
            "  (A skill is optional when the scope declares none.)\n"
            "  See README.md § 'Quick Start for an existing project' for the full setup."
        )

    # 5. STRATA_AGENT_SKILL must be in permitted_skills (skip when scope or
    #    skill missing). binding-class.
    if scope_obj is not None and resolved_skill:
        skill_error = _check_skill_permitted(scope_obj, resolved_scope, resolved_skill)
        if skill_error is not None:
            binding_errors.append(skill_error)

    all_errors = config_errors + binding_errors
    if all_errors:
        # Still printed for a human who does read stderr (local dev) — but
        # soft-start no longer exits on it; the same lists also travel back
        # via _STARTUP_ERRORS_CONFIG/_STARTUP_ERRORS_BINDING for every memory
        # tool to report (see main() and _require_bound_or_elicit(), below).
        header = (
            "Strata MCP server started but is not yet bound — "
            f"{len(all_errors)} validation "
            f"{'failure' if len(all_errors) == 1 else 'failures'}:\n"
        )
        body = "\n".join(f"\n[{i + 1}] {err}" for i, err in enumerate(all_errors))
        print(header + body, file=sys.stderr)

    return resolved_scope, resolved_skill, config_errors, binding_errors


def _resolve_bind(
    fleet: FleetConfig, scope_id: str, skill: str | None
) -> tuple[str | None, list[str]]:
    """Validate a candidate (scope_id, skill) binding against *fleet*.

    The one rule for "is this a valid binding to rebind to right now",
    shared by strata_bind (an explicit agent request) and the elicitation
    fallback in _attempt_elicit_bind (an accepted scope pick) — so the two
    entry points can never drift into disagreeing about what a valid
    rebind is. Deliberately does NOT touch the _AGENT_SCOPE/_AGENT_SKILL
    globals; callers apply the result themselves after deciding how to
    react to a failure (raise vs. silently give up).

    Returns:
        ``(resolved_skill, errors)`` — ``errors`` is empty on success, and
        ``resolved_skill`` is the skill to bind (including a resolved
        ``default_skill`` when *skill* was omitted).
    """
    errors: list[str] = []
    scope_obj, exists_error = _check_scope_exists(fleet, scope_id, require_active=True)
    if exists_error is not None:
        return None, [exists_error]

    assert scope_obj is not None
    # Companion default-skill rule (shared with startup auto-bind, same
    # helper): a scope with a default_skill may be bound without naming
    # one, so an omitted skill is not automatically "requires a skill."
    resolved_skill = _resolve_skill_default(scope_obj, skill)

    if resolved_skill:
        skill_error = _check_skill_permitted(scope_obj, scope_id, resolved_skill)
        if skill_error is not None:
            errors.append(skill_error)
    elif not _scope_waives_skill(scope_obj):
        errors.append(
            f"scope {scope_id!r} declares skills (default_skill or "
            "permitted_skills) but no skill was given.\n"
            "  Pass skill=<one of its permitted skills>, or omit skill only "
            "for an unrestricted scope."
        )

    return resolved_skill, errors


def _unresolved_message(fleet: FleetConfig | None) -> str:
    """Build the error text a memory tool returns while the session is unbound.

    Live-replay finding: an agent hit this exact error on its very first
    question and answered from the repo anyway, without asking the user
    anything at all — it read the validation-failure details and recovery
    mechanics as background information rather than a blocking instruction,
    since nothing at the top of the message said "stop." Fix: an unmissable
    imperative line now LEADS the whole message, before any of the
    ``[1]``/``[2]``-numbered failure details or recovery mechanics — "STOP
    — do not answer yet... ask the user now... then call strata_bind." Only
    once that instruction has been read does the message continue into the
    numbered failures and the class-aware detail below.

    That detail is the same content the refuse-to-start message used to
    print to stderr (now unreadable inside a harness), plus recovery
    instructions that are new to soft-start and class-aware (review
    follow-up): a config-class failure (broken/missing .strata/config.toml,
    broken fleet.yaml) can only be fixed by editing the file and restarting
    the process — strata_bind can't touch it, so its section never mentions
    strata_bind. A binding-class failure (which scope/skill) is
    strata_bind/elicitation-eligible and says so. Either section is omitted
    when that class has no failures, so a session left gated ONLY by a
    config-class problem after a successful strata_bind never sees a stale
    "call strata_bind" line for a binding that is already resolved.
    """
    all_errors = _STARTUP_ERRORS_CONFIG + _STARTUP_ERRORS_BINDING
    scope_ids = [s.id for s in fleet.active_scopes()] if fleet is not None else []

    if _STARTUP_ERRORS_BINDING:
        scopes_paren = f" (Available: {', '.join(scope_ids)}.)" if scope_ids else ""
        stop_line = (
            "STOP — do not answer the user's request yet. This project keeps "
            "shared memory your answer should draw on, but this session is not "
            "bound. Ask the user now: which scope should this session act as?"
            f"{scopes_paren} Then call strata_bind with their choice and retry "
            "this read.\n\n"
        )
    else:
        # Config-class only: no scope choice can fix this, so the
        # imperative points at telling the user and fixing/restarting
        # instead of asking a scope question that wouldn't help.
        stop_line = (
            "STOP — do not answer the user's request yet. This project keeps "
            "shared memory your answer should draw on, but this session's "
            "memory access is broken (see the failure below) — no scope choice "
            "fixes this. Tell the user, then fix the file(s) named below and "
            "restart the server.\n\n"
        )

    header = (
        "Strata MCP server started but this session is not yet bound — "
        f"{len(all_errors)} startup validation "
        f"{'failure' if len(all_errors) == 1 else 'failures'}:\n"
    )
    body = "\n".join(f"\n[{i + 1}] {err}" for i, err in enumerate(all_errors))

    sections: list[str] = []
    if _STARTUP_ERRORS_CONFIG:
        sections.append(
            "\n\nThe config/storage-source failure(s) above cannot be fixed by "
            "strata_bind — the server may be reading (or about to read) the "
            "wrong fleet.yaml or database entirely. Fix the file(s) named "
            "above and restart the server; a strata_bind call in the "
            "meantime cannot make this session's memory land in the right "
            "place."
        )
    if _STARTUP_ERRORS_BINDING:
        scopes_line = (
            f"  Available scope IDs: {', '.join(scope_ids)}\n"
            if scope_ids
            else "  (no active scopes found in fleet.yaml)\n"
        )
        sections.append(
            "\n\nTo recover the scope/skill binding without restarting this server:\n"
            f"{scopes_line}"
            "  Ask the user which scope this session should act as — never pick one "
            "yourself, binding decides whose memory this session reads and writes — "
            "then call strata_bind(scope_id=<their answer>[, skill=<skill>]) with "
            "their choice, or\n"
            "  if the scope they want isn't listed, fix fleet.yaml (its scopes, its "
            "permitted_skills) and call strata_bind again once they've confirmed the "
            "right one — fleet.yaml is re-read as part of that call, so a fix made "
            "after startup is bindable immediately, or\n"
            "  restart the server with STRATA_AGENT_SCOPE/STRATA_AGENT_SKILL set in its "
            "environment (these are read once, at process start — setting them in your "
            "shell after the server is already running has no effect on this session)."
        )
    sections.append(
        "\n\n  strata_list_scopes works unbound too (fleet topology is not scoped "
        "memory) if you need the full strata/scope/edge picture before picking."
    )
    sections.append(
        "\n\n  Never read or write files under .strata/ directly (its database, "
        "session files, or summaries) to work around this — that bypasses binding "
        "and judgment entirely. All memory access goes through the strata tools."
    )
    return stop_line + header + body + "".join(sections)


class _ScopePick(BaseModel):
    """Elicitation response schema (Change 2): the scope id the caller picked."""

    scope_id: str = Field(description="The scope_id to bind this session to.")


class _SwitchConfirm(BaseModel):
    """Elicitation response schema (self-bind guard): does the user confirm
    switching this session's already-bound scope to a different one?"""

    confirm: bool = Field(description="True to confirm switching this session's bound scope.")


# How long the server waits for a capability-declaring client to answer an
# elicitation before giving up. Context.elicit() / ServerSession.
# elicit_form() (mcp/server/fastmcp/server.py, mcp/server/session.py) don't
# expose a timeout parameter at all, even though the primitive underneath
# both of them — BaseSession.send_request() (mcp/shared/session.py) — takes
# one (request_read_timeout_seconds) and races it via anyio.fail_after,
# raising McpError on expiry. Without threading one through here, a client
# that declares the capability and then never answers hangs this tool call
# forever. _send_elicitation, below, calls send_request directly (bypassing
# the two convenience wrappers) so this timeout actually applies — shared by
# every elicitation this module sends (the scope pick, and the rebind
# confirmation).
_ELICIT_TIMEOUT = timedelta(seconds=120)


async def _send_elicitation(
    session: ServerSession,
    message: str,
    schema: type[BaseModel],
    related_request_id: RequestId | None,
) -> AcceptedElicitation | DeclinedElicitation | CancelledElicitation:
    """Send an elicitation request with _ELICIT_TIMEOUT enforced, generic over
    the response *schema* — shared by the scope-pick elicitation (Change 2,
    ``_ScopePick``) and the rebind-confirmation elicitation (self-bind guard,
    ``_SwitchConfirm``), so both go through the one place that actually
    threads a timeout.

    Replicates what mcp.server.elicitation.elicit_with_validation +
    ServerSession.elicit_form do internally (build an ElicitRequest, send it,
    interpret the ElicitResult) but calls session.send_request(...) directly
    so request_read_timeout_seconds can be passed — see _ELICIT_TIMEOUT's
    docstring for why neither of those wrappers allows that. A timeout
    surfaces as an McpError, same as any other transport failure; every
    caller treats that as elicitation-unavailable, never as the user's
    answer — a protocol-level failure proves nothing about what (if
    anything) a human decided.
    """
    result = await session.send_request(
        ServerRequest(
            ElicitRequest(
                params=ElicitRequestFormParams(
                    message=message,
                    requestedSchema=schema.model_json_schema(),
                ),
            )
        ),
        ElicitResult,
        request_read_timeout_seconds=_ELICIT_TIMEOUT,
        metadata=ServerMessageMetadata(related_request_id=related_request_id),
    )
    if result.action == "accept" and result.content is not None:
        return AcceptedElicitation(data=schema.model_validate(result.content))
    elif result.action == "decline":
        return DeclinedElicitation()
    else:
        return CancelledElicitation()


def _elicitation_session_if_capable() -> tuple[Context, ServerSession] | None:
    """Return ``(ctx, session)`` when an MCP request is in flight AND the
    client declares the elicitation capability; ``None`` otherwise (no
    session, or the client doesn't support it).

    The one "can we even ask?" check, shared by the scope-pick elicitation
    (Change 2) and the rebind-confirmation elicitation (self-bind guard) —
    so both entry points agree on what "the client can be asked" means.
    Never raises: any failure getting the context or session is treated the
    same as "can't ask."
    """
    try:
        ctx = mcp.get_context()
        request_context = ctx.request_context
        if request_context is None:
            return None
        session = request_context.session
        if not session.check_client_capability(
            ClientCapabilities(elicitation=ElicitationCapability())
        ):
            return None
        return ctx, session
    except Exception:  # noqa: BLE001 - no session / no capability: nothing was asked
        return None


async def _attempt_elicit_bind(fleet: FleetConfig) -> bool:
    """Try once to resolve the unbound session via server-initiated elicitation.

    Change 2. Offers the caller a pick of the fleet's active scopes; an
    accepted pick is bound via the exact same rule strata_bind enforces
    (_resolve_bind, above) — "bind via the same path as strata_bind." Clears
    ONLY _STARTUP_ERRORS_BINDING on success — a config-class failure (see
    _validate_binding's docstring) is left exactly as it was; the caller
    (_require_bound_or_elicit) never reaches this function while one is
    present, since a scope pick cannot fix it.

    Tolerant of everything: no MCP session available, the client not
    declaring the elicitation capability, a validation failure on the picked
    scope — every one of these returns False so the caller falls back to the
    plain Change-1 error result, without latching (nothing was actually
    asked of the client, so there's nothing to "not ask again"). A client
    that DOES get asked and comes back anything other than an explicit
    accept — a protocol-level decline or cancel, or a timeout — within
    _ELICIT_TIMEOUT sets _ELICIT_UNAVAILABLE so this process doesn't
    re-prompt (or re-hang on) the same client on its very next call. That
    flag never changes what the fallback error says — see its module-level
    docstring for why a non-accept must never be read as "the user
    declined." Never raises.
    """
    global _ELICIT_UNAVAILABLE

    if _ELICIT_UNAVAILABLE:
        return False

    scopes = fleet.active_scopes()
    if not scopes:
        return False

    capable = _elicitation_session_if_capable()
    if capable is None:
        return False
    ctx, session = capable

    listing = "\n".join(f"- {s.id}: {s.name}" for s in sorted(scopes, key=lambda s: s.id))
    message = (
        "This session needs a scope binding to continue — ask the user which "
        "scope it should act as (binding decides whose memory this session "
        "reads and writes). Choices:\n" + listing
    )

    try:
        result = await _send_elicitation(session, message, _ScopePick, ctx.request_id)
    except Exception:
        # Covers a timeout (McpError, after _ELICIT_TIMEOUT) and any other
        # transport failure once a request was actually sent to the client.
        # Marked non-functional so this process doesn't hang on, or
        # re-prompt, the same client again — NOT attributed to the user
        # (nothing here proves a human ever saw a dialog).
        _ELICIT_UNAVAILABLE = True
        return False

    if not isinstance(result, AcceptedElicitation):
        # DeclinedElicitation, CancelledElicitation, or an unexpected shape.
        # Live Codex-replay finding: a protocol-level decline/cancel can
        # come back with no dialog ever shown to a human (an
        # elicitation-capable client auto-responding), so this is marked
        # "elicitation non-functional," never "the user declined" — the
        # fallback below (the plain, class-aware unbound error) already
        # asks the agent to ask the user; nothing here overrides that.
        _ELICIT_UNAVAILABLE = True
        return False

    picked_scope_id = result.data.scope_id
    resolved_skill, errors = _resolve_bind(fleet, picked_scope_id, None)
    if errors:
        return False

    global _AGENT_SCOPE, _AGENT_SKILL, _UNRESOLVED, _STARTUP_ERRORS_BINDING
    with _binding_lock:
        _AGENT_SCOPE = picked_scope_id
        _AGENT_SKILL = resolved_skill
        _STARTUP_ERRORS_BINDING = []
        _UNRESOLVED = bool(_STARTUP_ERRORS_CONFIG)
        # A fresh binding clears the memo too — see its docstring (module
        # top): a client marked non-functional once isn't locked out of
        # elicitation forever by that.
        _ELICIT_UNAVAILABLE = False
    return True


async def _require_bound_or_elicit() -> None:
    """Guard called at the top of every tool that touches actual memory —
    a scope's summary, record, perspective, or session state — except
    strata_bind. Pure fleet-topology reads (currently only
    strata_list_scopes) do NOT call this: topology is not scoped memory,
    so an unbound agent helping the user bind can still see it — see
    _attach_unbound_notice, its ungated counterpart.

    A no-op — zero overhead — once the session is resolved (the common
    case). While unresolved: tries exactly one elicitation attempt (Change
    2) when the fleet itself loads fine AND the only remaining problem is
    binding-class (nothing to offer a pick of when the fleet won't load; no
    point offering one when a config-class failure means the session stays
    gated regardless of what gets picked — see _validate_binding's
    docstring); on any outcome other than a successful bind, raises with
    the same aggregated, class-aware startup-failure list plus recovery
    instructions that used to only reach stderr (Change 1).
    """
    if not _UNRESOLVED:
        return

    fleet: FleetConfig | None
    try:
        fleet = _load_fleet()
    except Exception:  # noqa: BLE001 - a fleet that still won't load has nothing to elicit
        fleet = None

    if not _STARTUP_ERRORS_CONFIG and fleet is not None and await _attempt_elicit_bind(fleet):
        return

    raise RuntimeError(_unresolved_message(fleet))


async def _attempt_elicit_switch_confirm(previous_scope_id: str, requested_scope_id: str) -> bool:
    """Try once, via elicitation, to get the user's DIRECT confirmation for
    switching this session's bound scope from *previous_scope_id* to
    *requested_scope_id* — the self-bind guard's Change-3 upgrade: where the
    client supports it, this REPLACES strata_bind's confirm= parameter
    dance.

    Live Codex-replay finding (bug): a real incident hit this exact path —
    on the FIRST switch call, with no dialog ever shown to a human, the
    result claimed ``switch_declined: True`` / "The user declined." Codex
    0.150.1 evidently declares the elicitation capability and auto-responds
    decline/cancel with no UI at all. A protocol-level decline is
    INDISTINGUISHABLE from a real human decline over this wire protocol, so
    treating "not accepted" as "the user said no" is a misattribution this
    function must never make again.

    Returns:
        ``True`` — the client declared the elicitation capability, was
            asked, and came back with an explicit ACCEPT carrying
            ``confirm=True``. This is the ONLY outcome with any authority
            to switch — an agent (or a client auto-responding on its
            behalf) never gets to supply this answer; only a real ACCEPT
            from the session can.
        ``False`` — every other outcome, with no exceptions: no MCP
            session, the client doesn't declare the capability, a
            protocol-level decline or cancel, a timeout, a transport
            error, or even an ACCEPT carrying ``confirm=False``. The
            caller treats ``False`` uniformly — it falls straight through
            to the standard announce-then-confirm two-step
            (``_switch_pending_result``), which asks the user again on a
            genuine follow-up call. That text-only path is exactly where a
            real "no" and "the dialog never worked" both belong: either
            way, the next right step is the same — ask the user directly,
            then get a real second call.

    Deliberately does NOT touch _ELICIT_UNAVAILABLE: unlike the scope-pick
    elicitation (Change 2), this is per-call only — a user (or a client
    that was simply having a bad moment) may legitimately answer the very
    same switch differently a moment later, so nothing here should lock a
    later attempt out.
    """
    capable = _elicitation_session_if_capable()
    if capable is None:
        return False
    ctx, session = capable

    message = (
        f"This session is currently bound to {previous_scope_id!r}. Switching to "
        f"{requested_scope_id!r} changes whose memory this session reads and writes "
        "for the rest of the session. Please confirm: should it switch?"
    )

    try:
        result = await _send_elicitation(session, message, _SwitchConfirm, ctx.request_id)
    except Exception:  # noqa: BLE001 - timeout/transport failure: not an answer, fall through
        return False

    if not isinstance(result, AcceptedElicitation):
        # Protocol-level decline/cancel, or an unexpected shape — never
        # attributed to the user (see the docstring above); falls through
        # to the standard two-step exactly like every other non-accept.
        return False
    return bool(result.data.confirm)


def _switch_pending_result(previous_scope_id: str, requested_scope_id: str) -> dict:
    """Build the heads-up result for an unconfirmed scope switch (self-bind
    guard) — the binding is left COMPLETELY unchanged in every case this
    builds a result for.

    Live Codex-replay finding (bug fix): this used to take a *declined*
    flag and, when true, say "The user declined. The binding stands." —
    but a protocol-level elicitation decline/cancel/timeout is
    indistinguishable from a real human "no" over the wire, so that wording
    was a claim this function could not actually back up. There is now
    exactly one message, used for every reason a switch isn't happening
    yet: no confirmation offered, a client that can't be asked, or an
    elicitation that came back anything other than an explicit accept.
    Whatever the reason, the next right step is identical — ask the user
    directly, then make a genuine follow-up call — so the result never
    needs to distinguish them.

    Reviewer follow-up (still honored): this always hands over the
    announce-then-confirm recipe rather than staying silent about it — that
    is safe precisely because the message never claims the user already
    answered "no." (An agent that actually got a real "no" from the user
    simply doesn't call strata_bind again — nothing here can stop that.)

    Live-test follow-up (two-step switch enforcement): the ``detail`` names
    the pending window explicitly — a bare ``confirm=True`` is honored only
    on a FOLLOW-UP call naming this SAME target within that window (see
    ``_PENDING_SWITCH`` and its docstring), never on the call that first
    announces the switch. The wording says so, so an agent reads this as
    "announce, then confirm on a second call," not as "just add
    confirm=True."
    """
    minutes = int(_PENDING_SWITCH_WINDOW_SECONDS // 60)
    detail = (
        "Ask the user to confirm the switch, then call "
        f"strata_bind(scope_id={requested_scope_id!r}, confirm=True) with "
        f"their answer within {minutes} minutes — do not confirm this "
        "yourself, and do not pass confirm=True on this same call; it is "
        "only honored on a follow-up call naming this same target."
    )
    return {
        "scope_id": _AGENT_SCOPE,
        "skill": _AGENT_SKILL,
        "session_id": _AGENT_SESSION_ID,
        "switch_pending": True,
        "message": (
            f"This session is bound to {previous_scope_id!r}; switching to "
            f"{requested_scope_id!r} changes whose memory it reads and writes. "
            f"{detail}"
        ),
    }


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="strata",
    instructions=(
        "Tools for reading from and contributing to the Strata fleet memory. "
        "Use strata_read_perspective before acting, contribute observations as "
        "context, and contribute binding decisions as directives only when warranted. "
        "Before finishing, contribute the session's outcomes back with "
        "strata_contribute; if there is genuinely nothing to record, call "
        "strata_session_closeout so an empty session stays distinguishable from a "
        "forgotten one. Binding decides whose memory this session reads and "
        "writes — never pick or change the bound scope on your own judgment. If "
        "a scope you need was just created, or this session should act as a "
        "different scope, ask the user which scope it should be, then call "
        "strata_bind with their answer — fleet.yaml is re-read as part of that "
        "call, so a scope added moments ago is bindable immediately, no restart "
        "required."
    ),
)


# ---------------------------------------------------------------------------
# Tool: strata_bind
#
# Feature B: a session created a scope (or fleet.yaml was edited for some
# other reason) and wants to work as that scope, without restarting the MCP
# server — today's binding is otherwise fixed at process startup
# (_validate_binding, above). This is the operator-facing incident this
# feature exists to close.
# ---------------------------------------------------------------------------


@mcp.tool()
async def strata_bind(scope_id: str, skill: str | None = None, confirm: bool = False) -> dict:
    """Bind (or rebind) this session to a scope (and optionally skill), right now.

    THE SCOPE MUST COME FROM THE USER. Binding decides whose memory this
    session reads and writes — never call this with a scope you (the agent)
    picked on your own judgment, even to "just answer one question." Ask
    the user which scope this session should act as, then call this tool
    with their answer.

    Startup binds this session to ``STRATA_AGENT_SCOPE``/``STRATA_AGENT_SKILL``
    once. This tool changes that binding mid-session — for the common case
    where a scope was just added to fleet.yaml (by this session, an operator,
    or another process) and isn't visible yet under the stale startup
    binding, or where the user wants this session to act as a different
    scope from here on.

    Reloads fleet.yaml first (the same lazy reload-on-read path every other
    tool call uses — Feature A), so a scope added moments ago is bindable
    immediately, no server restart required. Validates exactly like startup
    binding: the scope must exist and be active, and the skill (if given)
    must be permitted for it — reusing the same checks startup binding runs,
    so a binding accepted here is a binding accepted anywhere.

    Your session id (``STRATA_AGENT_SESSION_ID``) NEVER changes — this is
    the same session working as a different scope, not a new session.
    Contributions already record their own scope/skill/session provenance
    per call, so the record stays truthful regardless of how many times a
    session rebinds.

    SWITCHING requires an explicit ANNOUNCE-THEN-CONFIRM round trip
    (self-bind guard). Binding for the FIRST time this session (currently
    unbound) needs no confirmation — there is no prior identity to lose.
    But once this session is ALREADY bound to a scope, a call naming a
    DIFFERENT scope is a SWITCH, and a switch is never performed on the
    call that first names it — not even if that call already carries
    ``confirm=True``. ``confirm=True`` is a promise from whoever is
    calling, not proof the user actually answered, so it is honored ONLY on
    a FOLLOW-UP call that names the SAME target scope again, within a few
    minutes of the call that first announced it. The first call always
    returns ``switch_pending: True`` with the binding UNCHANGED, so hand
    that to the user, get their answer, and call again — with
    ``confirm=True`` and the SAME ``scope_id`` — once they've confirmed.
    Naming a different target, or waiting too long, restarts the
    announcement (another ``switch_pending`` result, not a switch). (A call
    naming the SAME scope you are already bound to is a no-op success,
    never a switch — only pass ``confirm`` when you actually mean to change
    identity.) Where the client supports server-initiated MCP elicitation,
    this tool asks the user to confirm the switch itself, in the SAME call
    that first names it — that one round trip replaces the announce-then-
    confirm dance entirely, ONLY when the response is an explicit accept
    with ``confirm=True``. Anything else the elicitation comes back with
    (a decline, a cancel, a timeout, or an accept with ``confirm=False``)
    is never treated as the user's answer — a protocol-level decline can
    come back with no dialog ever shown to a human — so it falls straight
    through to the SAME announce-then-confirm ``switch_pending`` result an
    unconfirmed call gets, binding left unchanged, ready for a genuine
    follow-up call.

    On failure — an invalid scope/skill, or a switch pending confirmation —
    the binding is left completely unchanged.

    Clears ONLY the binding-class startup failures (which scope/skill this
    session acts as) — never a config-class one (a broken/missing
    ``.strata/config.toml`` or ``fleet.yaml``; see ``_validate_binding``'s
    docstring for why: those mean the server may be running against the
    WRONG storage source, and no scope pick fixes that). If a config-class
    failure remains after an otherwise-successful bind, the result carries a
    ``config_notice`` explaining that memory tools stay gated regardless —
    review follow-up: strata_bind used to clear the WHOLE startup-failure
    list unconditionally, so a "successful" bind after a broken config.toml
    would silently look fully resolved while still writing to the wrong
    (env-fallback) store.

    Args:
        scope_id: The scope to bind to — the user's choice, never your own.
            Must exist in fleet.yaml and be ``active`` (an archived scope
            cannot be bound).
        skill: The skill to bind as, or omit it. When omitted and the scope
            declares a ``default_skill``, that default is used (the same
            companion rule startup auto-bind applies) — required only when
            the scope declares skills (``default_skill`` or
            ``permitted_skills``) and has no ``default_skill`` to fall back
            on. When given, must be one of the scope's ``permitted_skills``
            (any skill is allowed if that list is empty).
        confirm: Only meaningful when this session is already bound to a
            DIFFERENT scope than *scope_id* — pass ``True`` once the user
            has confirmed the switch, on a FOLLOW-UP call naming the same
            *scope_id* you already announced. Has NO effect on the call
            that first names a new target (that call always comes back as
            ``switch_pending`` regardless of this flag) — it only performs
            the switch when it matches an announcement from an earlier
            call, within a few minutes. Ignored entirely for an initial
            bind or a same-scope re-bind. Never set this to ``True`` on
            your own judgment; it must reflect the user's actual answer,
            given after you told them about the switch.

    Returns:
        On success: ``scope_id``, ``skill``, ``session_id`` (the new
        binding — session_id unchanged), and ``message`` (a one-line
        confirmation, noting the identity change when this was a switch).
        On an unconfirmed switch: the binding UNCHANGED — same ``scope_id``/
        ``skill`` as before this call — plus ``switch_pending: True`` and a
        ``message`` explaining what to do next (ask the user, then call
        again with ``confirm=True``). This is the result for EVERY reason a
        switch didn't happen yet — no confirmation offered, an elicitation
        that came back anything other than an explicit accept, or a client
        that can't be asked at all — never a claim that the user already
        said no (a protocol-level elicitation decline is indistinguishable
        from a real one, so this tool never attributes it to the user).

    Raises:
        RuntimeError: The scope does not exist, is archived, or the skill is
            not permitted — the error lists the valid scopes/skills. The
            binding is left unchanged in every failure case.
    """
    global _AGENT_SCOPE, _AGENT_SKILL, _UNRESOLVED, _STARTUP_ERRORS_BINDING
    global _ELICIT_UNAVAILABLE, _PENDING_SWITCH

    # This is THE recovery path for an unresolved session (Change 1), so it
    # deliberately re-reads fleet.yaml itself (below) rather than trusting
    # any cached startup fleet — a fleet fixed after startup must be
    # bindable without a restart. It never calls _require_bound_or_elicit():
    # eliciting from inside the tool that IS the elicitation's own bind
    # target would be circular.
    fleet = _load_fleet()

    # require_active=True is the shared "archived scopes cannot be bound"
    # rule — the same check startup binding runs (_validate_binding, above),
    # not a parallel reimplementation of it. _resolve_bind is the same
    # "is this a valid rebind" rule the elicitation fallback (Change 2)
    # applies to an accepted scope pick.
    resolved_skill, errors = _resolve_bind(fleet, scope_id, skill)

    if errors:
        # Feature A: the exact incident this tool exists to close is a
        # fleet.yaml edit that was invalid — silently kept serving the last
        # good fleet — followed by a bind attempt for the scope that edit
        # meant to add. Without the reload notice here, the refusal reads as
        # "add the scope you just added"; with it, it explains WHY the scope
        # is still invisible.
        notice = _fleet_notice()
        notice_line = f"\n(fleet reload warning: {notice})" if notice else ""
        raise RuntimeError(
            "strata_bind refused — binding unchanged (still "
            f"scope={_AGENT_SCOPE!r}, skill={_AGENT_SKILL!r}):\n"
            + "\n".join(f"- {e}" for e in errors)
            + notice_line
        )

    # Self-bind guard (operator finding: an agent picked a scope on its own
    # judgment, without the user ever weighing in). Binding for the first
    # time this session needs no confirmation — there is no prior identity
    # to lose. Naming the SAME scope again is a no-op, never a switch. Only
    # naming a DIFFERENT scope while ALREADY BOUND AND RESOLVED is a switch,
    # and a switch requires the user's explicit say-so — either via an
    # accepted elicitation (asked right here, right now) or a second call
    # carrying confirm=True (the fallback for a client that can't be asked).
    #
    # Gated on `not _UNRESOLVED` (reviewer follow-up — recovery friction):
    # a startup failure can leave _AGENT_SCOPE holding an INVALID id (e.g.
    # an unknown scope named in STRATA_AGENT_SCOPE) with the session still
    # gated. That stale, never-actually-bound value is not an "identity to
    # protect" — there is nothing legitimate to lose by moving off of it —
    # so a user-directed recovery bind to a valid scope must complete in
    # ONE call, not be treated as a switch away from a binding that was
    # never real to begin with.
    previous_scope_id = _AGENT_SCOPE
    is_switch = not _UNRESOLVED and bool(previous_scope_id) and scope_id != previous_scope_id

    if is_switch:
        # This await deliberately happens OUTSIDE _binding_lock. A
        # threading.Lock held across an `await` in this single-threaded
        # asyncio server would risk a same-thread deadlock: a concurrent,
        # synchronous lock.acquire() from another coroutine blocks the ONLY
        # thread that could ever run this suspended coroutine again to
        # release it. So the lock is only ever held for the synchronous
        # pending-record and binding writes below, never spanning this
        # call — leaving a narrow interleaving window, between this
        # elicitation returning and the lock acquisition below, where a
        # concurrent strata_bind call could read/write _PENDING_SWITCH
        # first. Accepted as a narrow race on a server that in practice
        # serves one request at a time; not solved here.
        #
        # Skipped entirely when confirm=True — this is the announce-then-
        # confirm follow-up call, not a fresh ask.
        #
        # Live Codex-replay finding (bug fix): _attempt_elicit_switch_confirm
        # returns True ONLY for an explicit accept — everything else (no
        # session, no capability, a protocol-level decline/cancel, a
        # timeout, a transport error, or even an accept carrying
        # confirm=False) returns False and falls straight through to the
        # announce-then-confirm two-step below, uniformly. A protocol-level
        # decline is indistinguishable from a real human "no," so it is
        # never treated as one here — see that function's docstring.
        switched_via_elicitation = not confirm and await _attempt_elicit_switch_confirm(
            previous_scope_id, scope_id
        )

        if not switched_via_elicitation:
            # Two-step enforcement (live-test finding): a bare confirm=True
            # is a PROMISE from whoever is calling, not a GATE — an agent
            # could self-supply it on the very first call, indistinguishable
            # from a genuinely user-confirmed one. confirm=True is honored
            # ONLY when this exact target was already announced, on an
            # EARLIER call, and this call names it again within the pending
            # window. No matching pending record (never announced, a
            # different target, or the window elapsed) means this IS the
            # announcing call, confirm=True or not — it does not switch.
            #
            # Read-then-write of _PENDING_SWITCH under _binding_lock (moved
            # here for symmetry with every other binding-state write below,
            # and so the check-and-set is one atomic step, not two).
            with _binding_lock:
                now = time.monotonic()
                pending = _PENDING_SWITCH
                pending_confirmed = (
                    confirm
                    and pending is not None
                    and pending.target_scope_id == scope_id
                    and now - pending.requested_at <= _PENDING_SWITCH_WINDOW_SECONDS
                )
                if not pending_confirmed:
                    _PENDING_SWITCH = _PendingSwitch(target_scope_id=scope_id, requested_at=now)
                    return _switch_pending_result(previous_scope_id, scope_id)
        # Confirmed — either an accepted elicitation or a matching,
        # unexpired announce-then-confirm pair. The pending record (if any)
        # is cleared below, along with every other successful-bind path.

    with _binding_lock:
        _AGENT_SCOPE = scope_id
        _AGENT_SKILL = resolved_skill
        # Clears ONLY the binding-class failures — see the docstring above
        # and _validate_binding's for why a config-class one (if present)
        # is deliberately left untouched here.
        _STARTUP_ERRORS_BINDING = []
        _UNRESOLVED = bool(_STARTUP_ERRORS_CONFIG)
        # A fresh binding clears the elicitation-unavailable memo too — see
        # its docstring (module top): a client marked non-functional once
        # isn't locked out of elicitation forever by that.
        _ELICIT_UNAVAILABLE = False
        # Tidiness (reviewer hardening): clear any pending switch on ANY
        # successful bind — initial, same-scope no-op, confirmed switch, or
        # an elicitation-accepted switch. An announced target shouldn't
        # stay confirmable after unrelated bind activity moved the session
        # on.
        _PENDING_SWITCH = None

    if is_switch:
        message = (
            f"Switched from {previous_scope_id!r} to {_AGENT_SCOPE!r}"
            f"{f' with skill {_AGENT_SKILL!r}' if _AGENT_SKILL else ''}. "
            f"This session now reads and writes {_AGENT_SCOPE!r}'s memory, not "
            f"{previous_scope_id!r}'s. Session id unchanged ({_AGENT_SESSION_ID!r})."
        )
    else:
        message = (
            f"Bound to {_AGENT_SCOPE!r}"
            f"{f' with skill {_AGENT_SKILL!r}' if _AGENT_SKILL else ''}. "
            f"Session id unchanged ({_AGENT_SESSION_ID!r})."
        )

    result = {
        "scope_id": _AGENT_SCOPE,
        "skill": _AGENT_SKILL,
        "session_id": _AGENT_SESSION_ID,
        "message": message,
    }
    if _STARTUP_ERRORS_CONFIG:
        result["config_notice"] = (
            "Binding succeeded, but memory tools remain gated: "
            f"{len(_STARTUP_ERRORS_CONFIG)} unresolved config/storage-source "
            "failure(s) (the server may be running against the wrong fleet.yaml "
            "or database). Fix the file(s) and restart the server — strata_bind "
            "cannot clear these."
        )
    return _attach_fleet_notice(result)


# ---------------------------------------------------------------------------
# Tool: strata_contribute
# ---------------------------------------------------------------------------


@mcp.tool()
async def strata_contribute(
    scope_id: str,
    content: str,
    proposed_classification: Literal["directive", "context"],
    subject: str | None = None,
    supersedes: str | None = None,
) -> dict:
    """Submit a contribution to a scope's scope-manager for judgment.

    This is how a session gives back what it learned: before finishing, record
    the session's outcomes here so the fleet's memory reflects what actually
    happened. If there is genuinely nothing to record, call
    ``strata_session_closeout`` instead — leaving a session that consumed memory
    silent lets that memory quietly go stale.

    A contribution is a proposal — not a direct write.  The scope-manager
    judges it and decides whether to accept it as a directive (binding for the
    scope and all descendants), accept it as context (non-binding knowledge),
    or decline it.  The proposed_classification is a hint; the scope-manager
    may re-classify in either direction.

    The contributor provenance block (scope, skill, session_id, ts) is
    populated automatically from the agent's environment variables:
    STRATA_AGENT_SCOPE, STRATA_AGENT_SKILL, STRATA_AGENT_SESSION_ID.

    Write surface: ``scope_id`` must be this agent's bound scope
    (``STRATA_AGENT_SCOPE``) or one of its inter-stratum ancestors — the same
    surface shape as the entitled read surface. Contribute to
    your own scope, or propose upward to an ancestor scope; that is the
    mechanism of legitimate upward influence. A peer or descendant scope is
    refused before any judging or recording happens — sideways flow reaches
    other scopes only via ratification through a shared ancestor, or a
    context-only reference edge, never a direct write.

    Args:
        scope_id: Target scope to contribute to (e.g. ``g_arch``). Must be
            the bound scope or one of its inter-stratum ancestors.
        content: The memory content being proposed.
        proposed_classification: Hint to the scope-manager — ``directive``
            for a binding decision, ``context`` for an observation or
            non-binding knowledge.
        subject: Optional short label for this contribution (e.g.
            ``rpc-protocol``), used for supersession matching.
        supersedes: Optional ID of a prior directive this contribution
            replaces (supersession pattern).

    Returns:
        ``contribution_id`` and ``judgment`` (decision, reasoning, summary_updated).

    Raises:
        RuntimeError: If the scope is not found, is archived, or is outside
            this agent's entitled write surface.
    """
    await _require_bound_or_elicit()

    # Snapshot the binding ONCE, into locals, and use these — never the
    # _AGENT_* globals directly — for both the authorization check below and
    # the provenance stamp on ContributorRef. Without this, a strata_bind
    # rebind landing between the two (a future async server, a concurrent
    # request) could authorize against one scope and stamp the contribution
    # as a different one.
    agent_scope, agent_skill, agent_session_id = _AGENT_SCOPE, _AGENT_SKILL, _AGENT_SESSION_ID

    fleet = _load_fleet()

    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise RuntimeError(f"Scope not found: {scope_id!r}")
    if scope.status == "archived":
        raise RuntimeError(f"Scope is archived and not accepting contributions: {scope_id!r}")
    _check_entitled_write(
        fleet, agent_scope, scope_id, agent_skill=agent_skill, agent_session_id=agent_session_id
    )

    stratum = next((s for s in fleet.strata if s.id == scope.stratum_id), None)
    if stratum is None:
        raise RuntimeError(
            f"Stratum {scope.stratum_id!r} for scope {scope_id!r} not found in fleet config."
        )

    ts = datetime.now(UTC).isoformat()
    contributor = ContributorRef(
        scope_id=agent_scope,
        skill=agent_skill,
        session_id=agent_session_id,
        ts=ts,
    )

    # The record-append -> read-summary -> judge -> record-judgment ->
    # summary-write sequence runs through the shared choke point in strata.app
    # under the per-scope serialization lock (issue #38), so two concurrent
    # contributions to the same scope can never leave the summary
    # unexplainable by the record. Imported lazily (like the scope-manager) to
    # keep the import path light until a contribution actually happens.
    from strata.app import JudgeUnavailable, run_contribution  # noqa: PLC0415

    manager = _build_scope_manager()

    try:
        outcome = run_contribution(
            scope=scope,
            stratum=stratum,
            content=content,
            proposed_classification=proposed_classification,
            subject=subject,
            supersedes=supersedes,
            contributor=contributor,
            fleet=fleet,
            record_store=_record_store,
            summary_store=_summary_store,
            scope_manager=manager,
            summary_max_words=_settings.summary_max_words,
            window_verbatim_tail=_settings.window_verbatim_tail,
            recency_window_size=_settings.recency_window_size,
            batch_cap=_settings.judgment_batch_cap,
        )
    except JudgeUnavailable as exc:
        # The contribution and a judgment-attempt-failed event are already in
        # the record (issue #57); a verdict is never fabricated. Surface the
        # contribution id and route the retry to strata_rejudge — calling
        # strata_contribute again would duplicate the contribution.
        raise RuntimeError(
            f"Scope-manager judgment failed ({exc.error_class}): {exc}. "
            f"The contribution is recorded as {exc.contribution_id} with a "
            "judgment-attempt-failed event but has no verdict yet. Retry with "
            f"strata_rejudge(contribution_id={exc.contribution_id!r}) — do NOT "
            "call strata_contribute again, which would duplicate it."
        ) from exc

    # Asymmetry release valve (#110): an accepted contribution resets the
    # read/contribute gap for this session; a decline does not.
    _record_accepted_contribution(outcome.decision)

    return _attach_fleet_notice(
        {
            "contribution_id": outcome.contribution_id,
            "judgment": {
                "decision": outcome.decision,
                "reasoning": outcome.reasoning,
                "summary_updated": outcome.summary_updated,
            },
        }
    )


# ---------------------------------------------------------------------------
# Tool: strata_rejudge
# ---------------------------------------------------------------------------


@mcp.tool()
async def strata_rejudge(contribution_id: str) -> dict:
    """Re-judge a contribution whose scope-manager judgment previously failed.

    Idempotent: if the contribution already has a verdict, this is
    a no-op that returns that verdict unchanged. Otherwise it re-reads the
    scope's CURRENT summary, invokes the scope-manager, records the judgment,
    and updates the summary — all under the scope's serialization lock, so a
    re-judge never races a concurrent contribution.

    Use this to recover a contribution left pending by a judge() failure (API
    outage, malformed model output): the failing strata_contribute response
    carries the contribution id and names this tool as the retry path. Calling
    strata_contribute again instead would duplicate the contribution. A verdict
    is an exercise of scope authority — re-judge invokes the scope-manager, it
    never fabricates one, and a failed attempt is recorded as an event, never a
    verdict.

    Write surface: re-judging exercises the scope's authority just as
    contributing does, so it is gated by the same entitled write surface
    (ADR 0006 D1) — the contribution's scope must be your bound scope or one of
    its inter-stratum ancestors.

    Args:
        contribution_id: The id returned by the failed strata_contribute call.

    Returns:
        ``contribution_id`` and ``judgment`` (decision, reasoning,
        summary_updated). ``summary_updated`` is False for the idempotent
        no-op (a verdict already existed).

    Raises:
        RuntimeError: If the contribution is unknown, its scope is outside this
            agent's entitled write surface, or the scope-manager fails again
            (the contribution stays pending; a fresh judgment-attempt-failed
            event is recorded and you may re-judge again later).
    """
    await _require_bound_or_elicit()

    fleet = _load_fleet()

    contribution = _record_store.get_contribution(contribution_id)
    if contribution is None:
        raise RuntimeError(f"Contribution not found: {contribution_id!r}")
    _check_entitled_write(
        fleet,
        _AGENT_SCOPE,
        contribution.scope_id,
        agent_skill=_AGENT_SKILL,
        agent_session_id=_AGENT_SESSION_ID,
    )

    from strata.app import JudgeUnavailable, rejudge_contribution  # noqa: PLC0415

    manager = _build_scope_manager()

    # Whether a verdict already existed (issue #57 idempotency). Captured before
    # re-judging so the asymmetry counter (#110) only counts the verdict once:
    # a re-judge that merely replays an existing verdict must not re-fire the
    # release valve for a contribution the original strata_contribute already
    # counted.
    already_judged = _record_store.get_judgment(contribution_id) is not None

    try:
        outcome = rejudge_contribution(
            contribution_id,
            fleet=fleet,
            record_store=_record_store,
            summary_store=_summary_store,
            scope_manager=manager,
            summary_max_words=_settings.summary_max_words,
            window_verbatim_tail=_settings.window_verbatim_tail,
            recency_window_size=_settings.recency_window_size,
        )
    except JudgeUnavailable as exc:
        raise RuntimeError(
            f"Scope-manager judgment failed again ({exc.error_class}): {exc}. "
            f"Contribution {exc.contribution_id} stays pending with a fresh "
            "judgment-attempt-failed event; call strata_rejudge again once the "
            "scope-manager is available."
        ) from exc

    # Asymmetry release valve (#110): count only a verdict this re-judge is the
    # first to record — the recovery of a previously-failed contribution.
    if not already_judged:
        _record_accepted_contribution(outcome.decision)

    return _attach_fleet_notice(
        {
            "contribution_id": outcome.contribution_id,
            "judgment": {
                "decision": outcome.decision,
                "reasoning": outcome.reasoning,
                "summary_updated": outcome.summary_updated,
            },
        }
    )


# ---------------------------------------------------------------------------
# Tool: strata_publish
#
# ADR 0007 D2 — publishing is a judged act, distinct from internal
# acceptance. NO scope_id parameter: own-scope-only publishing is
# STRUCTURAL, not a check that could fail — there is no publishing upward or
# sideways (that is what ratification and the entitled write surface are
# for), so the tool simply never accepts a target other than the bound
# scope.
# ---------------------------------------------------------------------------


@mcp.tool()
async def strata_publish(
    content: str,
    kind: Literal["directive", "context"],
    anchors: list[str],
    subject: str | None = None,
) -> dict:
    """Propose publishing content from this agent's bound scope's own memory.

    A publish call is a PROPOSAL, not a direct write — it mirrors
    ``strata_contribute``'s shape: the scope-manager judges it, distinctly
    from ordinary contribution judging, because "true and useful for us" is
    not "ready for others to act on" (CONTEXT.md § Publication; ADR 0007
    D2). The judge enforces **published ⊆ believed**: content absent from or
    contradicted by this scope's current summary is declined, including a
    plausible-sounding extension of what the summary actually says — do not
    round up.

    There is no publishing upward or sideways. This tool always acts on your
    bound scope (``STRATA_AGENT_SCOPE``) — publishing *for* another scope
    would exercise authority you do not hold; upward influence remains
    contribution + ratification, unaffected by this channel.

    Every publish call must carry at least one ANCHOR: either the id of a
    directive currently in this scope's summary, or a free-form subject
    string. Anchors are validated STRUCTURALLY, before judging — zero
    anchors, or an anchor explicitly tagged ``directive:<id>`` naming an id
    not currently in this scope's summary, is refused outright (an error,
    not a scope-manager decline; nothing is recorded).

    Args:
        content: The outward wording, published verbatim if accepted — never
            rewritten by the scope-manager (ADR 0007 D1).
        kind: ``'directive'`` or ``'context'`` as this content stands in your
            OWN scope's memory. Purely informative to readers — every
            published item is non-binding to them regardless (ADR 0007 D1).
        anchors: At least one anchor — a directive id from this scope's
            current summary, or a subject string.
        subject: Optional short label.

    Returns:
        ``act_id`` and ``judgment`` (``decision``: ``"accept"``/``"decline"``,
        ``reasoning``, ``artifact_updated``).

    Raises:
        RuntimeError: The bound scope is unknown, or the anchors fail
            structural validation.
    """
    await _require_bound_or_elicit()

    # Snapshot the binding ONCE — see strata_contribute for why (authorize
    # and stamp must use the identical value, never two separate global reads).
    agent_scope, agent_skill, agent_session_id = _AGENT_SCOPE, _AGENT_SKILL, _AGENT_SESSION_ID

    fleet = _load_fleet()

    scope = fleet.get_scope(agent_scope)
    if scope is None:
        raise RuntimeError(f"Your bound scope {agent_scope!r} was not found in the fleet config.")

    ts = datetime.now(UTC).isoformat()
    proposer = ContributorRef(
        scope_id=agent_scope,
        skill=agent_skill,
        session_id=agent_session_id,
        ts=ts,
    )

    manager = _build_scope_manager()

    try:
        outcome = propose_publish(
            agent_scope,
            content,
            kind,
            subject,
            anchors,
            proposer,
            fleet=fleet,
            record_store=_record_store,
            summary_store=_summary_store,
            scope_manager=manager,
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    return _attach_fleet_notice(
        {
            "act_id": outcome.act_id,
            "judgment": {
                "decision": outcome.decision,
                "reasoning": outcome.reasoning,
                "artifact_updated": outcome.artifact_updated,
            },
        }
    )


# ---------------------------------------------------------------------------
# Tool: strata_withdraw
# ---------------------------------------------------------------------------


@mcp.tool()
async def strata_withdraw(item_id: str) -> dict:
    """Propose withdrawing a published item from this agent's bound scope's publication.

    Same proposal-not-write shape as ``strata_publish`` — the scope-manager
    judges the withdrawal (ADR 0007 D2). Withdrawal is effective immediately
    for readers once accepted: composition is read-time (ADR 0004), so a
    withdrawn item is gone from every subsequent reader's perspective, with
    no cache to invalidate.

    This tool always acts on your bound scope's own publication — there is
    no withdrawing from another scope's outward face.

    Args:
        item_id: The ``pub_``-prefixed id of the published item to withdraw
            (from this scope's current publication).

    Returns:
        ``act_id`` and ``judgment`` (``decision``: ``"accept"``/``"decline"``,
        ``reasoning``, ``artifact_updated``).

    Raises:
        RuntimeError: The bound scope is unknown, or *item_id* is not in this
            scope's current publication.
    """
    await _require_bound_or_elicit()

    # Snapshot the binding ONCE — see strata_contribute for why.
    agent_scope, agent_skill, agent_session_id = _AGENT_SCOPE, _AGENT_SKILL, _AGENT_SESSION_ID

    fleet = _load_fleet()

    scope = fleet.get_scope(agent_scope)
    if scope is None:
        raise RuntimeError(f"Your bound scope {agent_scope!r} was not found in the fleet config.")

    ts = datetime.now(UTC).isoformat()
    proposer = ContributorRef(
        scope_id=agent_scope,
        skill=agent_skill,
        session_id=agent_session_id,
        ts=ts,
    )

    manager = _build_scope_manager()

    try:
        outcome = propose_withdraw(
            agent_scope,
            item_id,
            proposer,
            fleet=fleet,
            record_store=_record_store,
            summary_store=_summary_store,
            scope_manager=manager,
        )
    except (ValueError, KeyError) as exc:
        raise RuntimeError(str(exc)) from exc

    return _attach_fleet_notice(
        {
            "act_id": outcome.act_id,
            "judgment": {
                "decision": outcome.decision,
                "reasoning": outcome.reasoning,
                "artifact_updated": outcome.artifact_updated,
            },
        }
    )


# ---------------------------------------------------------------------------
# Tool: strata_read_scope_summary
# ---------------------------------------------------------------------------


@mcp.tool()
async def strata_read_scope_summary(scope_id: str | None = None) -> dict:
    """Return the scope summary for the given scope — or a referenced scope's publication.

    For your own bound scope or an inter-stratum ancestor, this returns the
    scope summary: the curated, condensed working view of a scope, maintained
    by its scope-manager, with two sections — directives (binding decisions
    that propagate to all descendant scopes) and context (non-binding
    observations and knowledge).

    For a chain-REFERENCED scope (ADR 0007 D4: the ADR 0006 D3 amendment),
    this returns that scope's **publication** instead — its curated, judged
    outward face — never its internal summary. The entitled content across a
    reference was always "its outward face" (CONTEXT.md § Reference edge:
    "what a reference edge delivers is the referenced scope's publication —
    never its full internal summary"); the face just became a real, judged
    artifact rather than the whole internal summary.

    Args:
        scope_id: The scope whose summary (or publication) to read (e.g.
            ``g_arch``). Defaults to this agent's bound scope. An explicit
            scope_id must be within this agent's entitled *context* surface
            (ADR 0006 D3/D4): the bound scope, one of its inter-stratum
            ancestors, or a scope referenced by a scope on that chain via a
            reference edge. Unreferenced scopes and descendants are not
            directly readable.

    Returns:
        For own scope / ancestor: parsed scope summary — ``scope_id``,
        ``directives``, ``context``, ``updated_at``, ``version``, ``exists``.
        If the scope has no summary on disk yet, a synthesized empty summary
        is returned with ``version=0`` and ``exists=False`` — distinguishable
        from a real first write (``version=1``, ``exists=True``); see
        :class:`strata.summary_store.ScopeSummary`.

        For a chain-referenced scope: ``{"scope_id": ..., "relation":
        "peer_reference", "publication": {"items": [<item dicts: id, kind,
        content, subject, anchors, published_at>]}}``. A referenced scope
        that has published nothing returns ``{"items": []}`` — the honestly
        empty face.

    Raises:
        RuntimeError: If the scope does not exist, or if scope_id is outside
            this agent's entitled context surface.
    """
    await _require_bound_or_elicit()

    fleet = _load_fleet()

    if scope_id is None:
        scope_id = _AGENT_SCOPE
    _check_entitled_context(fleet, _AGENT_SCOPE, scope_id)

    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise RuntimeError(f"Scope not found: {scope_id!r}")

    # Read receipt (#110): a summary read consumes this scope's memory — count it
    # toward the session asymmetry counters and the per-scope staleness metric.
    _record_read(scope_id)

    # ADR 0007 D4: a chain-referenced scope (not the bound scope, not an
    # ancestor) is entitled for its OUTWARD FACE, never its internal summary.
    chain_ids = {s.id for s in fleet.entitlement_view(_AGENT_SCOPE).chain}
    if scope_id not in chain_ids:
        items = read_publication(scope_id, summaries_dir=_summaries_dir)
        return _attach_nudge(
            {
                "scope_id": scope_id,
                "relation": "peer_reference",
                "publication": {"items": [_publication_item_dict(i) for i in items]},
            }
        )

    existing = _summary_store.read(scope_id)
    if existing is not None:
        return _attach_nudge(existing.model_dump())

    # Scope exists but has no summary yet — return a synthesized empty
    # summary. version=0 + exists=False mark it as synthesized so it's never
    # mistaken for a real first write (version=1, exists=True).
    empty = ScopeSummary(
        scope_id=scope_id,
        directives=[],
        context="",
        updated_at=datetime.now(tz=UTC).isoformat(),
        version=0,
        exists=False,
    )
    return _attach_nudge(empty.model_dump())


# ---------------------------------------------------------------------------
# Tool: strata_read_perspective
# ---------------------------------------------------------------------------


@mcp.tool()
async def strata_read_perspective(scope_id: str | None = None) -> dict:
    """Return this agent's perspective on the fleet's long-term memory.

    A perspective is a composed, provenance-preserving view of: the scope's
    own summary, all inter-stratum ancestor summaries up to the root, and —
    ADR 0006 D3, delivering the referenced scope's **publication** per the
    ADR 0007 D4 amendment — the outward face of any scopes referenced (one
    hop, via a reference edge) by a scope on that chain. Layers are ordered
    root-first: ancestors first, then the requested scope's own layer, then
    referenced-scope layers (sorted by scope id for deterministic ordering).

    Every layer carries ``relation`` (``"self"``, ``"ancestor"``, or
    ``"peer_reference"``) and ``binding`` (``True`` for self/ancestor layers,
    ``False`` for reference layers). Reference layers are **context only** —
    nothing in them binds the reader, whether the referenced scope sits on
    your own stratum, above it, or below it (ADR 0010 D2); each is labelled
    with that scope's own stratum. Self/ancestor layers carry that scope's
    full ``summary``; reference layers carry the referenced scope's CURRENT
    ``publication`` (``{"items": [...]}``, verbatim, never its internal
    summary — a scope that has published nothing gets an empty ``items``
    list, the honestly empty face). References-of-references are not
    traversed: only edges whose source scope is itself on the chain count
    (one hop, per ``FleetConfig.entitlement_view``).

    If a chain scope has no summary on disk yet, its layer is still included
    with empty directives and context so that the structure is visible; that
    layer's summary honestly reports ``version=0``/``exists=False`` rather
    than looking like a real first write.

    Args:
        scope_id: The scope for which to build the perspective. Defaults to
            this agent's bound scope. An explicit scope_id must be the bound
            scope or one of its inter-stratum ancestors — this is
            the perspective *target*, which stays chain-only (ADR 0006 D4):
            you compose a perspective for your own chain, not for a peer's.

    Returns:
        ``{layers: [{scope_id, stratum_id, relation, binding, summary |
        publication}], scope_id: <requested>, _layers_count: N}`` ordered
        root-first, then self, then sorted peer layers.

    Raises:
        RuntimeError: If the scope is unknown, or if scope_id is outside this
            agent's entitled (chain-only) surface.
    """
    await _require_bound_or_elicit()

    fleet = _load_fleet()

    if scope_id is None:
        scope_id = _AGENT_SCOPE
    _check_entitled(fleet, _AGENT_SCOPE, scope_id)

    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise RuntimeError(f"Scope not found: {scope_id!r}")

    # Read receipt (#110): a perspective read is attributed to its TARGET scope
    # (the scope whose perspective was requested), not fanned out to every
    # ancestor layer — "read this scope's perspective" is the metric's unit.
    _record_read(scope_id)

    # Composition (ordering, relation labelling, the synthesized-empty-
    # summary fallback) lives in strata.perspective — the importable library
    # primitive (issue #83A) — not here. This tool's job is entitlement plus
    # the scope-not-found error above; scope existence is already confirmed,
    # so compose_perspective's own ValueError never triggers.
    #
    # operator_reader (ADR 0008 D2): agents read operator layers through this
    # tool like any other layer — no separate operator-facing MCP surface
    # exists (agents are never the operator, ADR 0008 D1) — so the perspective
    # they compose is judge-consistent with what bound their scope at write
    # time.
    def _operator_reader(attachment_scope_id: str) -> list:
        return read_operator_layer(attachment_scope_id, summaries_dir=_summaries_dir)

    # publication_reader (ADR 0007 D4): this server ALWAYS wires it in — the
    # release that ships D4 also retires whole-face peer reads; there is no
    # legacy-shape agent surface left to preserve here.
    def _publication_reader(peer_scope_id: str) -> list:
        return read_publication(peer_scope_id, summaries_dir=_summaries_dir)

    return _attach_nudge(
        compose_perspective(
            scope_id,
            fleet=fleet,
            summary_store=_summary_store,
            operator_reader=_operator_reader,
            publication_reader=_publication_reader,
        )
    )


# ---------------------------------------------------------------------------
# Tool: strata_list_scopes
# ---------------------------------------------------------------------------


@mcp.tool()
def strata_list_scopes() -> dict:
    """Return the full fleet configuration: strata, scopes, and edges.

    Checks fleet.yaml for changes on every call and reloads if needed
    (ADR 0004 Decision 1; ADR 0002 addendum — lazy reload-on-read) so the
    agent always sees the current fleet topology, without re-parsing the
    file when nothing changed.

    Use this to understand the fleet's structure — which scopes exist, how
    they are arranged into strata, and which chain and reference edges
    connect them.

    Works even while this session is unbound (soft-start, Change 1): fleet
    topology is not scoped memory, so an agent helping the user pick a
    scope to bind to can still see what's available — unlike every other
    tool here, this one does NOT call _require_bound_or_elicit. While
    unbound, the response carries an additional ``unbound_notice`` so that
    state stays visible (_attach_unbound_notice) rather than looking like
    an ordinary, fully-bound call.

    Returns:
        Fleet config: ``strata`` (list), ``scopes`` (list), ``edges`` (list).
        Each edge carries ``kind`` — ``"chain"`` (binding; ``from_scope_id``
        is always the child) or ``"reference"`` (non-binding; ``from_scope_id``
        references ``to_scope_id``). ``unbound_notice`` is present only while
        this session has not yet been bound.
    """
    fleet = _load_fleet()

    active = fleet.active_scopes()
    active_ids = {s.id for s in active}
    active_edges = [e for e in fleet.edges if e.from_ in active_ids and e.to in active_ids]

    result = _attach_fleet_notice(
        {
            "strata": [s.model_dump() for s in fleet.strata],
            "scopes": [s.model_dump() for s in active],
            "edges": [
                {"from_scope_id": e.from_, "to_scope_id": e.to, "kind": e.kind}
                for e in active_edges
            ],
        }
    )
    return _attach_unbound_notice(result)


# ---------------------------------------------------------------------------
# Tool: strata_read_scope_record
# ---------------------------------------------------------------------------


@mcp.tool()
async def strata_read_scope_record(
    scope_id: str | None = None,
    limit: int | None = None,
    before_id: str | None = None,
) -> dict:
    """Return one page of a scope's immutable contribution record (forensic view).

    The record is the append-only log of every write ever accepted into the
    scope, including the scope-manager's judgment on each contribution.  Use
    this for debugging, accountability investigation, or understanding the
    history behind the current scope summary.

    BOUNDED BY DEFAULT: an unadorned call returns the NEWEST page,
    not the whole record. The record only ever grows, so a whole-scope read of
    a long-lived scope runs to megabytes and overflows the tool-result limit of
    the very agents this view exists for. Nothing is hidden — walk back through
    older pages with before_id until ``page.next_before_id`` is null.

    Two cheaper reads to prefer over walking this one:
      - To check ONE contribution — "did the contribution I submitted get
        judged, and what did the scope-manager say?" — call
        strata_read_contribution. It answers in a few hundred bytes and is the
        right tool after a strata_contribute call whose outcome you never saw.
      - To CONSUME memory rather than audit it, call strata_read_perspective.
        The record is forensic; the perspective is the working view.

    Migration note (entitlement supersedes the earlier HTTP-parity note): this
    tool used to skip fleet loading entirely and return an empty record for
    any unknown scope, mirroring the old HTTP ``GET /scopes/{id}/record``
    contract. Entitlement now takes precedence over that parity concern — the
    fleet is loaded on every call so the entitled-surface check can run.
    Reading the bound scope's own record while it has no rows yet still
    returns the empty record shape; a scope_id outside the entitled surface
    raises instead of silently returning an empty record.

    Stays chain-only even after ADR 0006 D3 widened scope summary reads to
    chain-referenced peers (D4): the record audits the authority that binds
    you, and a peer scope — however freely its summary composes into your
    perspective as context — only ever informs you, never binds you. Its own
    record is its own accountability surface, not yours.

    Args:
        scope_id: The scope whose record to read (e.g. ``g_backend``).
            Defaults to this agent's bound scope. An explicit scope_id must
            be the bound scope or one of its inter-stratum ancestors
            — a peer scope is not readable here even when it is
            referenced by your chain and its summary is otherwise readable
            (ADR 0006 D4).
        limit: Page size. Defaults to the configured record page size.
        before_id: Cursor for older pages — pass the previous response's
            ``page.next_before_id``, and stop when that is null. Paging is
            stable under concurrent contributions: the cursor anchors on a
            contribution, so a contribution appended mid-walk lands above the
            walk and can neither shift nor repeat a row already returned.

    Returns:
        ``contributions``, ``judgments``, ``judgment_attempts``, and
        ``contribution_states`` (lists) covering this page's contributions
        only, newest first, plus a ``page`` block carrying ``limit``,
        ``total`` (the whole record's size), and ``next_before_id`` (null on
        the last page).

    Raises:
        RuntimeError: If scope_id is outside this agent's entitled
            (chain-only) surface, or the paging arguments are out of range —
            including a before_id that is not a contribution in this scope.
    """
    await _require_bound_or_elicit()

    fleet = _load_fleet()

    if scope_id is None:
        scope_id = _AGENT_SCOPE
    _check_entitled(fleet, _AGENT_SCOPE, scope_id)

    try:
        page = _record_store.page_record(
            scope_id=scope_id,
            limit=limit if limit is not None else _settings.record_page_size,
            before_id=before_id,
        )
    except ValueError as exc:
        raise RuntimeError(f"Invalid record page: {exc}") from exc

    # The record is a forensic view, not memory consumption, so reading it does
    # not increment the read counter (#110). It still surfaces the nudge when the
    # session already crossed the threshold on its perspective/summary reads.
    return _attach_nudge(
        {
            "contributions": [asdict(c) for c in page.contributions],
            "judgments": [asdict(j) for j in page.judgments],
            # Failed-judgment events (issue #57): a contribution with attempts but
            # no judgment is pending, distinguishable in the forensic view.
            "judgment_attempts": [asdict(a) for a in page.judgment_attempts],
            # Per-contribution state (issue #118): judged / judge_failed /
            # pending, so "the judge errored" never reads as "still in flight".
            "contribution_states": [asdict(s) for s in page.contribution_states],
            # next_before_id is null once the record is exhausted — page until
            # then rather than inferring the end from a short page.
            "page": {
                "limit": page.limit,
                "total": page.total,
                "next_before_id": page.next_before_id,
            },
        }
    )


# ---------------------------------------------------------------------------
# Tool: strata_read_contribution (issue #130 — the by-id record read)
# ---------------------------------------------------------------------------


@mcp.tool()
async def strata_read_contribution(contribution_id: str) -> dict:
    """Return one contribution with its state, verdict, and judgment attempts.

    The cheap answer to "what happened to the contribution I submitted?" — the
    read to make when a strata_contribute call never returned its outcome (the
    client timed out, the session was interrupted): the contribution may well
    have landed and been judged, and calling strata_contribute again would
    duplicate it. This costs a few hundred bytes where the same question asked
    through strata_read_scope_record costs the scope's whole record.

    The three states are the ones the record distinguishes (issues #57, #118):
    ``judged`` (a verdict exists — ``judgment`` carries the decision and the
    scope-manager's notes), ``judge_failed`` (the judge was attempted and
    errored; nothing more happens without strata_rejudge), and ``pending``
    (judgment is in flight or was never attempted).

    Read surface: the contribution's own scope must be within this agent's
    entitled (chain-only) surface, exactly as strata_read_scope_record requires
    — a by-id lookup never reaches a record the scope read cannot.

    Args:
        contribution_id: The id returned by strata_contribute (``c_``-prefixed).

    Returns:
        ``contribution``, ``state`` (the derived state block), ``judgment``
        (null unless the state is ``judged``), and ``judgment_attempts``.

    Raises:
        RuntimeError: If the contribution is unknown, or its scope is outside
            this agent's entitled (chain-only) surface.
    """
    await _require_bound_or_elicit()

    fleet = _load_fleet()

    entry = _record_store.get_record_entry(contribution_id)
    if entry is None:
        raise RuntimeError(f"Contribution not found: {contribution_id!r}")
    _check_entitled(fleet, _AGENT_SCOPE, entry.contribution.scope_id)

    # Forensic, like the scope record read: no read counter increment (#110),
    # but the nudge still rides along.
    return _attach_nudge(
        {
            "contribution": asdict(entry.contribution),
            "state": asdict(entry.state),
            "judgment": asdict(entry.judgment) if entry.judgment is not None else None,
            "judgment_attempts": [asdict(a) for a in entry.judgment_attempts],
        }
    )


# ---------------------------------------------------------------------------
# Tool: strata_session_stats (issue #110 — the session's cheap self-query)
# ---------------------------------------------------------------------------


@mcp.tool()
async def strata_session_stats() -> dict:
    """Return this session's mechanical read/contribute asymmetry counters.

    A cheap, mechanical self-query: the MCP server tracks, per
    session, how many perspective/summary reads, accepted contribution acts, and
    explicit declines this session has performed. These counters never judge what
    is memory-worthy — the model always makes that call. They exist so a later
    nudge (#111) has something specific to say ("this session has read N
    perspectives and contributed nothing yet") and so a turn-boundary hook (#112)
    can read the same state cheaply.

    ``declines`` stays ``0`` until WP2 adds the ``strata_session_closeout`` tool;
    the field is present now so the shape is stable across work packages.

    Returns:
        ``session_id``, ``reads``, ``contributions``, ``declines``, and
        ``reads_by_scope`` (scope_id → ``{count, last_read_at}``). A session that
        has done nothing yet returns zeroed counters, never an error.
    """
    await _require_bound_or_elicit()

    if _session_store is not None:
        state = _session_store.read(_AGENT_SESSION_ID)
        if state is not None:
            return state.model_dump()
    return {
        "session_id": _AGENT_SESSION_ID,
        "reads": 0,
        "contributions": 0,
        "declines": 0,
        "reads_by_scope": {},
        "updated_at": "",
    }


# ---------------------------------------------------------------------------
# Tool: strata_session_closeout (issue #111 — the mechanical decline path)
# ---------------------------------------------------------------------------


@mcp.tool()
async def strata_session_closeout(reason: str) -> dict:
    """Record that this session has nothing to contribute — a MECHANICAL act.

    Call this before finishing when the session read from the fleet's memory but
    genuinely has no outcome to record. It is recorded exactly like a read
    receipt: NO scope-manager, NO judge call, NO admission decision, zero
    judge-token cost, and nothing enters any scope's memory. It only
    increments this session's mechanical ``declines`` counter — the asymmetry's
    release valve — which resets the read/contribute gap and silences the
    read-time nudge for the rest of the session.

    This exists so *forgot* and *nothing happened* stay distinguishable, and so
    being honest about an empty session is free: a real contribution pays for
    judgment, an honest "nothing to record" does not. Prefer ``strata_contribute``
    whenever the session produced anything worth remembering; reach for this only
    when it truly did not.

    Per-session and repeatable: each call records one decline (that is fine).

    Args:
        reason: A short, required statement of why there is nothing to record
            (e.g. "read-only investigation, no decisions made"). Requiring it
            keeps the decline a deliberate act rather than a reflex; it is not
            written into any scope's memory.

    Returns:
        This session's now-reset counters — ``session_id``, ``reads``,
        ``contributions``, ``declines`` (now incremented), ``reads_by_scope``,
        and ``updated_at`` — the same shape as ``strata_session_stats``.
    """
    await _require_bound_or_elicit()

    _logger.info("session %r closeout: %s", _AGENT_SESSION_ID, reason)
    state = _record_decline()
    if state is not None:
        return state.model_dump()
    # Store unavailable or the write failed (best-effort, like the read
    # receipts): report zeroed counters rather than failing the closeout.
    return {
        "session_id": _AGENT_SESSION_ID,
        "reads": 0,
        "contributions": 0,
        "declines": 0,
        "reads_by_scope": {},
        "updated_at": "",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the Strata MCP server.

    Startup order (issue #46 — nothing touches storage before validation):

    1. Resolve paths (project config walk-up; env fallback — reusing the ONE
       load_project_config() call below; see the note above the fallback
       branch for why a second call was a bug, not redundancy).
    2. Load the fleet config — parse/invariant failures become startup-error
       entries, not tracebacks.
    3. Validate the agent binding (scope, skill) — all failures aggregated
       and classified (config-class vs binding-class — see
       _validate_binding's docstring).
    4. Initialise storage (migrations, stores) — a failure here is still
       fatal (a corrupt DB or unwritable directory isn't something
       strata_bind can recover from) and exits 1 with a single actionable
       message.
    5. Serve — always. Soft-start (dated addendum, ADR 0005 D5): binding
       failures from step 3 (unbound multi-scope, unknown scope,
       impermissible skill, missing/invalid fleet) no longer exit here. The
       server completes the MCP handshake regardless, stores the aggregated,
       classified failure lists, and every memory tool but strata_bind
       returns the relevant one as its error result until the session is
       bound — see _require_bound_or_elicit(). A harness that swallows
       stderr (Codex, Claude Code) otherwise leaves the human never seeing
       why nothing works.
    """
    # Walk for the project config once at startup so we can show the user
    # exactly which paths we examined when validation fails.
    startup_errors: list[str] = []
    searched_paths_out: list[Path] = []
    project_config = None
    try:
        project_config = load_project_config(searched_paths_out=searched_paths_out)
    except ProjectConfigError as exc:
        startup_errors.append(
            f".strata/config.toml is invalid: {exc}\n"
            "  Fix the file (or delete it and re-run `strata register`), then restart "
            "the server — this is read once, at process start."
        )

    # Resolve storage paths from the project_config ALREADY loaded above,
    # never by calling resolve_storage_paths() (which would call
    # load_project_config() a second time, independently) — review
    # follow-up: an invalid .strata/config.toml made that second call raise
    # the SAME ProjectConfigError again, uncaught, crashing main() with a
    # raw traceback instead of degrading gracefully like every other
    # config-class failure. Mirrors resolve_storage_paths' own precedence
    # (project wins; env settings are the fallback) without re-deriving it.
    if project_config is not None:
        paths = StoragePaths(
            db_path=str(project_config.db),
            summaries_dir=str(project_config.summaries_dir),
            fleet_yaml_path=str(project_config.fleet_yaml),
            source="project",
            project_root=project_config.project_root,
        )
    else:
        paths = StoragePaths(
            db_path=_settings.db_path,
            summaries_dir=_settings.summaries_dir,
            fleet_yaml_path=_settings.fleet_yaml_path,
            source="env",
            project_root=None,
        )
    _set_paths(paths)

    # Load fleet only when we have a config; without one there's nothing to
    # validate against, and the loader would just hit env-var fallbacks.
    # Parse errors and invariant violations become config-class startup
    # errors, not tracebacks.
    fleet = None
    if project_config is not None:
        try:
            fleet = _load_fleet()
        except FleetConfigError as exc:
            startup_errors.append(
                f"fleet config at {paths.fleet_yaml_path} is invalid "
                f"[{exc.kind}]: {exc.message}\n"
                "  Fix fleet.yaml, then restart the server."
            )
        except yaml.YAMLError as exc:
            startup_errors.append(
                f"fleet config at {paths.fleet_yaml_path} is not valid YAML: {exc}\n"
                "  Fix fleet.yaml, then restart the server."
            )

    # Rebind the module globals to the resolved (possibly auto-bound) scope
    # and skill — every tool function below reads _AGENT_SCOPE / _AGENT_SKILL
    # directly, so the auto-bind decision must land here before mcp.run().
    global _AGENT_SCOPE, _AGENT_SKILL, _UNRESOLVED, _STARTUP_ERRORS_CONFIG, _STARTUP_ERRORS_BINDING
    (
        _AGENT_SCOPE,
        _AGENT_SKILL,
        _STARTUP_ERRORS_CONFIG,
        _STARTUP_ERRORS_BINDING,
    ) = _validate_binding(
        fleet,
        _AGENT_SCOPE,
        _AGENT_SKILL,
        project_config_found=project_config is not None,
        searched_paths=[str(p) for p in searched_paths_out],
        extra_errors=startup_errors,
    )
    _UNRESOLVED = bool(_STARTUP_ERRORS_CONFIG) or bool(_STARTUP_ERRORS_BINDING)

    # Storage init after validation — failures here (unwritable directory,
    # corrupt DB) also render as a refuse-to-start message, not a traceback.
    try:
        _init_stores()
    except (OSError, sqlite3.Error) as exc:
        print(
            "Strata MCP server refuses to start — storage initialisation failed:\n\n"
            f"[1] cannot initialise storage at db={paths.db_path!r}, "
            f"summaries={paths.summaries_dir!r}:\n"
            f"  {exc}\n"
            "  Check that the paths in .strata/config.toml exist and are writable.",
            file=sys.stderr,
        )
        sys.exit(1)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
