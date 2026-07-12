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
    Recorded in contribution provenance.  Required — server refuses to start
    if unset (ADR 0005 Decision 5).
STRATA_AGENT_SKILL
    The skill this agent is running (e.g. ``strata-developer``).
    Recorded in contribution provenance.  Required — server refuses to start
    if unset (ADR 0005 Decision 5).
STRATA_AGENT_SESSION_ID
    Unique identifier for this session.
    Recorded in contribution provenance.  Optional — defaults to a generated
    value when absent.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from mcp.server.fastmcp import FastMCP

from strata.fleet_config import FleetConfig, FleetConfigError
from strata.migrator import run_migrations
from strata.operator import read_operator_layer
from strata.perspective import compose_perspective
from strata.project_config import (
    ProjectConfigError,
    StoragePaths,
    load_project_config,
    resolve_storage_paths,
)
from strata.record_store import ContributorRef, RecordStore
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
_record_store: RecordStore | None = None
_summary_store: SummaryStore | None = None


def _set_paths(paths: StoragePaths) -> None:
    """Publish resolved storage paths to the module globals (no I/O)."""
    global _db_path, _summaries_dir, _fleet_yaml_path
    _db_path = paths.db_path
    _summaries_dir = paths.summaries_dir
    _fleet_yaml_path = paths.fleet_yaml_path


def _init_stores() -> None:
    """Initialise storage-backed singletons — called AFTER binding validation.

    Applies pending migrations so the DB is ready before the first tool call.
    """
    global _record_store, _summary_store
    run_migrations(_db_path)
    _record_store = RecordStore(_db_path)
    _summary_store = SummaryStore(_summaries_dir)


def _build_scope_manager():
    """Construct a :class:`ScopeManager` bound to the configured model.

    Imports anthropic + the scope-manager lazily (they pull in the Anthropic
    SDK, which may not be configured in every env) and are only needed when a
    contribution or re-judge actually invokes the judge.
    """
    import anthropic  # noqa: PLC0415

    from strata.scope_manager import ScopeManager  # noqa: PLC0415

    return ScopeManager(
        client=anthropic.Anthropic(api_key=_settings.anthropic_api_key),
        model=_settings.manager_model,
    )


# Agent provenance — recorded on every contribution.
# STRATA_AGENT_SCOPE and STRATA_AGENT_SKILL have no defaults;
# _validate_binding() enforces they are set before mcp.run().
# STRATA_AGENT_SESSION_ID is optional; generate one when absent.
_AGENT_SCOPE: str = os.environ.get("STRATA_AGENT_SCOPE", "")
_AGENT_SKILL: str = os.environ.get("STRATA_AGENT_SKILL", "")
_AGENT_SESSION_ID: str = os.environ.get("STRATA_AGENT_SESSION_ID", f"sess_{uuid.uuid4().hex[:8]}")

# ---------------------------------------------------------------------------
# Fleet config helper — re-read on every call that needs fleet info (ADR 0004
# Decision 1): no mtime watcher, no IPC. The 8 load-time invariants run on
# each read. Cheap: fleet.yaml is KB-range and parses fast.
# ---------------------------------------------------------------------------


def _load_fleet() -> FleetConfig:
    """Load and validate the fleet config from disk.

    Re-reads fleet.yaml on every call so the MCP server always sees the
    current config without IPC or a file-watcher.

    Uses the effective fleet YAML path resolved at startup: project config
    takes precedence over env-var settings (ADR 0005 Decision 2).
    """
    fleet_path = Path(_fleet_yaml_path)
    if not fleet_path.exists():
        return FleetConfig(strata=[], scopes=[], edges=[])
    return FleetConfig.load(fleet_path)


