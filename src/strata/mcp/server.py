"""Strata MCP server — stdio transport, embedded mode.

Operates directly on RecordStore, SummaryStore, and FleetConfig in-process.
No HTTP proxy — the FastAPI backend is the Console UI layer only.
(ADR 0004 Decision 1 — embedded mode.)

Vocabulary follows CONTEXT.md verbatim: scope, stratum, directive, context,
contribution, scope summary, perspective, record, provenance.

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

import os
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.record_store import ContributorRef, RecordStore
from strata.settings import get_settings
from strata.summary_store import ScopeSummary, SummaryStore

# ---------------------------------------------------------------------------
# Module-level singletons — instantiated once at import via get_settings()
# ---------------------------------------------------------------------------

_settings = get_settings()

# Apply any pending migrations so the DB is ready before first tool call.
run_migrations(_settings.db_path)

_record_store = RecordStore(_settings.db_path)
_summary_store = SummaryStore(_settings.summaries_dir)

# Agent provenance — recorded on every contribution.
# STRATA_AGENT_SCOPE and STRATA_AGENT_SKILL have no defaults;
# _validate_binding() enforces they are set before mcp.run().
# STRATA_AGENT_SESSION_ID is optional; generate one when absent.
_AGENT_SCOPE: str = os.environ.get("STRATA_AGENT_SCOPE", "")
_AGENT_SKILL: str = os.environ.get("STRATA_AGENT_SKILL", "")
_AGENT_SESSION_ID: str = os.environ.get(
    "STRATA_AGENT_SESSION_ID", f"sess_{uuid.uuid4().hex[:8]}"
)

# ---------------------------------------------------------------------------
# Fleet config helper — re-read on every call that needs fleet info (ADR 0004
# Decision 1): no mtime watcher, no IPC. The 8 load-time invariants run on
# each read. Cheap: fleet.yaml is KB-range and parses fast.
# ---------------------------------------------------------------------------


def _load_fleet() -> FleetConfig:
    """Load and validate the fleet config from disk.

    Re-reads fleet.yaml on every call so the MCP server always sees the
    current config without IPC or a file-watcher.
    """
    fleet_path = Path(_settings.fleet_yaml_path)
    if not fleet_path.exists():
        return FleetConfig(strata=[], scopes=[], edges=[])
    return FleetConfig.load(fleet_path)


# ---------------------------------------------------------------------------
# Refuse-to-start validation (ADR 0005 Decision 5)
# ---------------------------------------------------------------------------


def _validate_binding(fleet: FleetConfig, scope: str, skill: str) -> None:
    """Validate agent binding before starting the MCP server.

    Checks (in order):
    1. STRATA_AGENT_SCOPE env var is set.
    2. The scope exists in the fleet config.
    3. STRATA_AGENT_SKILL is set.
    4. STRATA_AGENT_SKILL is in the scope's permitted_skills (if that list
       is non-empty; an empty list means any skill is permitted).

    Any failure calls sys.exit(1) with an actionable message.

    Args:
        fleet:  The loaded FleetConfig (or empty FleetConfig if no file).
        scope:  Value of STRATA_AGENT_SCOPE (may be empty string).
        skill:  Value of STRATA_AGENT_SKILL (may be empty string).
    """
    import sys

    # 1. STRATA_AGENT_SCOPE must be set.
    if not scope:
        print(
            "Strata MCP server refuses to start: STRATA_AGENT_SCOPE is not set.\n"
            "\n"
            "Set it before launching Claude Code:\n"
            "  export STRATA_AGENT_SCOPE=<scope_id>\n"
            "  export STRATA_AGENT_SKILL=<skill_name>\n"
            "\n"
            "See README.md § 'Quick Start for an existing project' for the full setup.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 2. Scope must exist in fleet config.
    scope_obj = fleet.get_scope(scope)
    if scope_obj is None:
        available = [s.id for s in fleet.active_scopes()]
        available_str = ", ".join(available) if available else "(none — fleet.yaml may be empty)"
        print(
            f"Strata MCP server refuses to start: scope {scope!r} not found in fleet config.\n"
            f"\n"
            f"Available scope IDs: {available_str}\n"
            f"\n"
            f"Update STRATA_AGENT_SCOPE to one of the above, or add scope {scope!r} to your "
            f"fleet.yaml.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 3. STRATA_AGENT_SKILL must be set.
    if not skill:
        print(
            "Strata MCP server refuses to start: STRATA_AGENT_SKILL is not set.\n"
            "\n"
            "Set it before launching Claude Code:\n"
            "  export STRATA_AGENT_SCOPE=<scope_id>\n"
            "  export STRATA_AGENT_SKILL=<skill_name>\n"
            "\n"
            "See README.md § 'Quick Start for an existing project' for the full setup.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 4. STRATA_AGENT_SKILL must be in permitted_skills (when list is non-empty).
    permitted = scope_obj.permitted_skills or []
    if permitted and skill not in permitted:
        print(
            f"Strata MCP server refuses to start: skill {skill!r} is not in the permitted "
            f"skills for scope {scope!r}.\n"
            f"\n"
            f"Permitted skills for {scope!r}: {', '.join(permitted)}\n"
            f"\n"
            f"Update STRATA_AGENT_SKILL to one of the above, or update permitted_skills in "
            f"fleet.yaml.",
            file=sys.stderr,
        )
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

    Args:
        scope_id: Target scope to contribute to (e.g. ``g_arch``).
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
        RuntimeError: If the scope is not found or is archived.
    """
    fleet = _load_fleet()

    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise RuntimeError(f"Scope not found: {scope_id!r}")
    if scope.status == "archived":
        raise RuntimeError(f"Scope is archived and not accepting contributions: {scope_id!r}")

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

    contribution = _record_store.append_contribution(
        scope_id=scope_id,
        content=content,
        proposed_classification=proposed_classification,
        subject=subject,
        supersedes=supersedes,
        contributor=contributor,
    )

    current_summary = _summary_store.read(scope_id)
    recent_contributions = _record_store.list_contributions(scope_id=scope_id, limit=20)

    # Resolve the inter-stratum parent's summary for manager context (ADR 0004
    # Decision 2). The caller (here) does the graph traversal; the manager is a
    # pure judgment primitive that receives the resolved summary.
    parent_scope = fleet.inter_stratum_parent(scope_id)
    parent_summary = _summary_store.read(parent_scope.id) if parent_scope is not None else None

    # Import here to avoid circular imports and keep the scope-manager import
    # lazy — it pulls in anthropic which may not be configured in all envs.
    import anthropic  # noqa: PLC0415

    from strata.scope_manager import ScopeManager  # noqa: PLC0415

    manager = ScopeManager(
        client=anthropic.Anthropic(api_key=_settings.anthropic_api_key),
        model=_settings.manager_model,
    )

    try:
        judgment = manager.judge(
            scope=scope,
            stratum=stratum,
            parent_summary=parent_summary,
            current_summary=current_summary,
            recent_contributions=recent_contributions,
            new_contribution=contribution,
        )
    except Exception as exc:
        raise RuntimeError(f"Scope-manager judgment failed: {exc}") from exc

    _record_store.record_judgment(
        contribution_id=contribution.id,
        decision=judgment.decision,
        judged_by="scope-manager",
        notes=judgment.reasoning,
    )

    summary_updated = False
    if judgment.decision != "decline" and judgment.new_summary is not None:
        _summary_store.write(scope_id, judgment.new_summary)
        summary_updated = True

    return {
        "contribution_id": contribution.id,
        "judgment": {
            "decision": judgment.decision,
            "reasoning": judgment.reasoning,
            "summary_updated": summary_updated,
        },
    }


