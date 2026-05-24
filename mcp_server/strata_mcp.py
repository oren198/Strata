"""Strata MCP server — stdio transport.

Proxies to the Strata backend HTTP API, exposing five tools that a Claude Code
agent (session, skill, scope) can call to read from and contribute to the
fleet's shared memory.

Vocabulary follows CONTEXT.md verbatim: scope, stratum, directive, context,
contribution, scope summary, perspective, record, provenance.

Environment variables
---------------------
STRATA_BACKEND_URL
    Base URL of the Strata backend.  Default: http://127.0.0.1:8000
STRATA_BACKEND_TIMEOUT
    HTTP request timeout in seconds.  Default: 30
STRATA_AGENT_SCOPE
    The scope this agent is bound to (e.g. ``g_backend``).
    Recorded in contribution provenance.  Default: ``unknown``
STRATA_AGENT_SKILL
    The skill this agent is running (e.g. ``strata-developer``).
    Recorded in contribution provenance.  Default: ``unknown``
STRATA_AGENT_SESSION_ID
    Unique identifier for this session.
    Recorded in contribution provenance.  Default: ``sess_local``
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Literal

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration (env-var driven, mirrors STRATA_ prefix convention)
# ---------------------------------------------------------------------------

_BACKEND_URL: str = os.environ.get("STRATA_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
_TIMEOUT: float = float(os.environ.get("STRATA_BACKEND_TIMEOUT", "30"))

# Agent provenance — recorded on every contribution
_AGENT_SCOPE: str = os.environ.get("STRATA_AGENT_SCOPE", "unknown")
_AGENT_SKILL: str = os.environ.get("STRATA_AGENT_SKILL", "unknown")
_AGENT_SESSION_ID: str = os.environ.get("STRATA_AGENT_SESSION_ID", "sess_local")

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _client() -> httpx.Client:
    """Return a configured :class:`httpx.Client` for the Strata backend."""
    return httpx.Client(base_url=_BACKEND_URL, timeout=_TIMEOUT)


def _raise_for_status(response: httpx.Response) -> None:
    """Raise a descriptive :class:`RuntimeError` for non-2xx responses.

    Includes the HTTP status code and the raw response body so the calling
    agent can diagnose the problem without extra round-trips.
    """
    if response.is_error:
        raise RuntimeError(f"Strata backend returned {response.status_code}: {response.text!r}")


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
        JSON response from the backend: ``contribution_id`` and ``judgment``
        (decision, reasoning, summary_updated).

    Raises:
        RuntimeError: If the backend returns a non-2xx status or is
            unreachable.
    """
    ts = datetime.now(UTC).isoformat()
    body = {
        "scope_id": scope_id,
        "content": content,
        "proposed_classification": proposed_classification,
        "subject": subject,
        "supersedes": supersedes,
        "contributor": {
            "scope_id": _AGENT_SCOPE,
            "skill": _AGENT_SKILL,
            "session_id": _AGENT_SESSION_ID,
            "ts": ts,
        },
    }
    try:
        with _client() as client:
            response = client.post("/contribute", json=body)
    except httpx.ConnectError as exc:
        raise RuntimeError(f"Cannot reach Strata backend at {_BACKEND_URL!r}: {exc}") from exc
    _raise_for_status(response)
    return response.json()


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
        Parsed JSON scope summary: ``scope_id``, ``directives``, ``context``,
        ``updated_at``.

    Raises:
        RuntimeError: If the scope does not exist (404), the backend returns
            another non-2xx status, or the backend is unreachable.
    """
    try:
        with _client() as client:
            response = client.get(f"/scopes/{scope_id}/summary")
    except httpx.ConnectError as exc:
        raise RuntimeError(f"Cannot reach Strata backend at {_BACKEND_URL!r}: {exc}") from exc
    _raise_for_status(response)
    return response.json()


# ---------------------------------------------------------------------------
# Tool: strata_read_perspective
# ---------------------------------------------------------------------------


@mcp.tool()
def strata_read_perspective(scope_id: str) -> dict:
    """Return this agent's perspective on the fleet's long-term memory.

    A perspective is a composed, provenance-preserving view of the scope's
    own summary plus all inter-stratum ancestor summaries up to the root.
    It is the primary read interface for an agent before acting.

    V1 NOTE: True perspective composition across ancestor scopes is post-V1.
    For now this returns the scope's own summary only, decorated with a
    ``_v1_limitation`` note.  The tool signature and name are stable; skill
    prompts written against this tool will not need to change when full
    perspective composition is implemented.

    Args:
        scope_id: The scope for which to build the perspective.

    Returns:
        Scope summary JSON plus a ``_v1_limitation`` key documenting the
        stub behaviour.

    Raises:
        RuntimeError: If the scope is unknown, the backend returns a non-2xx
            status, or the backend is unreachable.
    """
    summary = strata_read_scope_summary(scope_id)
    summary["_v1_limitation"] = (
        "Perspective composition across ancestor scopes is post-V1. "
        "This response contains the scope's own summary only."
    )
    return summary


# ---------------------------------------------------------------------------
# Tool: strata_list_scopes
# ---------------------------------------------------------------------------


@mcp.tool()
def strata_list_scopes() -> dict:
    """Return the full fleet configuration: strata, scopes, and edges.

    Use this to understand the fleet's structure — which scopes exist, how
    they are arranged into strata, and which inter-stratum and intra-stratum
    edges connect them.

    Returns:
        Parsed JSON fleet config: ``strata`` (list), ``scopes`` (list),
        ``edges`` (list).

    Raises:
        RuntimeError: If the backend returns a non-2xx status or is
            unreachable.
    """
    try:
        with _client() as client:
            response = client.get("/scopes")
    except httpx.ConnectError as exc:
        raise RuntimeError(f"Cannot reach Strata backend at {_BACKEND_URL!r}: {exc}") from exc
    _raise_for_status(response)
    return response.json()


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

    Args:
        scope_id: The scope whose record to read (e.g. ``g_backend``).

    Returns:
        Parsed JSON: ``contributions`` (list) and ``judgments`` (list).

    Raises:
        RuntimeError: If the scope is unknown, the backend returns a non-2xx
            status, or the backend is unreachable.
    """
    try:
        with _client() as client:
            response = client.get(f"/scopes/{scope_id}/record")
    except httpx.ConnectError as exc:
        raise RuntimeError(f"Cannot reach Strata backend at {_BACKEND_URL!r}: {exc}") from exc
    _raise_for_status(response)
    return response.json()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