# ---------------------------------------------------------------------------
# Entitlement surfaces (ADR 0006 — one model, shared computation, distinct
# capacities). The write surface (ADR 0006 D1, below) is derived from the
# ancestor-chain computation alone and never grows. The read side now splits
# in two (ADR 0006 D3/D4, shipped): a chain-only surface for records and
# perspective targets, and a wider context surface — chain plus
# chain-referenced peer scopes (one hop, via intra-stratum edges) — for scope
# summary reads and perspective peer layers. Sideways knowledge flow still
# never extends to writes: it stays gated behind ratification or a
# context-only reference edge, never a direct write.
# ---------------------------------------------------------------------------


def _binding_surface_scope_ids(fleet: FleetConfig) -> set[str]:
    """Return this agent's bound scope plus its inter-stratum ancestor chain.

    This is the shared computation behind the chain-only entitlement surfaces
    (_entitled_scope_ids for records/perspective targets,
    _entitled_write_scope_ids for writes). Intra-stratum peers are never
    included here — a peer's memory binds this agent only if a common
    ancestor's scope-manager ratifies it into a directive. (Chain-referenced
    peers still reach this agent through the wider *context* surface — see
    _context_surface_scope_ids — but never through this binding surface.)
    """
    ancestors = fleet.inter_stratum_ancestors(_AGENT_SCOPE)
    return {_AGENT_SCOPE, *(s.id for s in ancestors)}


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


def _entitled_scope_ids(fleet: FleetConfig) -> set[str]:
    """Return the scope ids entitled for records and perspective targets.

    The chain-only surface is this agent's bound scope (``_AGENT_SCOPE``)
    plus its inter-stratum ancestor chain. Intra-stratum peers are
    deliberately excluded here even when chain-referenced — a peer's record
    is its own, and a peer is never a valid perspective target (ADR 0006
    D4). Chain-referenced peers are readable via the wider context surface
    (_check_entitled_context) and appear as non-binding layers inside a
    perspective, never as the perspective's own target or record.
    """
    return _binding_surface_scope_ids(fleet)