# ---------------------------------------------------------------------------
# Tool: strata_read_scope_summary
# ---------------------------------------------------------------------------


@mcp.tool()
def strata_read_scope_summary(scope_id: str) -> dict:
    """Return the scope summary for the given scope.

    The scope summary is the curated, condensed working view of a scope,
    maintained by its scope-manager.  It has two sections: directives (binding
    decisions that propagate to all descendant scopes) and context (non-binding
    observations and knowledge).

    Args:
        scope_id: The scope whose summary to read (e.g. ``g_arch``).

    Returns:
        Parsed scope summary: ``scope_id``, ``directives``, ``context``,
        ``updated_at``.

    Raises:
        RuntimeError: If the scope does not exist.
    """
    fleet = _load_fleet()

    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise RuntimeError(f"Scope not found: {scope_id!r}")

    existing = _summary_store.read(scope_id)
    if existing is not None:
        return existing.model_dump()

    # Scope exists but has no summary yet — return an empty summary.
    empty = ScopeSummary(
        scope_id=scope_id,
        directives=[],
        context="",
        updated_at=datetime.now(tz=UTC).isoformat(),
    )
    return empty.model_dump()


# ---------------------------------------------------------------------------
# Tool: strata_read_perspective
# ---------------------------------------------------------------------------


def _summary_for_scope(scope_id: str) -> dict:
    """Return a scope's summary as a plain dict, using an empty summary if none exists."""
    existing = _summary_store.read(scope_id)
    if existing is not None:
        return existing.model_dump()
    empty = ScopeSummary(
        scope_id=scope_id,
        directives=[],
        context="",
        updated_at=datetime.now(tz=UTC).isoformat(),
    )
    return empty.model_dump()


@mcp.tool()
def strata_read_perspective(scope_id: str) -> dict:
    """Return this agent's perspective on the fleet's long-term memory.

    A perspective is a composed, provenance-preserving view of the scope's
    own summary plus all inter-stratum ancestor summaries up to the root.
    Layers are ordered root-first (L0 first, requested scope last).

    Only inter-stratum edges are traversed — peer (intra-stratum) edges are
    never followed.  If a scope in the ancestor chain has no summary on disk
    yet, its layer is still included with empty directives and context so that
    the structure is visible.

    Args:
        scope_id: The scope for which to build the perspective.

    Returns:
        ``{layers: [{scope_id, stratum_id, summary}], scope_id: <requested>,
        _layers_count: N}`` ordered root-first.

    Raises:
        RuntimeError: If the scope is unknown.
    """
    fleet = _load_fleet()

    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise RuntimeError(f"Scope not found: {scope_id!r}")

    # Build the ancestor chain (root-first), then append the requested scope.
    ancestors = fleet.inter_stratum_ancestors(scope_id)
    chain = [*ancestors, scope]

    layers = []
    for s in chain:
        layers.append(
            {
                "scope_id": s.id,
                "stratum_id": s.stratum_id,
                "summary": _summary_for_scope(s.id),
            }
        )

    return {
        "scope_id": scope_id,
        "layers": layers,
        "_layers_count": len(layers),
    }


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
def strata_read_scope_record(scope_id: str) -> dict:
    """Return the immutable contribution record for a scope (forensic view).

    The record is the append-only log of every write ever accepted into the
    scope, including the scope-manager's judgment on each contribution.  Use
    this for debugging, accountability investigation, or understanding the
    history behind the current scope summary.

    Does not re-read fleet.yaml — record retrieval is purely from the
    SQLite record store and needs no fleet info.

    Migration note: returns an empty record (``{"contributions": [],
    "judgments": []}``) for unknown scopes, where the old HTTP
    ``GET /scopes/{id}/record`` endpoint would have returned 404. The other
    MCP tools still raise on unknown scopes, matching the prior behaviour.

    Args:
        scope_id: The scope whose record to read (e.g. ``g_backend``).

    Returns:
        ``contributions`` (list) and ``judgments`` (list).
    """
    contributions = _record_store.list_contributions(scope_id=scope_id)
    judgments = _record_store.list_judgments(scope_id=scope_id)

    return {
        "contributions": [asdict(c) for c in contributions],
        "judgments": [asdict(j) for j in judgments],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the Strata MCP server.

    Validates the agent binding (scope, skill) before starting.  Any
    validation failure calls sys.exit(1) with an actionable message.
    """
    fleet = _load_fleet()
    _validate_binding(fleet, _AGENT_SCOPE, _AGENT_SKILL)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