def _check_entitled(fleet: FleetConfig, scope_id: str) -> None:
    """Raise RuntimeError if *scope_id* is outside the chain-only entitled surface.

    Used by strata_read_scope_record and the strata_read_perspective target
    (ADR 0006 D4) — both stay chain-only even after D3's context surface
    widened scope summary reads.
    """
    if fleet.get_scope(_AGENT_SCOPE) is None:
        # Binding was valid at startup but the bound scope has since vanished
        # from fleet.yaml (rename/removal). Without this check the entitled
        # surface silently collapses and every read gets a misleading
        # peer-entitlement error.
        raise RuntimeError(
            f"your bound scope {_AGENT_SCOPE!r} no longer exists in the fleet "
            "config — fleet.yaml changed since this session started. Restore "
            "the scope in fleet.yaml or relaunch with a valid binding."
        )
    if scope_id not in _entitled_scope_ids(fleet):
        raise RuntimeError(
            f"scope {scope_id!r} is outside your entitled surface "
            f"(your scope {_AGENT_SCOPE!r} plus its inter-stratum ancestors). "
            "Records and perspective targets stay chain-only: a record "
            "audits the authority that binds you, and a perspective is "
            "composed for your own chain, not a peer's. A scope reachable "
            "only through a peer reference informs you via "
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


def _context_surface_scope_ids(fleet: FleetConfig) -> set[str]:
    """Return the scope ids entitled for scope summary reads.

    The context surface is this agent's chain-only surface plus every peer
    scope referenced (one hop, via an intra-stratum edge) by a scope on that
    chain — computed via ``fleet.entitlement_view(_AGENT_SCOPE)`` so peer
    logic lives in exactly one place.
    """
    view = fleet.entitlement_view(_AGENT_SCOPE)
    return {s.id for s in view.chain} | {s.id for s in view.referenced_peers}


def _check_entitled_context(fleet: FleetConfig, scope_id: str) -> None:
    """Raise RuntimeError if *scope_id* is outside the entitled context surface."""
    if fleet.get_scope(_AGENT_SCOPE) is None:
        # Same stale-binding hazard as the chain-only check.
        raise RuntimeError(
            f"your bound scope {_AGENT_SCOPE!r} no longer exists in the fleet "
            "config — fleet.yaml changed since this session started. Restore "
            "the scope in fleet.yaml or relaunch with a valid binding."
        )
    if scope_id not in _context_surface_scope_ids(fleet):
        raise RuntimeError(
            f"scope {scope_id!r} is outside your entitled context surface "
            f"(your scope {_AGENT_SCOPE!r}, its inter-stratum ancestors, and "
            "any peer scope referenced by a scope on that chain via an "
            "intra-stratum edge). Unreferenced peer scopes and descendants "
            "are not directly readable — legitimizing a knowledge flow "
            "between scopes is a reviewed reference edge in fleet.yaml, not "
            "a workaround here."
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


def _entitled_write_scope_ids(fleet: FleetConfig) -> set[str]:
    """Return the scope ids this agent is entitled to contribute to directly.

    The entitled write surface is this agent's bound scope (``_AGENT_SCOPE``)
    plus its inter-stratum ancestor chain — identical in shape to the read
    surface today, computed via the same shared helper, but named separately
    because the two are expected to diverge (ADR 0006 D1/D3/D4).
    """
    return _binding_surface_scope_ids(fleet)


def _check_entitled_write(fleet: FleetConfig, scope_id: str) -> None:
    """Raise RuntimeError if *scope_id* is outside the entitled write surface."""
    if fleet.get_scope(_AGENT_SCOPE) is None:
        # Same stale-binding hazard as the read surface: without this check,
        # a bound scope removed from fleet.yaml mid-session would silently
        # collapse the write surface and every write would get a misleading
        # entitlement error instead of a rebind error.
        raise RuntimeError(
            f"your bound scope {_AGENT_SCOPE!r} no longer exists in the fleet "
            "config — fleet.yaml changed since this session started. Restore "
            "the scope in fleet.yaml or relaunch with a valid binding."
        )
    if scope_id not in _entitled_write_scope_ids(fleet):
        _logger.warning(
            "contribution refused: contributor scope=%r skill=%r session=%r "
            "target scope=%r is outside the entitled write surface",
            _AGENT_SCOPE,
            _AGENT_SKILL,
            _AGENT_SESSION_ID,
            scope_id,
        )
        raise RuntimeError(
            f"scope {scope_id!r} is outside your entitled write surface "
            f"(your scope {_AGENT_SCOPE!r} plus its inter-stratum ancestors). "
            "Contribute to your own scope, or propose upward to an ancestor "
            "scope — that is how memory legitimately moves toward broader "
            "authority. Sideways flow to a peer scope happens only through "
            "ratification into a shared ancestor scope, or a context-only "
            "reference edge — never a direct write."
        )


# ---------------------------------------------------------------------------
# Refuse-to-start validation (ADR 0005 Decision 5)
# ---------------------------------------------------------------------------


def _validate_binding(
    fleet: FleetConfig | None,
    scope: str,
    skill: str,
    *,
    project_config_found: bool = False,
    searched_paths: list[str] | None = None,
    extra_errors: list[str] | None = None,
) -> None:
    """Validate agent binding before starting the MCP server.

    Runs all five checks independently, then reports every failure in a
    single error message before ``sys.exit(1)`` (per ADR 0005 Decision 5 —
    "all failures are reported in a single error message"). A user with
    multiple missing pieces sees the complete remediation list in one pass
    rather than fix-one-rerun-fix-next.

    Checks (in order, outermost setup gap → innermost binding mismatch):

    1. ``.strata/config.toml`` resolvable via walk-up.
    2. ``STRATA_AGENT_SCOPE`` env var set.
    3. Scope exists in fleet config.
    4. ``STRATA_AGENT_SKILL`` env var set.
    5. ``STRATA_AGENT_SKILL`` is in the scope's ``permitted_skills`` (when
       that list is non-empty).

    Args:
        fleet:                 The loaded FleetConfig, or ``None`` if check 1
                               failed (no config → no fleet to validate
                               against). Checks 3 + 5 are skipped when fleet
                               is None.
        scope:                 Value of ``STRATA_AGENT_SCOPE`` (may be empty).
        skill:                 Value of ``STRATA_AGENT_SKILL`` (may be empty).
        project_config_found:  True when ``.strata/config.toml`` was located.
        searched_paths:        Paths that were searched (for the error
                               message when config not found).
        extra_errors:          Startup failures collected before binding
                               validation (malformed config/fleet files, issue
                               #46) — reported in the same aggregated message.
    """
    errors: list[str] = list(extra_errors) if extra_errors else []

    # 1. .strata/config.toml must be resolvable.
    if not project_config_found:
        paths_str = (
            "\n  ".join(searched_paths)
            if searched_paths
            else "(no paths — walk-up search from CWD found nothing)"
        )
        errors.append(
            ".strata/config.toml not found.\n"
            "  Strata looked for .strata/config.toml walking up from the current directory:\n"
            f"    {paths_str}\n"
            "  Run `strata register` from your project root to create it, then open Claude Code\n"
            "  from within the project directory."
        )

    # 2. STRATA_AGENT_SCOPE must be set.
    if not scope:
        errors.append(
            "STRATA_AGENT_SCOPE is not set.\n"
            "  Set it before launching Claude Code:\n"
            "    export STRATA_AGENT_SCOPE=<scope_id>\n"
            "    export STRATA_AGENT_SKILL=<skill_name>\n"
            "  See README.md § 'Quick Start for an existing project' for the full setup."
        )

    # 3. Scope must exist in fleet config (skip when fleet not loaded or scope unset).
    scope_obj = None
    if fleet is not None and scope:
        scope_obj = fleet.get_scope(scope)
        if scope_obj is None:
            available = [s.id for s in fleet.active_scopes()]
            available_str = (
                ", ".join(available) if available else "(none — fleet.yaml may be empty)"
            )
            errors.append(
                f"scope {scope!r} not found in fleet config.\n"
                f"  Available scope IDs: {available_str}\n"
                f"  Update STRATA_AGENT_SCOPE to one of the above, or add scope {scope!r} to your "
                f"fleet.yaml."
            )

    # 4. STRATA_AGENT_SKILL must be set.
    if not skill:
        errors.append(
            "STRATA_AGENT_SKILL is not set.\n"
            "  Set it before launching Claude Code:\n"
            "    export STRATA_AGENT_SCOPE=<scope_id>\n"
            "    export STRATA_AGENT_SKILL=<skill_name>\n"
            "  See README.md § 'Quick Start for an existing project' for the full setup."
        )

    # 5. STRATA_AGENT_SKILL must be in permitted_skills (skip when scope or skill missing).
    if scope_obj is not None and skill:
        permitted = scope_obj.permitted_skills or []
        if permitted and skill not in permitted:
            errors.append(
                f"skill {skill!r} is not in the permitted skills for scope {scope!r}.\n"
                f"  Permitted skills for {scope!r}: {', '.join(permitted)}\n"
                f"  Update STRATA_AGENT_SKILL to one of the above, or update permitted_skills in "
                f"fleet.yaml."
            )

    if errors:
        # Report all failures in a single error message — the user sees the
        # complete remediation list in one pass.
        header = (
            "Strata MCP server refuses to start — "
            f"{len(errors)} validation {'failure' if len(errors) == 1 else 'failures'}:\n"
        )
        body = "\n".join(f"\n[{i + 1}] {err}" for i, err in enumerate(errors))
        print(header + body, file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="strata",
    instructions=(
        "Tools for reading from and contributing to the Strata fleet memory. "
        "Use strata_read_perspective before acting, contribute observations as "
        "context, and contribute binding decisions as directives only when warranted."
    ),
)


# ---------------------------------------------------------------------------
# Tool: strata_contribute
# ---------------------------------------------------------------------------


@mcp.tool()
def strata_contribute(
    scope_id: str,
    content: str,
    proposed_classification: Literal["directive", "context"],
    subject: str | None = None,
    supersedes: str | None = None,
) -> dict:
    """Submit a contribution to a scope's scope-manager for judgment.

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
    surface shape as the entitled read surface (issue #48). Contribute to
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
    fleet = _load_fleet()

    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise RuntimeError(f"Scope not found: {scope_id!r}")
    if scope.status == "archived":
        raise RuntimeError(f"Scope is archived and not accepting contributions: {scope_id!r}")
    _check_entitled_write(fleet, scope_id)

    stratum = next((s for s in fleet.strata if s.id == scope.stratum_id), None)
    if stratum is None:
        raise RuntimeError(
            f"Stratum {scope.stratum_id!r} for scope {scope_id!r} not found in fleet config."
        )

    ts = datetime.now(UTC).isoformat()
    contributor = ContributorRef(
        scope_id=_AGENT_SCOPE,
        skill=_AGENT_SKILL,
        session_id=_AGENT_SESSION_ID,
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

    return {
        "contribution_id": outcome.contribution_id,
        "judgment": {
            "decision": outcome.decision,
            "reasoning": outcome.reasoning,
            "summary_updated": outcome.summary_updated,
        },
    }


# ---------------------------------------------------------------------------
# Tool: strata_rejudge
# ---------------------------------------------------------------------------


@mcp.tool()
def strata_rejudge(contribution_id: str) -> dict:
    """Re-judge a contribution whose scope-manager judgment previously failed.

    Idempotent (issue #57): if the contribution already has a verdict, this is
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
    fleet = _load_fleet()

    contribution = _record_store.get_contribution(contribution_id)
    if contribution is None:
        raise RuntimeError(f"Contribution not found: {contribution_id!r}")
    _check_entitled_write(fleet, contribution.scope_id)

    from strata.app import JudgeUnavailable, rejudge_contribution  # noqa: PLC0415

    manager = _build_scope_manager()

    try:
        outcome = rejudge_contribution(
            contribution_id,
            fleet=fleet,
            record_store=_record_store,
            summary_store=_summary_store,
            scope_manager=manager,
            summary_max_words=_settings.summary_max_words,
        )
    except JudgeUnavailable as exc:
        raise RuntimeError(
            f"Scope-manager judgment failed again ({exc.error_class}): {exc}. "
            f"Contribution {exc.contribution_id} stays pending with a fresh "
            "judgment-attempt-failed event; call strata_rejudge again once the "
            "scope-manager is available."
        ) from exc

    return {
        "contribution_id": outcome.contribution_id,
        "judgment": {
            "decision": outcome.decision,
            "reasoning": outcome.reasoning,
            "summary_updated": outcome.summary_updated,
        },
    }


# ---------------------------------------------------------------------------
# Tool: strata_read_scope_summary
# ---------------------------------------------------------------------------


@mcp.tool()
def strata_read_scope_summary(scope_id: str | None = None) -> dict:
    """Return the scope summary for the given scope.

    The scope summary is the curated, condensed working view of a scope,
    maintained by its scope-manager.  It has two sections: directives (binding
    decisions that propagate to all descendant scopes) and context (non-binding
    observations and knowledge).

    Args:
        scope_id: The scope whose summary to read (e.g. ``g_arch``). Defaults
            to this agent's bound scope. An explicit scope_id must be within
            this agent's entitled *context* surface (ADR 0006 D3/D4): the
            bound scope, one of its inter-stratum ancestors, or a peer scope
            referenced by a scope on that chain via an intra-stratum edge.
            Unreferenced peers and descendants are not directly readable.

    Returns:
        Parsed scope summary: ``scope_id``, ``directives``, ``context``,
        ``updated_at``, ``version``, ``exists``. If the scope has no summary
        on disk yet, a synthesized empty summary is returned with
        ``version=0`` and ``exists=False`` — distinguishable from a real
        first write (``version=1``, ``exists=True``); see
        :class:`strata.summary_store.ScopeSummary` (issue #59).

    Raises:
        RuntimeError: If the scope does not exist, or if scope_id is outside
            this agent's entitled context surface.
    """
    fleet = _load_fleet()

    if scope_id is None:
        scope_id = _AGENT_SCOPE
    _check_entitled_context(fleet, scope_id)

    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise RuntimeError(f"Scope not found: {scope_id!r}")

    existing = _summary_store.read(scope_id)
    if existing is not None:
        return existing.model_dump()

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
    return empty.model_dump()


# ---------------------------------------------------------------------------
# Tool: strata_read_perspective
# ---------------------------------------------------------------------------


@mcp.tool()
def strata_read_perspective(scope_id: str | None = None) -> dict:
    """Return this agent's perspective on the fleet's long-term memory.

    A perspective is a composed, provenance-preserving view of: the scope's
    own summary, all inter-stratum ancestor summaries up to the root, and —
    ADR 0006 D3 — the summaries of any peer scopes referenced (one hop, via
    an intra-stratum edge) by a scope on that chain. Layers are ordered
    root-first: ancestors first, then the requested scope's own layer, then
    referenced-peer layers (sorted by scope id for deterministic ordering).

    Every layer carries ``relation`` (``"self"``, ``"ancestor"``, or
    ``"peer_reference"``) and ``binding`` (``True`` for self/ancestor layers,
    ``False`` for peer layers). Peer layers are **context only** — nothing in
    them binds the reader: a peer's directives remain directives in their
    home scope, but to this reader they are context (CONTEXT.md §
    Intra-stratum edge). Each peer layer carries that peer's full summary,
    clearly labelled by its own scope id — composition is provenance-
    preserving, not lossy; a peer's content is never stripped down before
    being composed in. Peer-of-peer references are not traversed: only edges
    whose source scope is itself on the chain count (one hop, per
    ``FleetConfig.entitlement_view``).

    If a chain or peer scope has no summary on disk yet, its layer is still
    included with empty directives and context so that the structure is
    visible; that layer's summary honestly reports ``version=0``/
    ``exists=False`` rather than looking like a real first write (issue #59).

    Args:
        scope_id: The scope for which to build the perspective. Defaults to
            this agent's bound scope. An explicit scope_id must be the bound
            scope or one of its inter-stratum ancestors (issue #48) — this is
            the perspective *target*, which stays chain-only (ADR 0006 D4):
            you compose a perspective for your own chain, not for a peer's.

    Returns:
        ``{layers: [{scope_id, stratum_id, summary, relation, binding}],
        scope_id: <requested>, _layers_count: N}`` ordered root-first, then
        self, then sorted peer layers.

    Raises:
        RuntimeError: If the scope is unknown, or if scope_id is outside this
            agent's entitled (chain-only) surface.
    """
    fleet = _load_fleet()

    if scope_id is None:
        scope_id = _AGENT_SCOPE
    _check_entitled(fleet, scope_id)

    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise RuntimeError(f"Scope not found: {scope_id!r}")

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

    return compose_perspective(
        scope_id,
        fleet=fleet,
        summary_store=_summary_store,
        operator_reader=_operator_reader,
    )


# ---------------------------------------------------------------------------
# Tool: strata_list_scopes
# ---------------------------------------------------------------------------


@mcp.tool()
def strata_list_scopes() -> dict:
    """Return the full fleet configuration: strata, scopes, and edges.

    Re-reads fleet.yaml from disk on every call (ADR 0004 Decision 1) so
    the agent always sees the current fleet topology.

    Use this to understand the fleet's structure — which scopes exist, how
    they are arranged into strata, and which inter-stratum and intra-stratum
    edges connect them.

    Returns:
        Fleet config: ``strata`` (list), ``scopes`` (list), ``edges`` (list).
    """
    fleet = _load_fleet()

    active = fleet.active_scopes()
    active_ids = {s.id for s in active}
    active_edges = [e for e in fleet.edges if e.from_ in active_ids and e.to in active_ids]

    return {
        "strata": [s.model_dump() for s in fleet.strata],
        "scopes": [s.model_dump() for s in active],
        "edges": [{"from_scope_id": e.from_, "to_scope_id": e.to} for e in active_edges],
    }


# ---------------------------------------------------------------------------
# Tool: strata_read_scope_record
# ---------------------------------------------------------------------------


@mcp.tool()
def strata_read_scope_record(scope_id: str | None = None) -> dict:
    """Return the immutable contribution record for a scope (forensic view).

    The record is the append-only log of every write ever accepted into the
    scope, including the scope-manager's judgment on each contribution.  Use
    this for debugging, accountability investigation, or understanding the
    history behind the current scope summary.

    Migration note (issue #48 supersedes the earlier HTTP-parity note): this
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
            (issue #48) — a peer scope is not readable here even when it is
            referenced by your chain and its summary is otherwise readable
            (ADR 0006 D4).

    Returns:
        ``contributions`` (list) and ``judgments`` (list).

    Raises:
        RuntimeError: If scope_id is outside this agent's entitled
            (chain-only) surface.
    """
    fleet = _load_fleet()

    if scope_id is None:
        scope_id = _AGENT_SCOPE
    _check_entitled(fleet, scope_id)

    contributions = _record_store.list_contributions(scope_id=scope_id)
    judgments = _record_store.list_judgments(scope_id=scope_id)
    judgment_attempts = _record_store.list_judgment_attempts(scope_id=scope_id)

    return {
        "contributions": [asdict(c) for c in contributions],
        "judgments": [asdict(j) for j in judgments],
        # Failed-judgment events (issue #57): a contribution with attempts but
        # no judgment is pending, distinguishable in the forensic view.
        "judgment_attempts": [asdict(a) for a in judgment_attempts],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the Strata MCP server.

    Startup order (issue #46 — nothing touches storage before validation):

    1. Resolve paths (project config walk-up; env fallback).
    2. Load the fleet config — parse/invariant failures become refuse-to-start
       entries, not tracebacks.
    3. Validate the agent binding (scope, skill) — all failures aggregated.
    4. Initialise storage (migrations, stores) — failures also render as a
       refuse-to-start message.
    5. Serve.

    Any failure exits 1 with a single actionable message (ADR 0005 D5).
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
            "  Fix the file (or delete it and re-run `strata register`)."
        )

    paths = resolve_storage_paths(_settings)
    _set_paths(paths)

    # Load fleet only when we have a config; without one there's nothing to
    # validate against, and the loader would just hit env-var fallbacks.
    # Parse errors and invariant violations become refuse-to-start entries.
    fleet = None
    if project_config is not None:
        try:
            fleet = _load_fleet()
        except FleetConfigError as exc:
            startup_errors.append(
                f"fleet config at {paths.fleet_yaml_path} is invalid "
                f"[{exc.kind}]: {exc.message}\n"
                "  Fix fleet.yaml, then relaunch."
            )
        except yaml.YAMLError as exc:
            startup_errors.append(
                f"fleet config at {paths.fleet_yaml_path} is not valid YAML: {exc}\n"
                "  Fix fleet.yaml, then relaunch."
            )

    _validate_binding(
        fleet,
        _AGENT_SCOPE,
        _AGENT_SKILL,
        project_config_found=project_config is not None,
        searched_paths=[str(p) for p in searched_paths_out],
        extra_errors=startup_errors,
    )

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
