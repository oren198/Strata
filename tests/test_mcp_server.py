"""Tests for the Strata MCP server tool functions — embedded mode.

The MCP server now operates directly on RecordStore and SummaryStore in-process
(ADR 0004 Decision 1).  No HTTP backend is required.

Tests:
1. strata_contribute writes a row to RecordStore without any HTTP server.
2. strata_read_scope_summary reads from SummaryStore (file on disk) directly.
3. strata_read_perspective returns layers in root-first order (Decision 3).
4. strata_list_scopes reads fleet.yaml fresh on each call; second call reflects
   a change made between the two calls.
5. strata_read_scope_record reads contributions and judgments from RecordStore
   directly (no fleet info needed, no HTTP).
6. strata_contribute raises RuntimeError when scope is not in fleet config.
7. WAL mode: after RecordStore init, PRAGMA journal_mode returns 'wal'.

Decision 3 (perspective composition) tests:
8.  strata_read_perspective on a root scope returns exactly one layer.
9.  strata_read_perspective on a deep scope returns N+1 layers, root-first.
10. An UNREFERENCED peer (intra-stratum, no reference edge) is absent from layers.
11. Missing ancestor summary → layer still present with empty content.
12. _v1_limitation key is absent (regression guard).

ADR 0006 Decision D1 (entitled write-target surface) tests:
13. strata_contribute to own scope, parent, and root/grandparent all succeed.
14. strata_contribute to a sibling (peer) scope is refused with the write
    entitlement error.
15. strata_contribute to a descendant scope is refused with the write
    entitlement error.
16. A refused write leaves no row in the record store (no contribution, no
    judgment).
17. A refused write emits a WARNING log line naming the contributor scope,
    skill, session id, and the refused target scope.
18. Unknown-scope and archived-scope errors are unchanged, and are still
    reported before the entitlement check runs.

ADR 0006 Decisions D3+D4 (peer-reference composition, read-surface split):
19. Self/ancestor perspective layers carry relation + binding=True.
20. A peer referenced by a chain scope appears as a peer_reference,
    binding=False layer with its full summary.
21. A peer referenced by an ANCESTOR (not just the target scope) also appears.
22. Peer-of-peer references are not traversed (one hop only).
23. An unreferenced sibling stays absent even in a fleet with referenced peers.
24. A referenced peer with no on-disk summary gets version=0/exists=False.
25. Peer layers are sorted by scope id for deterministic ordering.
26. strata_read_scope_summary succeeds for a chain-referenced peer (context
    surface); still refuses an unreferenced sibling.
27. strata_read_scope_record refuses a referenced peer — records stay
    chain-only.
28. strata_read_perspective refuses a referenced peer as its TARGET —
    perspectives compose your own chain, not a peer's.

The MCP protocol layer (FastMCP, stdio transport) is not tested here — that is
the SDK's responsibility.  Only the tool wrappers are exercised.

Vocabulary follows CONTEXT.md: scope, stratum, directive, context,
contribution, scope summary, perspective, record, provenance.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest
import yaml

# Make strata importable when running from the repo root.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.fleet_config import FleetConfig  # noqa: E402
from strata.migrator import run_migrations  # noqa: E402
from strata.record_store import ContributorRef, RecordStore  # noqa: E402
from strata.scope_manager import ScopeManagerJudgment  # noqa: E402
from strata.summary_store import ScopeSummary, SummaryStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> str:
    """Apply migrations to a fresh DB and return the path string."""
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    return db_path


def _make_fleet_yaml(tmp_path: Path) -> Path:
    """Write a minimal fleet.yaml and return its path.

    Edge convention: child→parent (from=child, to=parent), matching the
    dev-team.yaml and research-group.yaml templates.  g_backend (L1) is a
    child of g_arch (L0).
    """
    fleet = {
        "strata": [
            {"id": "L0", "name": "executive", "ordinal": 0},
            {"id": "L1", "name": "team", "ordinal": 1},
        ],
        "scopes": [
            {"id": "g_arch", "name": "Architecture", "stratum_id": "L0"},
            {"id": "g_backend", "name": "Backend Dev", "stratum_id": "L1"},
        ],
        "edges": [
            # Inter-stratum: child (L1) → parent (L0)
            {"from": "g_backend", "to": "g_arch"},
        ],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return fleet_path


def _make_deep_fleet_yaml(tmp_path: Path) -> Path:
    """Write a three-level fleet.yaml for ancestor-walk tests.

    Topology: g_exec (L0) ← g_func (L1) ← g_team (L2)
    g_peer is an L1 scope with no intra-stratum reference edge to or from
    g_func — an *unreferenced* sibling that must never appear in the g_team
    perspective or be directly readable (ADR 0006 D3/D4 still refuse
    unreferenced peers; only chain-referenced peers gain a surface).
    """
    fleet = {
        "strata": [
            {"id": "L0", "name": "executive", "ordinal": 0},
            {"id": "L1", "name": "function", "ordinal": 1},
            {"id": "L2", "name": "team", "ordinal": 2},
        ],
        "scopes": [
            {"id": "g_exec", "name": "Executive", "stratum_id": "L0"},
            {"id": "g_func", "name": "Function", "stratum_id": "L1"},
            {"id": "g_team", "name": "Team", "stratum_id": "L2"},
            {"id": "g_peer", "name": "Peer Function", "stratum_id": "L1"},
        ],
        "edges": [
            # Inter-stratum: child → parent
            {"from": "g_func", "to": "g_exec"},
            {"from": "g_team", "to": "g_func"},
            {"from": "g_peer", "to": "g_exec"},
            # No intra-stratum edge to/from g_peer — deliberately unreferenced.
        ],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return fleet_path


def _make_peer_composition_fleet_yaml(tmp_path: Path) -> Path:
    """Write a fleet.yaml exercising ADR 0013 D2/D3 (one-hop publication composition).

    Topology: g_exec (L0) ← g_func (L1) ← g_team (L2).

    Reference edges (publication only, one hop — ADR 0013 D3):
      - g_team → g_team_peer   (g_team's OWN reference — must appear)
      - g_func → g_peer_a      (g_team's PARENT's own reference — must NOT
        appear in g_team's perspective; it is g_func's edge, not g_team's)
      - g_func → g_peer_b      (second parent-owned reference — same rule)
      - g_exec → g_exec_peer   (the ROOT's own reference — must NOT appear
        either, for the same reason, two hops removed)
      - g_peer_a → g_peer_of_peer (peer-of-peer — one hop only, must NOT
        appear in g_func's perspective either since g_peer_a is not itself
        on g_func's chain)

    g_sibling is an L1 scope with no reference edge at all — an unreferenced
    sibling that must never appear and must never be directly readable.
    """
    fleet = {
        "strata": [
            {"id": "L0", "name": "executive", "ordinal": 0},
            {"id": "L1", "name": "function", "ordinal": 1},
            {"id": "L2", "name": "team", "ordinal": 2},
        ],
        "scopes": [
            {"id": "g_exec", "name": "Executive", "stratum_id": "L0"},
            {"id": "g_exec_peer", "name": "Executive Peer", "stratum_id": "L0"},
            {"id": "g_func", "name": "Function", "stratum_id": "L1"},
            {"id": "g_team", "name": "Team", "stratum_id": "L2"},
            {"id": "g_team_peer", "name": "Team Peer", "stratum_id": "L2"},
            {"id": "g_peer_a", "name": "Peer A", "stratum_id": "L1"},
            {"id": "g_peer_b", "name": "Peer B", "stratum_id": "L1"},
            {"id": "g_peer_of_peer", "name": "Peer Of Peer", "stratum_id": "L1"},
            {"id": "g_sibling", "name": "Unreferenced Sibling", "stratum_id": "L1"},
        ],
        "edges": [
            # Inter-stratum: child → parent
            {"from": "g_func", "to": "g_exec"},
            {"from": "g_team", "to": "g_func"},
            # Intra-stratum peer references (publication only, one hop)
            {"from": "g_func", "to": "g_peer_b"},
            {"from": "g_func", "to": "g_peer_a"},
            {"from": "g_exec", "to": "g_exec_peer"},
            {"from": "g_peer_a", "to": "g_peer_of_peer"},
            {"from": "g_team", "to": "g_team_peer"},
        ],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return fleet_path


def _make_write_surface_fleet_yaml(tmp_path: Path) -> Path:
    """Write a fleet.yaml for ADR 0006 D1 (entitled write-target surface) tests.

    Topology: g_exec (L0) <- g_func (L1) <- g_team (L2), with g_team2 as a
    sibling of g_team (also L2, child of g_func, no reference edge between
    them) and g_archived an archived L2 scope (also a child of g_func).
    """
    fleet = {
        "strata": [
            {"id": "L0", "name": "executive", "ordinal": 0},
            {"id": "L1", "name": "function", "ordinal": 1},
            {"id": "L2", "name": "team", "ordinal": 2},
        ],
        "scopes": [
            {"id": "g_exec", "name": "Executive", "stratum_id": "L0"},
            {"id": "g_func", "name": "Function", "stratum_id": "L1"},
            {"id": "g_team", "name": "Team", "stratum_id": "L2"},
            {"id": "g_team2", "name": "Team Two", "stratum_id": "L2"},
            {
                "id": "g_archived",
                "name": "Archived Team",
                "stratum_id": "L2",
                "status": "archived",
            },
        ],
        "edges": [
            # Inter-stratum: child → parent
            {"from": "g_func", "to": "g_exec"},
            {"from": "g_team", "to": "g_func"},
            {"from": "g_team2", "to": "g_func"},
            {"from": "g_archived", "to": "g_func"},
        ],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return fleet_path


def _make_summary(scope_id: str, context: str = "some context") -> ScopeSummary:
    """Build a minimal ScopeSummary for seeding tests."""
    return ScopeSummary(
        scope_id=scope_id,
        directives=[],
        context=context,
        updated_at="2026-05-30T00:00:00+00:00",
    )


def _make_contributor() -> ContributorRef:
    return ContributorRef(
        scope_id="g_backend",
        skill="strata-developer",
        session_id="sess_test",
        ts="2026-05-30T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Import helper — reload strata_mcp with patched settings pointing to tmp dirs
# ---------------------------------------------------------------------------


def _load_mcp_module(db_path: str, summaries_dir: str, fleet_yaml_path: str):
    """Import (or reload) strata.mcp.server with settings wired to *tmp_path*."""
    import importlib

    # Remove any prior import so the module-level singletons re-initialise.
    for key in list(sys.modules.keys()):
        if "strata.mcp" in key or "strata_mcp" in key:
            del sys.modules[key]

    from strata.settings import Settings, get_settings

    get_settings.cache_clear()

    fake_settings = Settings(
        db_path=db_path,
        summaries_dir=summaries_dir,
        fleet_yaml_path=fleet_yaml_path,
        manager_model="claude-haiku-4-5",
        anthropic_api_key=None,
    )

    # Patch both get_settings and load_project_config so module-level singletons
    # use our tmp-path instances and don't accidentally discover a real project config
    # on the filesystem.
    with (
        patch("strata.settings.get_settings", return_value=fake_settings),
        patch("strata.project_config.load_project_config", return_value=None),
    ):
        import strata.mcp.server as mod

        importlib.reload(mod)

    # Patch module-level singletons to use our tmp-path instances.
    mod._settings = fake_settings
    mod._project_config = None
    mod._db_path = db_path
    mod._summaries_dir = summaries_dir
    mod._fleet_yaml_path = fleet_yaml_path
    mod._record_store = RecordStore(db_path)
    mod._summary_store = SummaryStore(summaries_dir)

    # Session-state substrate (issue #110): reads/contributions are recorded into
    # a per-session JSON file beside the summaries dir. Wire it the way
    # _set_paths/_init_stores would in production so the read/contribute tools
    # can update it.
    from strata.session_state import SessionStateStore, sessions_dir_for

    mod._sessions_dir = str(sessions_dir_for(summaries_dir))
    mod._session_store = SessionStateStore(mod._sessions_dir)

    return mod


# ---------------------------------------------------------------------------
# _AGENT_SESSION_ID resolution — the module-level constant must route
# through the shared strata.session_state.resolve_agent_session_id fallback
# (issue #112 gap: an unset/empty STRATA_AGENT_SESSION_ID must not collapse
# to an empty session id, and must land on the same id the freshness Stop
# hook computes independently for the same parent pid).
# ---------------------------------------------------------------------------


def test_agent_session_id_falls_back_deterministically_when_unset(
    tmp_path: Path, monkeypatch
) -> None:
    """An unset STRATA_AGENT_SESSION_ID resolves _AGENT_SESSION_ID to the
    shared sess_auto_<parent pid> fallback at import time, matching what
    strata.session_state.resolve_agent_session_id computes for the hook."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    monkeypatch.delenv("STRATA_AGENT_SESSION_ID", raising=False)
    monkeypatch.setattr("os.getppid", lambda: 13131)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    assert mod._AGENT_SESSION_ID == "sess_auto_13131"

    from strata.session_state import resolve_agent_session_id

    # Same computation, same fixed ppid — the pairing the hook relies on.
    assert resolve_agent_session_id({}) == mod._AGENT_SESSION_ID


def test_agent_session_id_treats_empty_string_as_unset(tmp_path: Path, monkeypatch) -> None:
    """Empty string counts as unset — Codex ships literal empty env values —
    so _AGENT_SESSION_ID falls back the same way an absent var does, never
    to an empty-string session id."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    monkeypatch.setenv("STRATA_AGENT_SESSION_ID", "")
    monkeypatch.setattr("os.getppid", lambda: 24242)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    assert mod._AGENT_SESSION_ID == "sess_auto_24242"
    assert mod._AGENT_SESSION_ID != ""


def test_agent_session_id_explicit_value_unchanged(tmp_path: Path, monkeypatch) -> None:
    """An explicit STRATA_AGENT_SESSION_ID is used verbatim — auto-fallback
    never engages when one is set."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    monkeypatch.setenv("STRATA_AGENT_SESSION_ID", "sess_explicit")

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    assert mod._AGENT_SESSION_ID == "sess_explicit"


# ---------------------------------------------------------------------------
# Test 1: strata_contribute writes to RecordStore without HTTP server
# ---------------------------------------------------------------------------


async def test_contribute_writes_to_record_store_without_http(tmp_path: Path) -> None:
    """strata_contribute must append a contribution to RecordStore in-process."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    # Patch _load_fleet to return our test fleet directly (avoids disk path issues).
    fleet = FleetConfig.load(fleet_path)

    # We mock the scope-manager so we don't need a real Anthropic key.
    fake_judgment = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="Valid observation.",
        new_summary=_make_summary("g_arch", "updated context"),
    )

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_AGENT_SKILL", "strata-developer"),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=fake_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        result = await mod.strata_contribute(
            scope_id="g_arch",
            content="All services should use structured logging.",
            proposed_classification="context",
            subject="logging-standard",
            supersedes=None,
        )

    # Result shape matches the existing contract.
    assert "contribution_id" in result
    assert result["judgment"]["decision"] == "accept_as_context"
    assert result["judgment"]["summary_updated"] is True

    # The contribution must be in the RecordStore.
    with RecordStore(db_path) as rs:
        contributions = rs.list_contributions(scope_id="g_arch")
    assert len(contributions) == 1
    assert contributions[0].content == "All services should use structured logging."
    assert contributions[0].contributor.skill == "strata-developer"


# ---------------------------------------------------------------------------
# Test 2: strata_read_scope_summary reads from SummaryStore (file on disk)
# ---------------------------------------------------------------------------


async def test_read_scope_summary_reads_from_summary_store(tmp_path: Path) -> None:
    """strata_read_scope_summary must read the ScopeSummary from disk directly."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    # Seed a summary on disk.
    summary = _make_summary("g_arch", "arch context from disk")
    ss = SummaryStore(summaries_dir)
    ss.write("g_arch", summary)

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_arch"),
    ):
        mod._summary_store = ss
        result = await mod.strata_read_scope_summary("g_arch")

    assert result["scope_id"] == "g_arch"
    assert result["context"] == "arch context from disk"
    assert result["directives"] == []
    assert "updated_at" in result


async def test_read_scope_summary_no_summary_yet_reports_version_zero_and_not_exists(
    tmp_path: Path,
) -> None:
    """Issue #59: a scope with no on-disk summary gets a synthesized empty
    summary that is honest about being synthesized — version=0, exists=False
    — rather than looking identical to a real first write (version=1,
    exists=True).
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_arch"),
    ):
        mod._summary_store = SummaryStore(summaries_dir)
        result = await mod.strata_read_scope_summary("g_arch")

    assert result["version"] == 0
    assert result["exists"] is False


async def test_read_scope_summary_after_first_write_reports_version_one_and_exists(
    tmp_path: Path,
) -> None:
    """Issue #59: once a scope has a real first write, strata_read_scope_summary
    reports version=1, exists=True — distinguishable from the version=0,
    exists=False it would have reported a moment earlier.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    ss = SummaryStore(summaries_dir)
    ss.write("g_arch", _make_summary("g_arch", "arch context"))

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_arch"),
    ):
        mod._summary_store = ss
        result = await mod.strata_read_scope_summary("g_arch")

    assert result["version"] == 1
    assert result["exists"] is True


# ---------------------------------------------------------------------------
# Test 3: strata_read_perspective returns layers in root-first order
# ---------------------------------------------------------------------------


async def test_read_perspective_returns_layers_root_first(tmp_path: Path) -> None:
    """strata_read_perspective returns a layered perspective (Decision 3).

    For g_backend (L1, child of g_arch L0) the perspective has three layers:
    g_arch's directives-only ancestor layer, g_backend's own layer, then
    g_arch's publication layer (ADR 0013 D3 — the chain parent's publication
    composes as a dedicated layer, always wired by this server).
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    ss = SummaryStore(summaries_dir)
    ss.write("g_arch", _make_summary("g_arch", "arch context"))
    ss.write("g_backend", _make_summary("g_backend", "backend context"))

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
    ):
        mod._summary_store = ss
        result = await mod.strata_read_perspective("g_backend")

    assert result["scope_id"] == "g_backend"
    assert result["_layers_count"] == 3
    layers = result["layers"]
    assert len(layers) == 3
    # Root-first ordering: ancestor, self, then the parent's publication layer.
    assert [layer["scope_id"] for layer in layers] == ["g_arch", "g_backend", "g_arch"]
    assert [layer["relation"] for layer in layers] == [
        "ancestor",
        "self",
        "parent_publication",
    ]
    # Ancestor layer: directives only, never a "summary" or "context" key.
    assert "summary" not in layers[0]
    assert layers[0]["directives"] == []
    # Self layer keeps its full summary.
    assert layers[1]["summary"]["context"] == "backend context"
    # The parent's publication layer is honestly empty — nothing published.
    assert layers[2]["publication"] == {"items": []}


async def test_read_perspective_includes_operator_layers_for_bound_chain(tmp_path: Path) -> None:
    """ADR 0008 D2: strata_read_perspective composes operator layers for the agent's chain.

    Agents are never the operator (ADR 0008 D1 — no agent-facing operator MCP
    surface), but they DO read operator memory through the perspective they
    already read, so a judge-consistent view reaches them.
    """
    from strata.operator import operator_publish

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    ss = SummaryStore(summaries_dir)
    ss.write("g_arch", _make_summary("g_arch", "arch context"))
    ss.write("g_backend", _make_summary("g_backend", "backend context"))

    rs = RecordStore(db_path)
    operator_publish(
        "g_arch",
        "All services must use TLS 1.3.",
        "tls",
        record_store=rs,
        summaries_dir=summaries_dir,
    )
    rs.close()

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
    ):
        mod._summary_store = ss
        result = await mod.strata_read_perspective("g_backend")

    layers = result["layers"]
    # Operator layer for g_arch immediately precedes g_arch's own layer;
    # g_backend has no operator memory, so it gets no operator layer; the
    # parent's (g_arch's) publication layer composes last (ADR 0013 D3).
    assert [layer["relation"] for layer in layers] == [
        "operator",
        "ancestor",
        "self",
        "parent_publication",
    ]
    operator_layer = layers[0]
    assert operator_layer["scope_id"] == "g_arch"
    assert operator_layer["stratum_id"] == "operator"
    assert operator_layer["binding"] is True
    assert operator_layer["operator_memory"]["directives"][0]["content"] == (
        "All services must use TLS 1.3."
    )
    assert "context" not in operator_layer["operator_memory"]


# ---------------------------------------------------------------------------
# Test 4: strata_list_scopes re-reads fleet.yaml on each call
# ---------------------------------------------------------------------------


async def test_list_scopes_re_reads_fleet_yaml_each_call(tmp_path: Path) -> None:
    """strata_list_scopes must reflect changes to fleet.yaml between calls."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    # First call — two scopes.
    result1 = mod.strata_list_scopes()
    scope_ids_1 = {s["id"] for s in result1["scopes"]}
    assert "g_arch" in scope_ids_1
    assert "g_backend" in scope_ids_1

    # Mutate fleet.yaml: add a new scope.
    raw = yaml.safe_load(fleet_path.read_text(encoding="utf-8"))
    raw["scopes"].append({"id": "g_frontend", "name": "Frontend Dev", "stratum_id": "L1"})
    fleet_path.write_text(yaml.dump(raw, default_flow_style=False), encoding="utf-8")

    # Second call — must reflect the addition without a restart.
    result2 = mod.strata_list_scopes()
    scope_ids_2 = {s["id"] for s in result2["scopes"]}
    assert "g_frontend" in scope_ids_2, (
        "strata_list_scopes did not pick up fleet.yaml change between calls"
    )


# ---------------------------------------------------------------------------
# Test 5: strata_read_scope_record reads directly from RecordStore
# ---------------------------------------------------------------------------


async def test_read_scope_record_reads_from_record_store(tmp_path: Path) -> None:
    """strata_read_scope_record must return contributions and judgments from RecordStore."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    # Seed a contribution and judgment directly.
    contributor = _make_contributor()
    with RecordStore(db_path) as rs:
        c = rs.append_contribution(
            scope_id="g_arch",
            content="Use WAL mode for SQLite.",
            proposed_classification="directive",
            subject="wal-mode",
            supersedes=None,
            contributor=contributor,
        )
        rs.record_judgment(
            contribution_id=c.id,
            decision="accept_as_directive",
            judged_by="scope-manager",
            notes="Good call.",
        )

    mod._record_store = RecordStore(db_path)

    # Reading the record requires the fleet for the entitlement check
    # (issue #48) — patch _AGENT_SCOPE to the scope under test, which is now
    # the entitled bound scope.
    fleet = FleetConfig.load(fleet_path)
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_arch"),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        result = await mod.strata_read_scope_record("g_arch")

    assert len(result["contributions"]) == 1
    assert result["contributions"][0]["content"] == "Use WAL mode for SQLite."
    assert len(result["judgments"]) == 1
    assert result["judgments"][0]["decision"] == "accept_as_directive"


# ---------------------------------------------------------------------------
# Test 6: strata_contribute raises RuntimeError for unknown scope
# ---------------------------------------------------------------------------


async def test_contribute_raises_for_unknown_scope(tmp_path: Path) -> None:
    """strata_contribute must raise RuntimeError when the scope is not in fleet config."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError, match="Scope not found"),
    ):
        await mod.strata_contribute(
            scope_id="g_nonexistent",
            content="This should fail.",
            proposed_classification="context",
        )


# ---------------------------------------------------------------------------
# Test 7: WAL mode — PRAGMA journal_mode returns 'wal' after RecordStore init
# ---------------------------------------------------------------------------


def test_wal_mode_enabled_after_record_store_init(tmp_path: Path) -> None:
    """RecordStore must enable WAL journal mode on every connection open."""
    db_path = _make_db(tmp_path)

    with RecordStore(db_path) as rs:
        row = rs._conn.execute("PRAGMA journal_mode;").fetchone()
        assert row is not None
        journal_mode = row[0]

    assert journal_mode == "wal", (
        f"Expected journal_mode='wal', got {journal_mode!r}. "
        "Check that RecordStore.__init__ issues PRAGMA journal_mode=WAL."
    )


# ---------------------------------------------------------------------------
# Test 8: L0 root scope returns exactly one layer (itself)
# ---------------------------------------------------------------------------


async def test_perspective_root_scope_returns_one_layer(tmp_path: Path) -> None:
    """strata_read_perspective on a root (L0) scope returns a single layer."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    ss = SummaryStore(summaries_dir)
    ss.write("g_arch", _make_summary("g_arch", "root context"))

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_arch"),
    ):
        mod._summary_store = ss
        result = await mod.strata_read_perspective("g_arch")

    assert result["scope_id"] == "g_arch"
    assert result["_layers_count"] == 1
    assert len(result["layers"]) == 1
    assert result["layers"][0]["scope_id"] == "g_arch"
    assert result["layers"][0]["summary"]["context"] == "root context"


# ---------------------------------------------------------------------------
# Test 9: Deep scope returns N+1 layers (root-first), correct order
# ---------------------------------------------------------------------------


async def test_perspective_deep_scope_returns_layers_root_first(tmp_path: Path) -> None:
    """strata_read_perspective on a 3-level chain returns 3 layers in root-first order."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_deep_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    ss = SummaryStore(summaries_dir)
    ss.write("g_exec", _make_summary("g_exec", "executive context"))
    ss.write("g_func", _make_summary("g_func", "function context"))
    ss.write("g_team", _make_summary("g_team", "team context"))

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = ss
        result = await mod.strata_read_perspective("g_team")

    assert result["scope_id"] == "g_team"
    # Ancestors (directives only), self, then the parent's publication layer
    # (ADR 0013 D3 — always wired by this server).
    assert result["_layers_count"] == 4
    layers = result["layers"]
    assert [layer["scope_id"] for layer in layers] == ["g_exec", "g_func", "g_team", "g_func"]
    assert [layer["relation"] for layer in layers] == [
        "ancestor",
        "ancestor",
        "self",
        "parent_publication",
    ]
    assert "summary" not in layers[0]
    assert "summary" not in layers[1]
    assert layers[2]["summary"]["context"] == "team context"
    assert layers[3]["publication"] == {"items": []}


# ---------------------------------------------------------------------------
# Test 10: an UNREFERENCED peer (intra-stratum, no reference edge) never
# appears — renamed from test_perspective_peer_edges_not_traversed now that
# ADR 0006 D3 composes *referenced* peers as context-only layers (see the
# "ADR 0006 D3/D4" section below for the referenced-peer tests).
# ---------------------------------------------------------------------------


async def test_perspective_unreferenced_peer_never_appears(tmp_path: Path) -> None:
    """A peer scope with no intra-stratum reference edge must not appear in layers.

    The deep fleet has g_peer (L1), a same-stratum scope as g_func with no
    reference edge to or from it. When reading g_team's perspective, g_peer
    must not appear in any layer — composition only ever follows real
    reference edges, never mere sibling-hood (ADR 0006 D3).
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_deep_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    ss = SummaryStore(summaries_dir)
    ss.write("g_exec", _make_summary("g_exec", "executive context"))
    ss.write("g_func", _make_summary("g_func", "function context"))
    ss.write("g_team", _make_summary("g_team", "team context"))
    ss.write("g_peer", _make_summary("g_peer", "peer context — must not appear"))

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = ss
        result = await mod.strata_read_perspective("g_team")

    layer_scope_ids = {layer["scope_id"] for layer in result["layers"]}
    assert "g_peer" not in layer_scope_ids, (
        "Unreferenced peer scope g_peer must not appear in the perspective layers"
    )
    # Exactly the inter-stratum chain: exec, func, team
    assert layer_scope_ids == {"g_exec", "g_func", "g_team"}


# ---------------------------------------------------------------------------
# Test 11: Missing ancestor summary → layer still present with empty content
# ---------------------------------------------------------------------------


async def test_perspective_missing_ancestor_summary_produces_empty_layer(tmp_path: Path) -> None:
    """A scope with no on-disk summary still appears as a layer with empty content."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    # Write only the child summary; leave g_arch (the ancestor) with no file.
    ss = SummaryStore(summaries_dir)
    ss.write("g_backend", _make_summary("g_backend", "backend context"))

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
    ):
        mod._summary_store = ss
        result = await mod.strata_read_perspective("g_backend")

    # Ancestor, self, then the parent's publication layer (ADR 0013 D3).
    assert result["_layers_count"] == 3
    layers = result["layers"]
    # Root layer (g_arch) must be present even though no summary file exists
    # — directives only, empty, never a "summary" or "context" key.
    root_layer = next(layer for layer in layers if layer["relation"] == "ancestor")
    assert root_layer["scope_id"] == "g_arch"
    assert root_layer["directives"] == []
    assert "summary" not in root_layer
    assert "context" not in root_layer

    # The child layer (g_backend) has a real on-disk summary, so it reports
    # a real first write: version=1, exists=True.
    child_layer = next(layer for layer in layers if layer["scope_id"] == "g_backend")
    assert child_layer["summary"]["version"] == 1
    assert child_layer["summary"]["exists"] is True

    # The parent's publication layer is present too, honestly empty.
    parent_pub_layer = next(layer for layer in layers if layer["relation"] == "parent_publication")
    assert parent_pub_layer["scope_id"] == "g_arch"
    assert parent_pub_layer["publication"] == {"items": []}


# ---------------------------------------------------------------------------
# Test 12: _v1_limitation key is absent (regression guard)
# ---------------------------------------------------------------------------


async def test_perspective_no_v1_limitation_key(tmp_path: Path) -> None:
    """strata_read_perspective must NOT include the _v1_limitation key."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_arch"),
    ):
        mod._summary_store = SummaryStore(summaries_dir)
        result = await mod.strata_read_perspective("g_arch")

    assert "_v1_limitation" not in result, (
        "_v1_limitation must be removed now that real perspective composition is implemented"
    )


# ---------------------------------------------------------------------------
# ADR 0013 D2/D3 — one-hop publication composition (amends ADR 0006 D3)
#
# strata_read_perspective appends exactly one publication layer for the
# chain parent (relation="parent_publication") and one per scope the
# REQUESTED SCOPE ITSELF references (relation="peer_reference") — never a
# grandparent's publication, and never a publication reached only through an
# ancestor's own reference edge. Self/ancestor layers keep
# relation="self"/"ancestor" and binding=True; every publication layer is
# binding=False. Uses _make_peer_composition_fleet_yaml: g_exec (L0) <-
# g_func (L1) <- g_team (L2), with g_team referencing g_team_peer (its own —
# composes), g_func referencing g_peer_a/g_peer_b (the PARENT's own — does
# not compose for g_team), g_exec referencing g_exec_peer (the ROOT's own —
# does not compose for g_team either), g_peer_a referencing g_peer_of_peer
# (peer-of-peer, never composes), and g_sibling as an unreferenced L1 scope.
# ---------------------------------------------------------------------------


async def test_perspective_self_and_ancestor_layers_are_binding(tmp_path: Path) -> None:
    """Self and ancestor layers carry relation="self"/"ancestor" and binding=True."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = SummaryStore(summaries_dir)
        result = await mod.strata_read_perspective("g_team")

    chain_layers = {
        layer["scope_id"]: layer
        for layer in result["layers"]
        if layer["relation"] in ("ancestor", "self")
    }
    assert chain_layers["g_exec"]["relation"] == "ancestor"
    assert chain_layers["g_exec"]["binding"] is True
    assert chain_layers["g_func"]["relation"] == "ancestor"
    assert chain_layers["g_func"]["binding"] is True
    assert chain_layers["g_team"]["relation"] == "self"
    assert chain_layers["g_team"]["binding"] is True


async def test_perspective_own_reference_appears_as_publication_layer(tmp_path: Path) -> None:
    """A peer referenced by the REQUESTED SCOPE ITSELF appears, relation="peer_reference".

    ADR 0013 D2/D3: the peer layer carries that peer's PUBLICATION, not its
    internal summary — writing only a summary (no publication artifact)
    leaves the layer with an honestly empty ``publication.items`` list.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    ss = SummaryStore(summaries_dir)
    ss.write("g_team_peer", _make_summary("g_team_peer", "team peer context"))

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = ss
        result = await mod.strata_read_perspective("g_team")

    peer_layer = next(layer for layer in result["layers"] if layer["scope_id"] == "g_team_peer")
    assert peer_layer["relation"] == "peer_reference"
    assert peer_layer["binding"] is False
    # Never the peer's internal summary — no "summary" key at all.
    assert "summary" not in peer_layer
    assert peer_layer["publication"] == {"items": []}


async def test_perspective_own_reference_publication_composed_verbatim(tmp_path: Path) -> None:
    """A referenced peer's PUBLISHED items compose into the peer layer, verbatim and labelled."""
    from strata.publication import PublishedItem, _write_publication

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    ss = SummaryStore(summaries_dir)
    ss.write(
        "g_team_peer", _make_summary("g_team_peer", "team peer internal context — must NOT appear")
    )
    _write_publication(
        "g_team_peer",
        [
            PublishedItem(
                id="pub_a1",
                kind="context",
                content="Team Peer's outward status update.",
                subject="status",
                anchors=["subject:status"],
                published_at="2026-07-12T00:00:00+00:00",
            )
        ],
        summaries_dir=summaries_dir,
    )

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = ss
        result = await mod.strata_read_perspective("g_team")

    peer_layer = next(layer for layer in result["layers"] if layer["scope_id"] == "g_team_peer")
    assert peer_layer["publication"]["items"] == [
        {
            "id": "pub_a1",
            "kind": "context",
            "content": "Team Peer's outward status update.",
            "subject": "status",
            "anchors": ["subject:status"],
            "published_at": "2026-07-12T00:00:00+00:00",
        }
    ]
    # The internal summary's content never leaks into the composed layer.
    rendered = str(result)
    assert "must NOT appear" not in rendered


async def test_perspective_parent_publication_layer_composed(tmp_path: Path) -> None:
    """The chain PARENT's publication composes as a dedicated layer (ADR 0013 D3)."""
    from strata.publication import PublishedItem, _write_publication

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    _write_publication(
        "g_func",
        [
            PublishedItem(
                id="pub_func1",
                kind="context",
                content="Function's outward face.",
                subject=None,
                anchors=[],
                published_at="2026-07-12T00:00:00+00:00",
            )
        ],
        summaries_dir=summaries_dir,
    )

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = SummaryStore(summaries_dir)
        result = await mod.strata_read_perspective("g_team")

    parent_pub_layer = next(
        layer for layer in result["layers"] if layer["relation"] == "parent_publication"
    )
    assert parent_pub_layer["scope_id"] == "g_func"
    assert parent_pub_layer["binding"] is False
    assert parent_pub_layer["publication"]["items"][0]["content"] == "Function's outward face."


async def test_perspective_ancestor_referenced_peer_does_not_appear(tmp_path: Path) -> None:
    """A peer referenced by an ANCESTOR (not the requested scope itself) does NOT appear.

    ADR 0013 D3: publication travels exactly one edge. g_exec_peer is
    referenced by g_exec (the root, two strata up from g_team) — that is
    g_exec's own reference edge, not g_team's, so it never reaches g_team.
    Same for g_peer_a/g_peer_b, referenced by g_func (g_team's parent).
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    ss = SummaryStore(summaries_dir)
    ss.write("g_exec_peer", _make_summary("g_exec_peer", "exec peer context"))

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = ss
        result = await mod.strata_read_perspective("g_team")

    layer_scope_ids = {layer["scope_id"] for layer in result["layers"]}
    assert "g_exec_peer" not in layer_scope_ids
    assert "g_peer_a" not in layer_scope_ids
    assert "g_peer_b" not in layer_scope_ids

    # g_func's OWN perspective, though, does receive both — one hop from
    # g_func itself.
    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_func"),
    ):
        mod._summary_store = ss
        func_result = await mod.strata_read_perspective("g_func")
    func_layer_ids = {layer["scope_id"] for layer in func_result["layers"]}
    assert "g_peer_a" in func_layer_ids
    assert "g_peer_b" in func_layer_ids


async def test_perspective_peer_of_peer_not_traversed(tmp_path: Path) -> None:
    """Only one hop is followed — a peer's own peer reference is not composed in."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_func"),
    ):
        mod._summary_store = SummaryStore(summaries_dir)
        result = await mod.strata_read_perspective("g_func")

    layer_scope_ids = {layer["scope_id"] for layer in result["layers"]}
    assert "g_peer_of_peer" not in layer_scope_ids


async def test_perspective_unreferenced_sibling_absent_alongside_referenced_peers(
    tmp_path: Path,
) -> None:
    """An unreferenced sibling never appears, even in a fleet with referenced peers."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = SummaryStore(summaries_dir)
        result = await mod.strata_read_perspective("g_team")

    layer_scope_ids = {layer["scope_id"] for layer in result["layers"]}
    assert "g_sibling" not in layer_scope_ids


async def test_perspective_peer_without_publication_reports_honestly_empty_face(
    tmp_path: Path,
) -> None:
    """A referenced peer with nothing published gets an honestly empty face (ADR 0007 D4)."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = SummaryStore(summaries_dir)  # no summaries written anywhere
        result = await mod.strata_read_perspective("g_team")

    peer_layer = next(layer for layer in result["layers"] if layer["scope_id"] == "g_team_peer")
    assert "summary" not in peer_layer
    assert peer_layer["publication"] == {"items": []}


async def test_perspective_peer_layers_sorted_by_scope_id(tmp_path: Path) -> None:
    """Peer layers are ordered by scope id for deterministic output, after self/ancestors/parent."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_func"),
    ):
        mod._summary_store = SummaryStore(summaries_dir)
        result = await mod.strata_read_perspective("g_func")

    layers = result["layers"]
    # Chain first (root-first: g_exec, g_func-self), then g_exec's
    # parent_publication layer, then g_func's own references sorted by
    # scope id: g_peer_a, g_peer_b.
    assert [layer["scope_id"] for layer in layers] == [
        "g_exec",
        "g_func",
        "g_exec",
        "g_peer_a",
        "g_peer_b",
    ]
    assert layers[2]["relation"] == "parent_publication"
    peer_relations = [layer["relation"] for layer in layers[3:]]
    assert peer_relations == ["peer_reference", "peer_reference"]


# ---------------------------------------------------------------------------
# ADR 0006 D4 — read surface reconciliation
#
# strata_read_scope_summary widens to the context surface (chain + peers
# referenced by that chain); strata_read_scope_record and the
# strata_read_perspective *target* stay chain-only. Uses the same
# peer-composition fleet as the D3 tests above.
# ---------------------------------------------------------------------------


async def test_summary_read_of_referenced_peer_succeeds(tmp_path: Path) -> None:
    """strata_read_scope_summary succeeds for a peer referenced by the caller's chain.

    ADR 0007 D4: the entitled content for a peer is its PUBLICATION, not its
    internal summary — writing only a summary produces an honestly empty
    face, not the summary's content.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    ss = SummaryStore(summaries_dir)
    ss.write("g_peer_a", _make_summary("g_peer_a", "peer a context"))

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = ss
        result = await mod.strata_read_scope_summary("g_peer_a")

    assert result["scope_id"] == "g_peer_a"
    assert result["relation"] == "peer_reference"
    assert result["publication"] == {"items": []}
    assert "context" not in result


async def test_summary_read_of_referenced_peer_returns_its_publication(tmp_path: Path) -> None:
    """strata_read_scope_summary on a referenced peer returns its PUBLISHED items, verbatim."""
    from strata.publication import PublishedItem, _write_publication

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    _write_publication(
        "g_peer_a",
        [
            PublishedItem(
                id="pub_a1",
                kind="directive",
                content="Peer A's published directive (non-binding to us).",
                subject=None,
                anchors=["directive:c_x1"],
                published_at="2026-07-12T00:00:00+00:00",
            )
        ],
        summaries_dir=summaries_dir,
    )

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = SummaryStore(summaries_dir)
        result = await mod.strata_read_scope_summary("g_peer_a")

    assert result["scope_id"] == "g_peer_a"
    assert result["relation"] == "peer_reference"
    assert result["publication"]["items"][0]["id"] == "pub_a1"
    assert result["publication"]["items"][0]["content"] == (
        "Peer A's published directive (non-binding to us)."
    )


async def test_summary_read_of_unreferenced_sibling_still_refused(tmp_path: Path) -> None:
    """strata_read_scope_summary still refuses an unreferenced sibling scope."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
        pytest.raises(RuntimeError, match="entitled context surface") as exc_info,
    ):
        await mod.strata_read_scope_summary("g_sibling")

    message = str(exc_info.value)
    assert "g_sibling" in message
    assert "g_team" in message


async def test_record_read_of_referenced_peer_refused_chain_only(tmp_path: Path) -> None:
    """strata_read_scope_record refuses a referenced peer — records stay chain-only."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
        pytest.raises(RuntimeError, match="entitled surface") as exc_info,
    ):
        await mod.strata_read_scope_record("g_peer_a")

    message = str(exc_info.value)
    assert "g_peer_a" in message
    assert "chain-only" in message


async def test_perspective_target_of_referenced_peer_refused(tmp_path: Path) -> None:
    """A referenced peer is still refused as a perspective TARGET (ADR 0006 D4)."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
        pytest.raises(RuntimeError, match="entitled surface") as exc_info,
    ):
        await mod.strata_read_perspective("g_peer_a")

    message = str(exc_info.value)
    assert "g_peer_a" in message


# ---------------------------------------------------------------------------
# Issue #48 — entitlement-scoped reads
#
# Chain-only entitled surface = bound scope (_AGENT_SCOPE) + its inter-stratum
# ancestors, used for records and perspective targets. Scope summary reads
# widen to the context surface (ADR 0006 D3/D4 — see the section above).
# Uses the deep fleet: g_exec (L0) <- g_func (L1) <- g_team (L2), with g_peer
# as an unreferenced L1 sibling of g_func (NOT an ancestor of g_team, and not
# referenced by any scope on g_team's chain).
# ---------------------------------------------------------------------------


async def test_entitled_no_argument_returns_bound_scope_data(tmp_path: Path) -> None:
    """Calling read tools with no scope_id defaults to the agent's bound scope."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_deep_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    ss = SummaryStore(summaries_dir)
    ss.write("g_team", _make_summary("g_team", "team context"))
    ss.write("g_func", _make_summary("g_func", "function context"))
    ss.write("g_exec", _make_summary("g_exec", "executive context"))

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = ss
        summary_result = await mod.strata_read_scope_summary()
        perspective_result = await mod.strata_read_perspective()
        record_result = await mod.strata_read_scope_record()

    assert summary_result["scope_id"] == "g_team"
    assert summary_result["context"] == "team context"

    assert perspective_result["scope_id"] == "g_team"
    self_layer = next(
        layer for layer in perspective_result["layers"] if layer["relation"] == "self"
    )
    assert self_layer["scope_id"] == "g_team"

    assert record_result == {
        "contributions": [],
        "judgments": [],
        "judgment_attempts": [],
        "contribution_states": [],
        # The record read is bounded by default (issue #130); an empty record
        # still reports its page, so "empty" and "first page of many" are
        # never confused.
        "page": {"limit": mod._settings.record_page_size, "total": 0, "next_before_id": None},
    }


async def test_entitled_ancestor_read_allowed(tmp_path: Path) -> None:
    """Reading an inter-stratum ancestor of the bound scope is allowed, directives only.

    A chain edge carries only the ancestor's directives — a direct read of
    an ancestor scope must not leak its context either, or this tool would
    reopen exactly the leak perspective composition closes.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_deep_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    ss = SummaryStore(summaries_dir)
    ss.write(
        "g_exec",
        _make_summary("g_exec", "executive context — must never leak through a direct read"),
    )

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = ss
        result = await mod.strata_read_scope_summary("g_exec")

    assert result["scope_id"] == "g_exec"
    assert result["relation"] == "ancestor"
    assert result["binding"] is True
    assert result["directives"] == []
    assert "context" not in result
    assert "summary" not in result
    assert "must never leak" not in str(result)


async def test_entitled_ancestor_read_carries_directives(tmp_path: Path) -> None:
    """An ancestor's directives DO reach a direct read — only its context is withheld."""
    from strata.summary_store import Directive

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_deep_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    ss = SummaryStore(summaries_dir)
    ss.write(
        "g_exec",
        ScopeSummary(
            scope_id="g_exec",
            directives=[
                Directive(
                    id="c_root1",
                    content="Root directive — binds everyone.",
                    subject=None,
                    source_scope_id="g_exec",
                    source_skill="architect",
                    created_at="2026-07-12T00:00:00+00:00",
                )
            ],
            context="executive context",
            updated_at="2026-07-12T00:00:00+00:00",
        ),
    )

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = ss
        result = await mod.strata_read_scope_summary("g_exec")

    assert [d["id"] for d in result["directives"]] == ["c_root1"]
    assert result["directives"][0]["content"] == "Root directive — binds everyone."


async def test_entitled_peer_read_raises_with_entitlement_message(tmp_path: Path) -> None:
    """Reading an unreferenced peer (intra-stratum, non-ancestor) scope raises RuntimeError.

    g_peer in the deep fleet has no reference edge to or from g_func — an
    unreferenced sibling, refused under both the context surface (summary
    reads, ADR 0006 D3/D4) and the chain-only surface (perspective target
    and record reads).
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_deep_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    ss = SummaryStore(summaries_dir)
    ss.write("g_peer", _make_summary("g_peer", "peer context"))

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = ss
        with pytest.raises(RuntimeError, match="entitled context surface") as exc_info:
            await mod.strata_read_scope_summary("g_peer")

    message = str(exc_info.value)
    assert "g_peer" in message
    assert "g_team" in message

    # The chain-only surface refuses the same unreferenced peer for
    # perspective targets and record reads.
    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        with pytest.raises(RuntimeError, match="entitled surface"):
            await mod.strata_read_perspective("g_peer")
        with pytest.raises(RuntimeError, match="entitled surface"):
            await mod.strata_read_scope_record("g_peer")


async def test_entitled_own_empty_record_returns_empty_shape(tmp_path: Path) -> None:
    """Reading the bound scope's own record with no rows yet returns the empty shape."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_deep_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        result = await mod.strata_read_scope_record("g_team")

    assert result == {
        "contributions": [],
        "judgments": [],
        "judgment_attempts": [],
        "contribution_states": [],
        # Bounded by default (issue #130) — the empty shape now names its page.
        "page": {"limit": mod._settings.record_page_size, "total": 0, "next_before_id": None},
    }


# ---------------------------------------------------------------------------
# Entitlement edge cases (release-review findings)
# ---------------------------------------------------------------------------


async def test_descendant_read_is_denied(tmp_path: Path) -> None:
    """The entitled surface is self + ANCESTORS — descendants are not readable.

    Scope summary reads go through the wider context surface (ADR 0006 D3/
    D4), but that surface still never includes descendants — only chain +
    chain-referenced peers.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)
    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_arch"),  # the L0 parent
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError, match="entitled context surface"),
    ):
        await mod.strata_read_scope_summary("g_backend")  # its L1 child


async def test_stale_bound_scope_gets_distinct_error(tmp_path: Path) -> None:
    """Bound scope removed from fleet.yaml mid-session → rebind error, not a peer error."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)
    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_removed"),  # not in the fleet
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError, match="no longer exists in the fleet"),
    ):
        await mod.strata_read_perspective()


# ---------------------------------------------------------------------------
# ADR 0006 Decision D1 — entitled write-target surface
#
# strata_contribute must refuse any target scope outside the bound scope
# (_AGENT_SCOPE) plus its inter-stratum ancestors — the same surface shape as
# the #48 read surface, but a separate named concept (_check_entitled_write)
# with its own error message. Uses the write-surface fleet: g_exec (L0) <-
# g_func (L1) <- g_team (L2), with g_team2 a sibling of g_team and g_archived
# an archived sibling.
# ---------------------------------------------------------------------------


def _patch_agent_binding(
    mod, *, scope: str, skill: str = "strata-developer", session_id: str = "sess_test"
):
    """Return the three patch context managers used to bind an agent identity in tests."""
    return (
        patch.object(mod, "_AGENT_SCOPE", scope),
        patch.object(mod, "_AGENT_SKILL", skill),
        patch.object(mod, "_AGENT_SESSION_ID", session_id),
    )


def _seeded_pending_switch(mod, target_scope_id: str):
    """Patch context manager pre-seeding mod._PENDING_SWITCH as though
    *target_scope_id* was already announced, just now — so a single
    strata_bind(scope_id=target_scope_id, confirm=True) call in the test
    body performs the switch immediately, standing in for the real
    announce-then-confirm round trip when a test's focus is elsewhere
    (provenance, fleet hot-reload, session id stability, ...) and not the
    two-step enforcement itself."""
    return patch.object(
        mod,
        "_PENDING_SWITCH",
        mod._PendingSwitch(target_scope_id=target_scope_id, requested_at=time.monotonic()),
    )


@pytest.mark.parametrize(
    "target_scope_id",
    ["g_team", "g_func", "g_exec"],
    ids=["own-scope", "parent", "root-grandparent"],
)
async def test_contribute_within_write_surface_allowed(
    tmp_path: Path, target_scope_id: str
) -> None:
    """Own scope, parent, and root/grandparent are all within the write surface."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_write_surface_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    fake_judgment = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="Valid observation.",
        new_summary=_make_summary(target_scope_id, "updated context"),
    )

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_team")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=fake_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        result = await mod.strata_contribute(
            scope_id=target_scope_id,
            content="within the write surface",
            proposed_classification="context",
        )

    assert result["judgment"]["decision"] == "accept_as_context"
    with RecordStore(db_path) as rs:
        contributions = rs.list_contributions(scope_id=target_scope_id)
    assert len(contributions) == 1
    assert contributions[0].content == "within the write surface"


async def test_contribute_to_sibling_refused(tmp_path: Path) -> None:
    """A direct write into a peer (sibling) scope is refused (ADR 0006 D1).

    Sideways knowledge flow has exactly two sanctioned routes: ratification
    into a common ancestor, or a context-only reference edge — never a
    direct write.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_write_surface_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_team")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError, match="entitled write surface") as exc_info,
    ):
        await mod.strata_contribute(
            scope_id="g_team2",
            content="sideways contribution",
            proposed_classification="context",
        )

    message = str(exc_info.value)
    assert "g_team2" in message
    assert "g_team" in message


async def test_contribute_to_descendant_refused(tmp_path: Path) -> None:
    """A direct write into a descendant scope is refused (ADR 0006 D1).

    Authority already flows down structurally: publish at your own scope and
    it binds every descendant. A direct write into a child scope bypasses
    that scope's own judgment loop.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_write_surface_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_func")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError, match="entitled write surface") as exc_info,
    ):
        await mod.strata_contribute(
            scope_id="g_team",
            content="downward contribution",
            proposed_classification="context",
        )

    message = str(exc_info.value)
    assert "g_team" in message
    assert "g_func" in message


async def test_refused_write_leaves_no_record_row(tmp_path: Path) -> None:
    """A structurally-refused write must not append a contribution or judgment row.

    ADR 0006 D1: a structural refusal is an error, not a scope-manager
    decline — the record is the log of judged contributions, not of
    tool-call rejections.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_write_surface_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with RecordStore(db_path) as rs:
        assert rs.list_contributions(scope_id="g_team2") == []
        assert rs.list_judgments(scope_id="g_team2") == []

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_team")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError, match="entitled write surface"),
    ):
        await mod.strata_contribute(
            scope_id="g_team2",
            content="sideways contribution",
            proposed_classification="context",
        )

    with RecordStore(db_path) as rs:
        assert rs.list_contributions(scope_id="g_team2") == []
        assert rs.list_judgments(scope_id="g_team2") == []


async def test_refused_write_emits_warning_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A refused write emits one WARNING log line naming contributor and target.

    Grill decision (ADR 0006 D1): every refusal is logged (contributor
    scope/skill/session, target scope) for tracing and auditing without
    polluting the scope's record.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_write_surface_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    scope_p, skill_p, session_p = _patch_agent_binding(
        mod, scope="g_team", skill="strata-developer", session_id="sess_test"
    )
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        caplog.at_level(logging.WARNING, logger="strata.mcp"),
        pytest.raises(RuntimeError, match="entitled write surface"),
    ):
        await mod.strata_contribute(
            scope_id="g_team2",
            content="sideways contribution",
            proposed_classification="context",
        )

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1
    message = warning_records[0].getMessage()
    assert "g_team" in message
    assert "strata-developer" in message
    assert "sess_test" in message
    assert "g_team2" in message


async def test_contribute_raises_for_unknown_scope_before_entitlement_check(tmp_path: Path) -> None:
    """Scope-not-found errors are unchanged and reported before the entitlement check runs."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_write_surface_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_team")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError, match="Scope not found"),
    ):
        await mod.strata_contribute(
            scope_id="g_nonexistent",
            content="This should fail.",
            proposed_classification="context",
        )


async def test_contribute_raises_for_archived_scope_before_entitlement_check(
    tmp_path: Path,
) -> None:
    """Archived-scope errors are unchanged and reported before the entitlement check runs.

    g_archived is a sibling of g_team (not in g_team's write surface), so this
    also pins that the archived check fires first — fleet topology is not
    secret (strata_list_scopes is open), so existence checks may stay first.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_write_surface_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_team")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError, match="archived") as exc_info,
    ):
        await mod.strata_contribute(
            scope_id="g_archived",
            content="This should fail.",
            proposed_classification="context",
        )

    # Must be the archived-scope error, not the write-entitlement error.
    assert "entitled write surface" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# ADR 0006 Decision D2 — the judge gets an entitlement signal
# ---------------------------------------------------------------------------


async def test_contribute_passes_entitlement_view_to_judge(tmp_path: Path) -> None:
    """strata_contribute must compute and pass a non-None entitlement view to judge."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    fake_judgment = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="Valid observation.",
        new_summary=_make_summary("g_arch", "updated context"),
    )

    judge_spy = MagicMock(return_value=fake_judgment)

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_AGENT_SKILL", "strata-developer"),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", judge_spy),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        await mod.strata_contribute(
            scope_id="g_arch",
            content="All services should use structured logging.",
            proposed_classification="context",
            subject="logging-standard",
            supersedes=None,
        )

    assert judge_spy.call_count == 1
    passed_entitlement = judge_spy.call_args.kwargs["entitlement"]
    expected_entitlement = fleet.entitlement_view("g_arch")

    assert passed_entitlement is not None
    assert {s.id for s in passed_entitlement.chain} == {s.id for s in expected_entitlement.chain}
    assert {s.id for s in passed_entitlement.referenced_peers} == {
        s.id for s in expected_entitlement.referenced_peers
    }
    assert {s.id for s in passed_entitlement.others} == {s.id for s in expected_entitlement.others}


# ---------------------------------------------------------------------------
# Issue #57 — judge-failure recovery through the MCP surface
#
# strata_contribute on a judge() failure records a judgment-attempt-failed
# event (never a verdict), leaves no judgment, and raises an error carrying the
# contribution id and naming strata_rejudge as the retry path. strata_rejudge
# then recovers the pending contribution idempotently.
# ---------------------------------------------------------------------------


async def test_contribute_judge_failure_records_attempt_and_points_to_rejudge(
    tmp_path: Path,
) -> None:
    """A scope-manager failure records the contribution + an attempt event, no
    judgment, and the raised error carries the contribution id + strata_rejudge.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_AGENT_SKILL", "strata-developer"),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", side_effect=ValueError("LLM down")),
        patch("anthropic.Anthropic", return_value=MagicMock()),
        pytest.raises(RuntimeError) as exc_info,
    ):
        await mod.strata_contribute(
            scope_id="g_backend",
            content="contribution before the crash",
            proposed_classification="context",
        )

    message = str(exc_info.value)
    assert "strata_rejudge" in message
    assert "ValueError" in message

    with RecordStore(db_path) as rs:
        contributions = rs.list_contributions(scope_id="g_backend")
        judgments = rs.list_judgments(scope_id="g_backend")
        attempts = rs.list_judgment_attempts(scope_id="g_backend")

    assert len(contributions) == 1
    # The error names the contribution id so a retry can route to re-judge.
    assert contributions[0].id in message
    assert judgments == []
    assert len(attempts) == 1
    assert attempts[0].error_class == "ValueError"
    assert attempts[0].contribution_id == contributions[0].id
    # The pending contribution reached no reader: no summary was written.
    assert SummaryStore(summaries_dir).read("g_backend") is None


async def test_strata_rejudge_recovers_pending_then_idempotent(tmp_path: Path) -> None:
    """strata_rejudge judges a pending contribution against the current summary
    and appends exactly one judgment; a second call is a no-op (idempotent).
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend")

    # 1. A judge() failure leaves a pending contribution.
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", side_effect=ValueError("outage")),
        patch("anthropic.Anthropic", return_value=MagicMock()),
        pytest.raises(RuntimeError),
    ):
        await mod.strata_contribute(
            scope_id="g_backend",
            content="recover me",
            proposed_classification="context",
        )

    with RecordStore(db_path) as rs:
        contribution_id = rs.list_contributions(scope_id="g_backend")[0].id
        assert rs.list_judgments(scope_id="g_backend") == []

    # 2. First re-judge: the scope-manager is back — it judges and updates state.
    good_judgment = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="recovered",
        new_summary=_make_summary("g_backend", "recovered context"),
    )

    scope_p2, skill_p2, session_p2 = _patch_agent_binding(mod, scope="g_backend")
    with (
        scope_p2,
        skill_p2,
        session_p2,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=good_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        result = await mod.strata_rejudge(contribution_id)

    assert result["contribution_id"] == contribution_id
    assert result["judgment"]["decision"] == "accept_as_context"
    assert result["judgment"]["summary_updated"] is True
    with RecordStore(db_path) as rs:
        assert len(rs.list_judgments(scope_id="g_backend")) == 1

    # 3. Second re-judge: a verdict exists → no-op. The scope-manager must NOT
    # be invoked (a raising judge proves the short-circuit) and no second
    # judgment is written.
    scope_p3, skill_p3, session_p3 = _patch_agent_binding(mod, scope="g_backend")
    with (
        scope_p3,
        skill_p3,
        session_p3,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch(
            "strata.scope_manager.ScopeManager.judge",
            side_effect=AssertionError("re-judge must not judge when a verdict exists"),
        ),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        result2 = await mod.strata_rejudge(contribution_id)

    assert result2["judgment"]["decision"] == "accept_as_context"
    assert result2["judgment"]["summary_updated"] is False
    with RecordStore(db_path) as rs:
        assert len(rs.list_judgments(scope_id="g_backend")) == 1


async def test_strata_rejudge_unknown_contribution_raises(tmp_path: Path) -> None:
    """strata_rejudge on an unknown contribution id raises RuntimeError."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError, match="Contribution not found"),
    ):
        await mod.strata_rejudge("c_does_not_exist")


# ---------------------------------------------------------------------------
# ADR 0007 D2 — Tools: strata_publish / strata_withdraw
#
# Own-scope-only publishing is structural: neither tool takes a scope_id
# parameter — they always act on STRATA_AGENT_SCOPE.
# ---------------------------------------------------------------------------


def _seed_summary_with_directive(ss: SummaryStore, scope_id: str, directive_id: str) -> None:
    from strata.summary_store import Directive

    ss.write(
        scope_id,
        ScopeSummary(
            scope_id=scope_id,
            directives=[
                Directive(
                    id=directive_id,
                    content="Use protobuf for all RPC.",
                    subject="rpc",
                    source_scope_id=scope_id,
                    source_skill="strata-developer",
                    created_at="2026-07-12T00:00:00+00:00",
                )
            ],
            context="",
            updated_at="2026-07-12T00:00:00+00:00",
        ),
    )


async def test_strata_publish_acts_on_bound_scope_with_own_provenance(tmp_path: Path) -> None:
    """strata_publish always targets STRATA_AGENT_SCOPE and stamps the agent's own provenance."""
    from strata.scope_manager import PublicationJudgment

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    ss = SummaryStore(summaries_dir)
    _seed_summary_with_directive(ss, "g_backend", "c_dir1")

    fake_judgment = PublicationJudgment(decision="accept", reasoning="Fit for export.")

    scope_p, skill_p, session_p = _patch_agent_binding(
        mod, scope="g_backend", skill="strata-developer", session_id="sess_pub"
    )
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge_publication", return_value=fake_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        mod._summary_store = ss
        result = await mod.strata_publish(
            content="Use protobuf for all RPC.",
            kind="directive",
            anchors=["c_dir1"],
            subject="rpc-protocol",
        )

    assert result["judgment"]["decision"] == "accept"
    assert result["judgment"]["artifact_updated"] is True

    with RecordStore(db_path) as rs:
        acts = rs.list_publication_acts(scope_id="g_backend")
        assert len(acts) == 1
        act = acts[0]
        assert act.proposer.scope_id == "g_backend"
        assert act.proposer.skill == "strata-developer"
        assert act.proposer.session_id == "sess_pub"

    from strata.publication import read_publication

    items = read_publication("g_backend", summaries_dir=summaries_dir)
    assert len(items) == 1
    assert items[0].content == "Use protobuf for all RPC."


def test_strata_publish_no_scope_id_parameter_exists() -> None:
    """strata_publish's signature has no scope_id — own-scope-only publishing is structural."""
    import inspect

    import strata.mcp.server as mod

    params = inspect.signature(mod.strata_publish).parameters
    assert "scope_id" not in params


async def test_strata_publish_zero_anchors_raises_runtimeerror(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    ss = SummaryStore(summaries_dir)
    _seed_summary_with_directive(ss, "g_backend", "c_dir1")

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError, match="at least one anchor"),
    ):
        mod._summary_store = ss
        await mod.strata_publish(content="x", kind="context", anchors=[])

    with RecordStore(db_path) as rs:
        assert rs.list_publication_acts(scope_id="g_backend") == []


async def test_strata_withdraw_acts_on_bound_scope_with_own_provenance(tmp_path: Path) -> None:
    """strata_withdraw always targets STRATA_AGENT_SCOPE's own publication."""
    from strata.publication import PublishedItem, _write_publication, read_publication
    from strata.scope_manager import PublicationJudgment

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with RecordStore(db_path) as rs:
        act = rs.append_publication_act(
            scope_id="g_backend",
            act="publish",
            kind="context",
            content="Stale status.",
            subject=None,
            anchors=["subject:status"],
            withdraws=None,
            trigger=None,
            proposer=ContributorRef(
                scope_id="g_backend", skill="strata-developer", session_id="s1", ts="t"
            ),
        )
        rs.record_publication_judgment(
            act_id=act.id, decision="accept", judged_by="scope-manager", reasoning="seeded"
        )
    _write_publication(
        "g_backend",
        [
            PublishedItem(
                id=act.id,
                kind="context",
                content="Stale status.",
                subject=None,
                anchors=["subject:status"],
                published_at=act.created_at,
            )
        ],
        summaries_dir=summaries_dir,
    )

    fake_judgment = PublicationJudgment(decision="accept", reasoning="No longer accurate.")

    scope_p, skill_p, session_p = _patch_agent_binding(
        mod, scope="g_backend", skill="strata-developer", session_id="sess_wd"
    )
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge_publication", return_value=fake_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        mod._summary_store = SummaryStore(summaries_dir)
        result = await mod.strata_withdraw(act.id)

    assert result["judgment"]["decision"] == "accept"
    assert result["judgment"]["artifact_updated"] is True
    assert read_publication("g_backend", summaries_dir=summaries_dir) == []

    with RecordStore(db_path) as rs:
        acts = rs.list_publication_acts(scope_id="g_backend")
        withdraw_act = next(a for a in acts if a.act == "withdraw")
        assert withdraw_act.proposer.scope_id == "g_backend"
        assert withdraw_act.proposer.session_id == "sess_wd"


async def test_strata_withdraw_unknown_item_raises_runtimeerror(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError, match="not found"),
    ):
        mod._summary_store = SummaryStore(summaries_dir)
        await mod.strata_withdraw("pub_does_not_exist")


# ---------------------------------------------------------------------------
# Issue #110: per-session asymmetry counters + read receipts (mechanical)
# ---------------------------------------------------------------------------


async def test_read_scope_summary_increments_session_reads(tmp_path: Path) -> None:
    """A summary read increments the session's reads counter and per-scope receipt."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    SummaryStore(summaries_dir).write("g_arch", _make_summary("g_arch", "ctx"))
    fleet = FleetConfig.load(fleet_path)

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend", session_id="sess_r")
    with scope_p, skill_p, session_p, patch.object(mod, "_load_fleet", return_value=fleet):
        await mod.strata_read_scope_summary("g_arch")
        await mod.strata_read_scope_summary("g_arch")

    # The state file exists, is readable, and records both reads of g_arch.
    state = mod._session_store.read("sess_r")
    assert state is not None
    assert state.reads == 2
    assert state.contributions == 0
    assert state.declines == 0
    assert state.reads_by_scope["g_arch"].count == 2
    assert state.reads_by_scope["g_arch"].last_read_at != ""


async def test_read_perspective_records_read_for_target_scope(tmp_path: Path) -> None:
    """A perspective read is attributed to its target scope only, not its ancestors."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend", session_id="sess_p")
    with scope_p, skill_p, session_p, patch.object(mod, "_load_fleet", return_value=fleet):
        await mod.strata_read_perspective("g_backend")

    state = mod._session_store.read("sess_p")
    assert state is not None
    assert state.reads == 1
    # g_backend is the target; g_arch (its ancestor layer) is NOT attributed a read.
    assert set(state.reads_by_scope) == {"g_backend"}


async def test_session_stats_tool_returns_counters(tmp_path: Path) -> None:
    """strata_session_stats returns the live counters; zeroed before any activity."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    SummaryStore(summaries_dir).write("g_arch", _make_summary("g_arch", "ctx"))
    fleet = FleetConfig.load(fleet_path)

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend", session_id="sess_s")
    with scope_p, skill_p, session_p, patch.object(mod, "_load_fleet", return_value=fleet):
        # Before any read, the self-query returns zeroed counters (never errors).
        empty = await mod.strata_session_stats()
        assert empty["reads"] == 0
        assert empty["session_id"] == "sess_s"

        await mod.strata_read_scope_summary("g_arch")
        stats = await mod.strata_session_stats()

    assert stats["reads"] == 1
    assert stats["contributions"] == 0
    assert stats["reads_by_scope"]["g_arch"]["count"] == 1


async def test_accepted_contribution_increments_session_counter(tmp_path: Path) -> None:
    """An accepted contribution bumps the session's contributions counter (release valve)."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    fake_judgment = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="ok",
        new_summary=_make_summary("g_arch", "updated"),
    )

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend", session_id="sess_c")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=fake_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        await mod.strata_contribute(
            scope_id="g_arch",
            content="Use structured logging.",
            proposed_classification="context",
            subject=None,
            supersedes=None,
        )

    state = mod._session_store.read("sess_c")
    assert state is not None
    assert state.contributions == 1


async def test_declined_contribution_does_not_increment_counter(tmp_path: Path) -> None:
    """A scope-manager decline is not an accepted contribution — no counter bump."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    fake_judgment = ScopeManagerJudgment(
        decision="decline",
        reasoning="not memory-worthy",
        new_summary=None,
    )

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend", session_id="sess_d")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=fake_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        await mod.strata_contribute(
            scope_id="g_arch",
            content="trivia",
            proposed_classification="context",
            subject=None,
            supersedes=None,
        )

    # A decline creates no session file (no counters ever incremented) — or, if
    # one exists, contributions is still 0. Either way contributions must be 0.
    state = mod._session_store.read("sess_d")
    assert state is None or state.contributions == 0


# ---------------------------------------------------------------------------
# Issue #111: strata_session_closeout (mechanical decline) + read-time nudge
# + contribution-norm instructions
# ---------------------------------------------------------------------------


async def _seed_and_read(mod, fleet, *, scope: str, session_id: str, times: int) -> list[dict]:
    """Read g_arch's summary *times* times as *scope*/*session_id*; return the results.

    Every read increments the session's reads counter, so this walks the session
    up to (and past) the nudge threshold deterministically.
    """
    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope=scope, session_id=session_id)
    results: list[dict] = []
    with scope_p, skill_p, session_p, patch.object(mod, "_load_fleet", return_value=fleet):
        for _ in range(times):
            results.append(await mod.strata_read_scope_summary("g_arch"))
    return results


async def test_closeout_records_decline_without_building_judge(tmp_path: Path) -> None:
    """strata_session_closeout records a decline as a pure session-state write.

    The mechanical decline path must never construct the scope-manager or the
    Anthropic judge client — patching both to raise proves neither is touched.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend", session_id="sess_co")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(
            mod,
            "_build_scope_manager",
            side_effect=AssertionError("closeout must never build a scope manager"),
        ),
        patch(
            "anthropic.Anthropic",
            side_effect=AssertionError("closeout must never construct a judge client"),
        ),
    ):
        result = await mod.strata_session_closeout(
            reason="read-only investigation, nothing decided"
        )

    assert result["session_id"] == "sess_co"
    assert result["declines"] == 1
    assert result["contributions"] == 0

    state = mod._session_store.read("sess_co")
    assert state is not None
    assert state.declines == 1


async def test_no_nudge_below_threshold(tmp_path: Path) -> None:
    """Reads below the threshold carry no nudge — the early-read silence (#111)."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    SummaryStore(summaries_dir).write("g_arch", _make_summary("g_arch", "ctx"))
    fleet = FleetConfig.load(fleet_path)

    results = await _seed_and_read(mod, fleet, scope="g_backend", session_id="sess_nb", times=2)

    assert all("nudge" not in r for r in results)


async def test_nudge_appears_at_threshold_with_current_counts(tmp_path: Path) -> None:
    """At the threshold the nudge fires and names the CURRENT read count."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    SummaryStore(summaries_dir).write("g_arch", _make_summary("g_arch", "ctx"))
    fleet = FleetConfig.load(fleet_path)

    results = await _seed_and_read(mod, fleet, scope="g_backend", session_id="sess_th", times=3)

    # First two reads (below threshold) stay silent; the third fires.
    assert "nudge" not in results[0]
    assert "nudge" not in results[1]
    nudge = results[2]["nudge"]
    # Names the current count and points at the two release valves. Base tier
    # (not yet escalated) — the escalation marker is absent.
    assert "3" in nudge
    assert "strata_session_closeout" in nudge
    assert "strata_contribute" in nudge
    assert "stale" not in nudge


async def test_nudge_escalates_at_higher_threshold(tmp_path: Path) -> None:
    """Once reads reach the escalation threshold the wording sharpens."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    SummaryStore(summaries_dir).write("g_arch", _make_summary("g_arch", "ctx"))
    fleet = FleetConfig.load(fleet_path)

    results = await _seed_and_read(mod, fleet, scope="g_backend", session_id="sess_esc", times=6)

    base_nudge = results[2]["nudge"]  # reads == 3, base tier
    escalated = results[5]["nudge"]  # reads == 6, escalated tier

    assert "6" in escalated
    assert "stale" in escalated  # escalation marker, absent from the base tier
    assert escalated != base_nudge


async def test_nudge_silent_after_contribution(tmp_path: Path) -> None:
    """An accepted contribution resets the asymmetry and quiets the nudge."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    SummaryStore(summaries_dir).write("g_arch", _make_summary("g_arch", "ctx"))
    fleet = FleetConfig.load(fleet_path)

    # Read to the threshold: the last read carries a nudge.
    pre = await _seed_and_read(mod, fleet, scope="g_backend", session_id="sess_ac", times=3)
    assert "nudge" in pre[-1]

    fake_judgment = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="ok",
        new_summary=_make_summary("g_arch", "updated"),
    )

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend", session_id="sess_ac")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=fake_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        await mod.strata_contribute(
            scope_id="g_arch",
            content="Structured logging is the standard.",
            proposed_classification="context",
        )
        after = await mod.strata_read_scope_summary("g_arch")

    assert "nudge" not in after


async def test_nudge_silent_after_closeout(tmp_path: Path) -> None:
    """A mechanical closeout resets the asymmetry and quiets the nudge."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    SummaryStore(summaries_dir).write("g_arch", _make_summary("g_arch", "ctx"))
    fleet = FleetConfig.load(fleet_path)

    # Read to the threshold: the last read carries a nudge.
    pre = await _seed_and_read(mod, fleet, scope="g_backend", session_id="sess_cn", times=3)
    assert "nudge" in pre[-1]

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend", session_id="sess_cn")
    with scope_p, skill_p, session_p, patch.object(mod, "_load_fleet", return_value=fleet):
        closeout = await mod.strata_session_closeout(reason="nothing to record")
        after = await mod.strata_read_scope_summary("g_arch")

    assert closeout["declines"] == 1
    assert "nudge" not in after


async def test_nudge_rides_perspective_and_record_reads(tmp_path: Path) -> None:
    """The nudge is not summary-only: it rides perspective reads and record reads.

    A perspective read increments the counter like a summary read; a record read
    is a forensic view that does NOT increment (issue #110) but still surfaces
    the nudge once the session already crossed the threshold.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    SummaryStore(summaries_dir).write("g_backend", _make_summary("g_backend", "ctx"))
    fleet = FleetConfig.load(fleet_path)

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend", session_id="sess_pr")
    with scope_p, skill_p, session_p, patch.object(mod, "_load_fleet", return_value=fleet):
        # Three perspective reads take the session to the threshold.
        persp = [await mod.strata_read_perspective("g_backend") for _ in range(3)]
        record = await mod.strata_read_scope_record("g_backend")

    assert "nudge" not in persp[0]
    assert "nudge" in persp[2]
    assert "3" in persp[2]["nudge"]

    # The record read carries the nudge but did not itself bump the counter.
    assert "nudge" in record
    assert "3" in record["nudge"]
    assert mod._session_store.read("sess_pr").reads == 3


def test_instructions_declare_contribution_norm(tmp_path: Path) -> None:
    """The MCP server's initialize-handshake instructions carry the contribution norm."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    instructions = mod.mcp.instructions or ""
    assert "strata_session_closeout" in instructions
    assert "strata_contribute" in instructions
    assert "contribute" in instructions.lower()


# ---------------------------------------------------------------------------
# Test 38: the paged record read and the by-id lookup (issue #130)
# ---------------------------------------------------------------------------


def _seed_contributions(db_path: str, scope_id: str, count: int) -> list[str]:
    """Append *count* contributions to *scope_id* and return their ids, oldest first."""
    contributor = _make_contributor()
    with RecordStore(db_path) as rs:
        return [
            rs.append_contribution(
                scope_id=scope_id,
                content=f"contribution {i}",
                proposed_classification="context",
                subject=None,
                supersedes=None,
                contributor=contributor,
            ).id
            for i in range(count)
        ]


def _record_reader(tmp_path: Path, fleet_path: Path) -> tuple[object, str]:
    """Load the MCP module against a fresh migrated DB; return ``(module, db_path)``."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    return mod, db_path


async def test_read_scope_record_is_bounded_by_default(tmp_path: Path) -> None:
    """The breaking change issue #130 asks for: an unadorned call is the newest page.

    The unbounded default was the bug — a whole-record read overflows the
    tool-result limit of the agents the forensic view exists for.
    """
    fleet_path = _make_fleet_yaml(tmp_path)
    mod, db_path = _record_reader(tmp_path, fleet_path)
    expected = _seed_contributions(db_path, "g_backend", 5)
    mod._record_store = RecordStore(db_path)

    fleet = FleetConfig.load(fleet_path)
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        default_page = await mod.strata_read_scope_record("g_backend")
        small_page = await mod.strata_read_scope_record("g_backend", limit=2)

    # The default page size comes from settings, never a literal at the call site.
    assert default_page["page"]["limit"] == mod._settings.record_page_size
    assert default_page["page"]["total"] == 5
    # Newest first — the forensic reader almost always wants what just happened.
    assert [c["id"] for c in small_page["contributions"]] == [expected[4], expected[3]]
    assert small_page["page"]["next_before_id"] == expected[3]


async def test_read_scope_record_pages_walk_the_whole_record(tmp_path: Path) -> None:
    """Walking before_id until it is null covers the record exactly once."""
    fleet_path = _make_fleet_yaml(tmp_path)
    mod, db_path = _record_reader(tmp_path, fleet_path)
    expected = _seed_contributions(db_path, "g_backend", 5)
    mod._record_store = RecordStore(db_path)

    fleet = FleetConfig.load(fleet_path)
    walked: list[str] = []
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        cursor = None
        while True:
            page = await mod.strata_read_scope_record("g_backend", limit=2, before_id=cursor)
            walked.extend(c["id"] for c in page["contributions"])
            cursor = page["page"]["next_before_id"]
            if cursor is None:
                break

    assert walked == list(reversed(expected))


async def test_read_scope_record_rejects_out_of_range_paging(tmp_path: Path) -> None:
    """A limit below 1 and a stale cursor both fail loudly."""
    fleet_path = _make_fleet_yaml(tmp_path)
    mod, db_path = _record_reader(tmp_path, fleet_path)
    mod._record_store = RecordStore(db_path)

    fleet = FleetConfig.load(fleet_path)
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        with pytest.raises(RuntimeError, match="Invalid record page"):
            await mod.strata_read_scope_record("g_backend", limit=0)
        with pytest.raises(RuntimeError, match="before_id is not a contribution"):
            await mod.strata_read_scope_record("g_backend", before_id="c_does_not_exist")


async def test_read_contribution_returns_state_and_verdict(tmp_path: Path) -> None:
    """The by-id hit: one contribution, its state, and the scope-manager's notes."""
    fleet_path = _make_fleet_yaml(tmp_path)
    mod, db_path = _record_reader(tmp_path, fleet_path)
    (contribution_id,) = _seed_contributions(db_path, "g_backend", 1)
    with RecordStore(db_path) as rs:
        rs.record_judgment(
            contribution_id=contribution_id,
            decision="accept_as_context",
            judged_by="scope-manager",
            notes="Accepted: adds a fact the context section lacked.",
        )
    mod._record_store = RecordStore(db_path)

    fleet = FleetConfig.load(fleet_path)
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        result = await mod.strata_read_contribution(contribution_id)

    assert result["contribution"]["id"] == contribution_id
    assert result["state"]["state"] == "judged"
    assert result["judgment"]["decision"] == "accept_as_context"
    assert result["judgment"]["notes"] == "Accepted: adds a fact the context section lacked."
    assert result["judgment_attempts"] == []


async def test_read_contribution_marks_a_failed_judgment(tmp_path: Path) -> None:
    """judge_failed carries no verdict — "the judge errored" never reads as "in flight"."""
    fleet_path = _make_fleet_yaml(tmp_path)
    mod, db_path = _record_reader(tmp_path, fleet_path)
    (contribution_id,) = _seed_contributions(db_path, "g_backend", 1)
    with RecordStore(db_path) as rs:
        rs.record_judgment_attempt(
            contribution_id=contribution_id,
            error_class="APIError",
            message="upstream timeout",
            outcome="judge_failed",
        )
    mod._record_store = RecordStore(db_path)

    fleet = FleetConfig.load(fleet_path)
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        result = await mod.strata_read_contribution(contribution_id)

    assert result["state"]["state"] == "judge_failed"
    assert result["state"]["error_class"] == "APIError"
    assert result["judgment"] is None
    assert [a["error_class"] for a in result["judgment_attempts"]] == ["APIError"]


async def test_read_contribution_raises_for_an_unknown_id(tmp_path: Path) -> None:
    """The by-id miss raises the record's existing not-found idiom."""
    fleet_path = _make_fleet_yaml(tmp_path)
    mod, db_path = _record_reader(tmp_path, fleet_path)
    mod._record_store = RecordStore(db_path)

    fleet = FleetConfig.load(fleet_path)
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError, match="Contribution not found"),
    ):
        await mod.strata_read_contribution("c_does_not_exist")


async def test_read_contribution_refuses_a_contribution_outside_the_entitled_surface(
    tmp_path: Path,
) -> None:
    """The by-id lookup never reaches a record the scope read cannot (issue #48)."""
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)
    mod, db_path = _record_reader(tmp_path, fleet_path)
    (peer_contribution,) = _seed_contributions(db_path, "g_peer_a", 1)
    mod._record_store = RecordStore(db_path)

    fleet = FleetConfig.load(fleet_path)
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError, match="outside your entitled surface"),
    ):
        await mod.strata_read_contribution(peer_contribution)


# ---------------------------------------------------------------------------
# Feature A: lazy fleet reload (MCP side) — the reloader shared with the
# FastAPI backend (strata.fleet_reload.FleetReloader). An invalid edit must
# not break every subsequent tool call; it must keep serving the last good
# fleet and surface a notice in tool output.
# ---------------------------------------------------------------------------


async def test_list_scopes_no_reparse_when_fleet_yaml_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two calls with no file change between them must reload fleet.yaml only once."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    # First call primes the reloader's cache.
    mod.strata_list_scopes()

    calls = 0
    real_load = FleetConfig.load.__func__

    def counting_load(cls, path):
        nonlocal calls
        calls += 1
        return real_load(cls, path)

    monkeypatch.setattr(FleetConfig, "load", classmethod(counting_load))

    mod.strata_list_scopes()
    mod.strata_list_scopes()
    assert calls == 0, "unchanged fleet.yaml must not be re-parsed"


async def test_invalid_fleet_edit_keeps_serving_last_good_fleet_with_notice(tmp_path: Path) -> None:
    """An invalid mid-session edit must not break every subsequent tool call."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    good = mod.strata_list_scopes()
    assert "fleet_notice" not in good
    good_scope_ids = {s["id"] for s in good["scopes"]}

    # Corrupt fleet.yaml: scope references an undefined stratum.
    fleet_path.write_text(
        "strata:\n  - id: L0\n    name: exec\n    ordinal: 0\n"
        "scopes:\n  - id: g_arch\n    name: bad\n    stratum_id: NOPE\n"
        "edges: []\n",
        encoding="utf-8",
    )

    served = mod.strata_list_scopes()
    served_scope_ids = {s["id"] for s in served["scopes"]}
    assert served_scope_ids == good_scope_ids, "invalid edit must keep serving the last good fleet"
    assert "fleet_notice" in served
    assert "fleet.yaml" in served["fleet_notice"]


async def test_read_perspective_gates_when_fleet_yaml_vanishes(tmp_path: Path) -> None:
    """A fleet.yaml that vanishes AFTER a successful bind must gate memory
    reads with a loud, actionable error — not the success-shaped empty
    payload the reload-on-read fallback used to produce (with only an
    easy-to-miss ``fleet_notice`` riding along). That payload is
    indistinguishable from a legitimately new scope with nothing written
    yet, so an agent reading it has no reason to stop.

    This must gate the same way an unresolved binding already does — via
    _require_bound_or_elicit — since a vanished fleet source is a
    config-class failure strata_bind can never clear (a restart is
    required once the file is back)."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_AGENT_SKILL", "strata-developer"),
    ):
        # Prime the reloader with a good load — an ordinary, ungated read.
        good = await mod.strata_read_perspective()
        assert "fleet_notice" not in good

        fleet_path.unlink()

        with pytest.raises(RuntimeError) as exc_info:
            await mod.strata_read_perspective()

    message = str(exc_info.value)
    assert str(fleet_path) in message, "must name the missing file path"
    assert "restart" in message.lower(), "must say a restart is required once fixed"


async def test_list_scopes_stays_ungated_when_fleet_yaml_vanishes(tmp_path: Path) -> None:
    """strata_list_scopes is deliberately NOT gated by _require_bound_or_elicit
    (fleet topology is not scoped memory) — an operator diagnosing a vanished
    fleet.yaml still needs it to work, carrying the fleet_notice as before."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    good = mod.strata_list_scopes()
    assert "fleet_notice" not in good

    fleet_path.unlink()

    served = mod.strata_list_scopes()
    assert "fleet_notice" in served
    assert "fleet.yaml" in served["fleet_notice"]


# ---------------------------------------------------------------------------
# Feature B: strata_bind — rebind this session to a different scope at
# runtime, sharing Feature A's reload path so a scope added after server
# init is bindable without a restart (the exact incident this closes).
# ---------------------------------------------------------------------------


async def test_bind_to_scope_added_after_server_init_succeeds(tmp_path: Path) -> None:
    """The incident: a scope added to fleet.yaml after the server started must be bindable."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with patch.object(mod, "_AGENT_SCOPE", "g_backend"):
        # g_new does not exist yet — binding to it now must fail.
        with pytest.raises(RuntimeError, match="not found"):
            await mod.strata_bind(scope_id="g_new")

        # Add the scope to fleet.yaml out of band (no server restart).
        raw = yaml.safe_load(fleet_path.read_text(encoding="utf-8"))
        raw["scopes"].append({"id": "g_new", "name": "New Scope", "stratum_id": "L1"})
        fleet_path.write_text(yaml.dump(raw, default_flow_style=False), encoding="utf-8")

        # Already bound to g_backend, so this is a switch — the self-bind
        # guard (a separate feature) requires an announce-then-confirm round
        # trip; not what this test is about, so seed the announcement
        # directly and confirm in one call.
        with _seeded_pending_switch(mod, "g_new"):
            result = await mod.strata_bind(scope_id="g_new", confirm=True)

        assert result["scope_id"] == "g_new"
        assert mod._AGENT_SCOPE == "g_new"


async def test_bind_to_nonexistent_scope_lists_valid_scopes_and_leaves_binding_intact(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with patch.object(mod, "_AGENT_SCOPE", "g_backend"), patch.object(mod, "_AGENT_SKILL", None):
        with pytest.raises(RuntimeError) as exc_info:
            await mod.strata_bind(scope_id="g_does_not_exist")

        message = str(exc_info.value)
        assert "g_arch" in message
        assert "g_backend" in message
        # Binding must be completely unchanged after a refused bind.
        assert mod._AGENT_SCOPE == "g_backend"
        assert mod._AGENT_SKILL is None


async def test_bind_enforces_skill_permission(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet = {
        "strata": [{"id": "L0", "name": "exec", "ordinal": 0}],
        "scopes": [
            {
                "id": "g_restricted",
                "name": "Restricted",
                "stratum_id": "L0",
                "permitted_skills": ["strata-developer"],
            }
        ],
        "edges": [],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with patch.object(mod, "_AGENT_SCOPE", ""), patch.object(mod, "_AGENT_SKILL", None):
        with pytest.raises(RuntimeError, match="permitted skills"):
            await mod.strata_bind(scope_id="g_restricted", skill="not-allowed")

        result = await mod.strata_bind(scope_id="g_restricted", skill="strata-developer")
        assert result["skill"] == "strata-developer"
        assert mod._AGENT_SKILL == "strata-developer"


async def test_bind_leaves_session_id_unchanged(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_stable"),
    ):
        # Already bound to g_backend, so this is a switch — the self-bind
        # guard (a separate feature) requires an announce-then-confirm round
        # trip; not what this test is about, so seed the announcement
        # directly and confirm in one call.
        with _seeded_pending_switch(mod, "g_arch"):
            result = await mod.strata_bind(scope_id="g_arch", confirm=True)

        assert result["scope_id"] == "g_arch"
        assert result["session_id"] == "sess_stable"
        assert mod._AGENT_SESSION_ID == "sess_stable"


# ---------------------------------------------------------------------------
# Self-bind guard (operator finding from live testing): an agent picked a
# scope on its own judgment to answer a question, without the user ever
# weighing in. Two rules: (1) every bind-instructing text must route through
# the user, never read as permission for the agent to choose; (2) switching
# an ALREADY-bound session to a different scope requires an explicit,
# separate confirmation step — either an accepted elicitation, or a second
# strata_bind call carrying confirm=True. Initial bind (from unbound) and a
# same-scope re-bind are unaffected — no prior identity to protect.
# ---------------------------------------------------------------------------


async def test_rebind_without_confirm_does_not_switch_and_returns_heads_up(
    tmp_path: Path,
) -> None:
    """A switch attempted without confirmation must not take effect — the
    binding stays exactly as it was, and the result is a heads-up, not a
    silent no-op and not an exception."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with patch.object(mod, "_AGENT_SCOPE", "g_arch"), patch.object(mod, "_AGENT_SKILL", None):
        result = await mod.strata_bind(scope_id="g_backend")

        # Binding is untouched.
        assert result["scope_id"] == "g_arch"
        assert mod._AGENT_SCOPE == "g_arch"

        # A clear, programmatically-detectable heads-up, not silence.
        assert result["switch_pending"] is True
        assert "switch_declined" not in result
        assert "g_arch" in result["message"]
        assert "g_backend" in result["message"]
        assert "confirm" in result["message"].lower()
        assert (
            mod._PendingSwitch(target_scope_id="g_backend", requested_at=ANY) == mod._PENDING_SWITCH
        )


async def test_switch_cold_confirm_true_does_not_bypass_announce(tmp_path: Path) -> None:
    """Live-test finding: an agent self-supplying confirm=True on the VERY
    FIRST call for a switch must not switch — confirm=True is a promise
    from whoever is calling, not proof the user actually answered. Without
    a matching prior announcement, this is the exact bypass the two-step
    enforcement exists to kill."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with patch.object(mod, "_AGENT_SCOPE", "g_arch"), patch.object(mod, "_AGENT_SKILL", None):
        # No prior announcement exists — mod._PENDING_SWITCH is None.
        assert mod._PENDING_SWITCH is None

        result = await mod.strata_bind(scope_id="g_backend", confirm=True)

        # Still a heads-up, NOT a switch — the binding is untouched.
        assert result["switch_pending"] is True
        assert result["scope_id"] == "g_arch"
        assert mod._AGENT_SCOPE == "g_arch"
        # This first (cold) call is now the announcement, for a REAL
        # follow-up to confirm — not itself a confirmation.
        assert (
            mod._PendingSwitch(target_scope_id="g_backend", requested_at=ANY) == mod._PENDING_SWITCH
        )


async def test_switch_announce_then_confirm_within_window_switches(tmp_path: Path) -> None:
    """The legitimate two-step: announce (any confirm value), then a
    follow-up call naming the SAME target with confirm=True actually
    switches, and clears the pending record."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with patch.object(mod, "_AGENT_SCOPE", "g_arch"), patch.object(mod, "_AGENT_SKILL", None):
        announced = await mod.strata_bind(scope_id="g_backend")
        assert announced["switch_pending"] is True
        assert mod._AGENT_SCOPE == "g_arch"

        confirmed = await mod.strata_bind(scope_id="g_backend", confirm=True)

        assert confirmed["scope_id"] == "g_backend"
        assert "switch_pending" not in confirmed
        assert mod._AGENT_SCOPE == "g_backend"
        assert mod._PENDING_SWITCH is None


async def test_switch_mismatched_target_confirm_returns_new_heads_up(tmp_path: Path) -> None:
    """A confirm=True naming a DIFFERENT target than the one currently
    pending is cold for THAT target — it replaces the pending record with
    its own fresh announcement rather than switching to anything."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet = {
        "strata": [{"id": "L0", "name": "root", "ordinal": 0}],
        "scopes": [
            {"id": "g_arch", "name": "Arch", "stratum_id": "L0"},
            {"id": "g_backend", "name": "Backend", "stratum_id": "L0"},
            {"id": "g_other", "name": "Other", "stratum_id": "L0"},
        ],
        "edges": [],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with patch.object(mod, "_AGENT_SCOPE", "g_arch"), patch.object(mod, "_AGENT_SKILL", None):
        # Announce a switch to g_backend.
        await mod.strata_bind(scope_id="g_backend")
        assert mod._PENDING_SWITCH.target_scope_id == "g_backend"

        # A confirm=True for a DIFFERENT target (g_other) must not switch
        # to g_other, and must not be treated as confirming g_backend either.
        result = await mod.strata_bind(scope_id="g_other", confirm=True)

        assert result["switch_pending"] is True
        assert result["scope_id"] == "g_arch"
        assert mod._AGENT_SCOPE == "g_arch"
        # Pending now tracks the NEW target, not the old one.
        assert mod._PENDING_SWITCH.target_scope_id == "g_other"

        # And the original g_backend announcement no longer confirms,
        # since it was replaced.
        stale = await mod.strata_bind(scope_id="g_backend", confirm=True)
        assert stale["switch_pending"] is True
        assert mod._AGENT_SCOPE == "g_arch"


async def test_switch_pending_expires_after_window(tmp_path: Path) -> None:
    """A confirm=True arriving after the pending window elapsed is treated
    as cold — a fresh announcement, not a confirmation of the stale one.

    Monkeypatches time.monotonic() directly rather than seeding a stale
    wall-clock timestamp — expiry is computed from monotonic time
    specifically so an NTP jump can't stretch or shrink the window, so the
    test has to move the SAME clock the code reads."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    announced_at = 1_000_000.0
    now = announced_at + mod._PENDING_SWITCH_WINDOW_SECONDS + 1.0
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_arch"),
        patch.object(mod, "_AGENT_SKILL", None),
        patch.object(
            mod,
            "_PENDING_SWITCH",
            mod._PendingSwitch(target_scope_id="g_backend", requested_at=announced_at),
        ),
        patch.object(mod.time, "monotonic", return_value=now),
    ):
        result = await mod.strata_bind(scope_id="g_backend", confirm=True)

        # Expired — cold again, not a switch.
        assert result["switch_pending"] is True
        assert mod._AGENT_SCOPE == "g_arch"
        # Re-announced with the (patched) "now" timestamp.
        assert mod._PENDING_SWITCH.target_scope_id == "g_backend"
        assert mod._PENDING_SWITCH.requested_at == now


def test_switch_pending_message_never_claims_the_user_declined() -> None:
    """Live Codex-replay finding (bug fix): _switch_pending_result used to
    take a *declined* flag and, when true, say "The user declined. The
    binding stands." — but a protocol-level elicitation decline/cancel/
    timeout is indistinguishable from a real human "no" over the wire, so
    that wording was a claim the function couldn't actually back up (the
    live incident: a first-call switch attempt returned exactly this claim
    with no dialog ever shown to a human). There is now exactly one
    message, and it never asserts the user's answer either way — it always
    hands over the announce-then-confirm recipe, which is safe precisely
    because it never claims anyone already said no."""
    from strata.mcp.server import _switch_pending_result

    pending = _switch_pending_result("g_arch", "g_backend")
    message = pending["message"].lower()
    assert "declined" not in message
    assert "confirm=true" in message
    assert "switch_declined" not in pending


async def test_rebind_with_confirm_switches_and_notes_identity_change(tmp_path: Path) -> None:
    """The genuine announce-then-confirm round trip actually performs the
    switch on the SECOND call, and the result explicitly calls out the
    identity change — this is not a cosmetic rename."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with patch.object(mod, "_AGENT_SCOPE", "g_arch"), patch.object(mod, "_AGENT_SKILL", None):
        # First call announces — no switch yet, regardless of confirm.
        announced = await mod.strata_bind(scope_id="g_backend")
        assert announced.get("switch_pending") is True
        assert mod._AGENT_SCOPE == "g_arch"

        # Second call, same target, confirm=True: the switch actually happens.
        result = await mod.strata_bind(scope_id="g_backend", confirm=True)

        assert result["scope_id"] == "g_backend"
        assert mod._AGENT_SCOPE == "g_backend"
        assert "switch_pending" not in result
        assert "g_arch" in result["message"]
        assert "g_backend" in result["message"]
        # Explicitly frames this as a change of whose memory is read/written.
        assert "memory" in result["message"].lower()


async def test_rebind_same_scope_is_a_no_op_success_without_confirm(tmp_path: Path) -> None:
    """Naming the SAME scope you are already bound to is never a switch —
    confirm is neither required nor meaningful, and it stays a plain
    success like today."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with patch.object(mod, "_AGENT_SCOPE", "g_arch"), patch.object(mod, "_AGENT_SKILL", None):
        result = await mod.strata_bind(scope_id="g_arch")

        assert result["scope_id"] == "g_arch"
        assert "switch_pending" not in result
        assert mod._AGENT_SCOPE == "g_arch"


async def test_initial_bind_from_unbound_needs_no_confirm(tmp_path: Path) -> None:
    """An initial bind (session currently unbound) is not a switch — no
    prior identity to protect, so it proceeds immediately, confirm or not."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with patch.object(mod, "_AGENT_SCOPE", ""), patch.object(mod, "_AGENT_SKILL", None):
        result = await mod.strata_bind(scope_id="g_backend")

        assert result["scope_id"] == "g_backend"
        assert "switch_pending" not in result
        assert mod._AGENT_SCOPE == "g_backend"


async def test_recovery_bind_after_unknown_scope_startup_failure_is_one_call(
    tmp_path: Path,
) -> None:
    """Reviewer follow-up (recovery friction): after an unknown-scope
    startup failure, _AGENT_SCOPE holds the INVALID id from
    STRATA_AGENT_SCOPE — that stale, never-actually-bound value is not an
    identity worth protecting. Recovering to a valid scope while still
    unresolved must complete in a single strata_bind call, not be treated
    as a switch requiring confirmation."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with (
        # The startup shape: STRATA_AGENT_SCOPE named a scope that doesn't
        # exist in fleet.yaml, so _AGENT_SCOPE holds that invalid id and the
        # session is unresolved.
        patch.object(mod, "_AGENT_SCOPE", "g_does_not_exist"),
        patch.object(mod, "_AGENT_SKILL", None),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(
            mod,
            "_STARTUP_ERRORS_BINDING",
            ["scope 'g_does_not_exist' not found in fleet config."],
        ),
    ):
        result = await mod.strata_bind(scope_id="g_backend")

        assert result["scope_id"] == "g_backend"
        assert "switch_pending" not in result
        assert mod._AGENT_SCOPE == "g_backend"
        assert mod._UNRESOLVED is False


async def test_rebind_elicitation_accept_switches(tmp_path: Path) -> None:
    """Where the client declares the elicitation capability, strata_bind
    asks the user directly and an accepted confirmation switches — this
    replaces the confirm= dance when available."""
    from mcp.shared.memory import create_connected_server_and_client_session

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    async def _confirm_switch(context, params):
        from mcp import types as mcp_types

        return mcp_types.ElicitResult(action="accept", content={"confirm": True})

    with patch.object(mod, "_AGENT_SCOPE", "g_arch"), patch.object(mod, "_AGENT_SKILL", None):
        async with create_connected_server_and_client_session(
            mod.mcp, elicitation_callback=_confirm_switch
        ) as client:
            result = await client.call_tool("strata_bind", {"scope_id": "g_backend"})

        assert result.isError is not True
        assert mod._AGENT_SCOPE == "g_backend"


async def _elicit_cancel(context, params):
    from mcp import types as mcp_types

    return mcp_types.ElicitResult(action="cancel")


async def test_rebind_elicitation_protocol_decline_falls_through_to_two_step(
    tmp_path: Path,
) -> None:
    """Live Codex-replay finding (the actual incident this fix closes): a
    client that declares the elicitation capability and auto-responds
    "decline" with NO dialog ever shown to a human (observed: Codex
    v0.150.1) must NOT have that read as the user's answer. The switch call
    must fall through to the standard announce-then-confirm two-step —
    pending recorded, binding unchanged, heads-up text — with no claim that
    the user declined anywhere in the result. A genuine follow-up call with
    confirm=True then completes the switch."""
    from mcp.shared.memory import create_connected_server_and_client_session

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    with patch.object(mod, "_AGENT_SCOPE", "g_arch"), patch.object(mod, "_AGENT_SKILL", None):
        async with create_connected_server_and_client_session(
            mod.mcp, elicitation_callback=_elicit_decline
        ) as client:
            result = await client.call_tool("strata_bind", {"scope_id": "g_backend"})

        assert result.isError is not True  # a heads-up result, not an error
        text = _result_text(result)
        assert "switch_pending" in text
        # The exact live-incident claim must never appear.
        assert "declined" not in text.lower()
        assert "switch_declined" not in text
        assert "confirm=true" in text.lower()
        assert mod._AGENT_SCOPE == "g_arch"
        assert mod._PENDING_SWITCH is not None
        assert mod._PENDING_SWITCH.target_scope_id == "g_backend"

        # A genuine follow-up call (no elicitation session this time — a
        # plain confirm=True naming the same, now-pending target) completes
        # the switch. Still inside this `with` block, so _AGENT_SCOPE is
        # still "g_arch" as the announced call left it.
        confirmed = await mod.strata_bind(scope_id="g_backend", confirm=True)
        assert confirmed["scope_id"] == "g_backend"
        assert mod._AGENT_SCOPE == "g_backend"


async def test_rebind_elicitation_protocol_cancel_falls_through_to_two_step(
    tmp_path: Path,
) -> None:
    """Same finding, the other non-accept action: a client that
    auto-cancels must be treated identically to a decline — dialog
    unavailable, never attributed to the user."""
    from mcp.shared.memory import create_connected_server_and_client_session

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    with patch.object(mod, "_AGENT_SCOPE", "g_arch"), patch.object(mod, "_AGENT_SKILL", None):
        async with create_connected_server_and_client_session(
            mod.mcp, elicitation_callback=_elicit_cancel
        ) as client:
            result = await client.call_tool("strata_bind", {"scope_id": "g_backend"})

        assert result.isError is not True
        text = _result_text(result)
        assert "switch_pending" in text
        assert "declined" not in text.lower()
        assert mod._AGENT_SCOPE == "g_arch"
        assert mod._PENDING_SWITCH is not None
        assert mod._PENDING_SWITCH.target_scope_id == "g_backend"


async def test_rebind_elicitation_accept_confirm_false_also_falls_through(
    tmp_path: Path,
) -> None:
    """Item 3 (accept is the only outcome with user authority): even an
    explicit ACCEPT carrying confirm=False does not raise or claim a
    special "declined" state — it falls through to the same two-step
    heads-up as any other non-True outcome, uniformly."""

    async def _accept_confirm_false(context, params):
        from mcp import types as mcp_types

        return mcp_types.ElicitResult(action="accept", content={"confirm": False})

    from mcp.shared.memory import create_connected_server_and_client_session

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    with patch.object(mod, "_AGENT_SCOPE", "g_arch"), patch.object(mod, "_AGENT_SKILL", None):
        async with create_connected_server_and_client_session(
            mod.mcp, elicitation_callback=_accept_confirm_false
        ) as client:
            result = await client.call_tool("strata_bind", {"scope_id": "g_backend"})

        assert result.isError is not True
        text = _result_text(result)
        assert "switch_pending" in text
        assert mod._AGENT_SCOPE == "g_arch"


async def test_rebind_elicitation_never_marks_unavailable(tmp_path: Path) -> None:
    """Unlike the scope-pick elicitation (Change 2), a non-accepted switch
    confirmation must NOT set _ELICIT_UNAVAILABLE — a user (or client) might
    legitimately confirm the very same switch moments later."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_arch"),
        patch.object(mod, "_AGENT_SKILL", None),
        patch.object(
            mod,
            "_attempt_elicit_switch_confirm",
            return_value=False,
        ),
    ):
        await mod.strata_bind(scope_id="g_backend")
        assert mod._ELICIT_UNAVAILABLE is False


def _normalize(text: str) -> str:
    """Collapse whitespace/newlines to single spaces for robust substring
    checks against wrapped docstrings/messages — a phrase split across a
    line-wrap boundary must still be found."""
    return " ".join(text.lower().split())


async def test_reworded_bind_texts_route_through_the_user(tmp_path: Path) -> None:
    """Grep-style assertions: every bind-instructing text an agent reads
    must tell it to ask the user, and none may read as permission for the
    agent to pick a scope on its own."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    # 1. The unbound_notice attached to strata_list_scopes.
    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        listing = mod.strata_list_scopes()
    assert "ask the user" in _normalize(listing["unbound_notice"])

    # 2. The gated-tool unresolved error text (_unresolved_message) — the
    # REAL per-failure strings _validate_binding builds, not a hand-written
    # stand-in. A stand-in here is exactly what let the live-session
    # finding slip past this guard originally: _validate_binding's own
    # "STRATA_AGENT_SCOPE/SKILL is not set" items carried their own
    # self-bind instruction, independent of _unresolved_message's wrapper
    # text around them.
    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(
            mod, "_STARTUP_ERRORS_BINDING", _validate_binding_scope_and_skill_errors(fleet)
        ),
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError) as exc_info,
    ):
        await mod.strata_read_perspective()
    assert "ask the user" in _normalize(str(exc_info.value))

    # 3. strata_bind's own tool docstring (what the client sees as the
    # tool's description).
    assert "ask the user" in _normalize(mod.strata_bind.__doc__)
    assert "must come from the user" in _normalize(mod.strata_bind.__doc__)

    # 4. The switch-pending heads-up message.
    with patch.object(mod, "_AGENT_SCOPE", "g_arch"), patch.object(mod, "_AGENT_SKILL", None):
        pending = await mod.strata_bind(scope_id="g_backend")
    assert "ask the user" in _normalize(pending["message"])

    # No surviving self-bind instruction — the pre-fix text this whole
    # guard replaces — in any of the texts checked above.
    for text in (
        listing["unbound_notice"],
        str(exc_info.value),
        mod.strata_bind.__doc__,
        pending["message"],
    ):
        normalized = _normalize(text)
        assert "bind with strata_bind(scope_id=...)" not in normalized
        assert "call strata_bind to rebind this session to it" not in normalized


async def test_contribution_after_bind_carries_new_scope_in_provenance(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    fake_judgment = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="fine",
        new_summary=_make_summary("g_arch", "updated"),
    )

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_AGENT_SKILL", "strata-developer"),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_bind_test"),
        patch("strata.scope_manager.ScopeManager.judge", return_value=fake_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        # Already bound to g_backend, so this is a switch — the self-bind
        # guard (a separate feature) requires an announce-then-confirm round
        # trip; not what this test is about, so seed the announcement
        # directly and confirm in one call.
        with _seeded_pending_switch(mod, "g_arch"):
            bind_result = await mod.strata_bind(scope_id="g_arch", confirm=True)
        assert bind_result["scope_id"] == "g_arch"
        assert mod._AGENT_SCOPE == "g_arch"

        await mod.strata_contribute(
            scope_id="g_arch",
            content="Bound to g_arch and contributing.",
            proposed_classification="context",
        )

    with RecordStore(db_path) as rs:
        contributions = rs.list_contributions(scope_id="g_arch")
    assert len(contributions) == 1
    assert contributions[0].contributor.scope_id == "g_arch"
    assert contributions[0].contributor.session_id == "sess_bind_test"


# ---------------------------------------------------------------------------
# Review follow-up: strata_bind refusal carries the active fleet-reload
# warning (the exact incident — an invalid fleet.yaml edit falls back
# silently, then a bind for the scope that edit meant to add fails with no
# explanation of why it's still invisible).
# ---------------------------------------------------------------------------


async def test_bind_refusal_mentions_active_fleet_reload_warning(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with patch.object(mod, "_AGENT_SCOPE", "g_backend"):
        # Prime the reloader with a good fleet.
        mod.strata_list_scopes()

        # Corrupt the file the way the incident describes: an edit meant to
        # add a scope, but invalid — falls back silently to the last good
        # fleet.
        fleet_path.write_text(
            "strata:\n  - id: L0\n    name: exec\n    ordinal: 0\n"
            "scopes:\n  - id: g_new\n    name: bad\n    stratum_id: NOPE\n"
            "edges: []\n",
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError) as exc_info:
            await mod.strata_bind(scope_id="g_new")

    message = str(exc_info.value)
    assert "g_new" in message
    assert "not found" in message
    assert "fleet reload warning" in message
    assert "fleet.yaml" in message


# ---------------------------------------------------------------------------
# Review follow-up: strata_bind applies a scope's default_skill when skill is
# omitted — the same companion rule startup auto-bind applies, via the
# shared _resolve_skill_default helper (not reimplemented inline).
# ---------------------------------------------------------------------------


async def test_bind_applies_default_skill_when_omitted(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet = {
        "strata": [{"id": "L0", "name": "exec", "ordinal": 0}],
        "scopes": [
            {
                "id": "g_defaulted",
                "name": "Defaulted",
                "stratum_id": "L0",
                "default_skill": "strata-developer",
                "permitted_skills": ["strata-developer", "strata-reviewer"],
            }
        ],
        "edges": [],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with patch.object(mod, "_AGENT_SCOPE", ""), patch.object(mod, "_AGENT_SKILL", None):
        # No skill given — must fall back to default_skill rather than
        # refusing with "no skill was given" (misleading when one exists).
        result = await mod.strata_bind(scope_id="g_defaulted")

        assert result["skill"] == "strata-developer"
        assert mod._AGENT_SKILL == "strata-developer"


# ---------------------------------------------------------------------------
# Review follow-up: archived-scope refusal is one shared rule, applied at
# BOTH startup binding validation and strata_bind (not reimplemented twice).
# ---------------------------------------------------------------------------


async def test_bind_refuses_archived_scope_via_shared_check(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet = {
        "strata": [{"id": "L0", "name": "exec", "ordinal": 0}],
        "scopes": [{"id": "g_gone", "name": "Retired", "stratum_id": "L0", "status": "archived"}],
        "edges": [],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with patch.object(mod, "_AGENT_SCOPE", ""), pytest.raises(RuntimeError, match="archived"):
        await mod.strata_bind(scope_id="g_gone")


# ---------------------------------------------------------------------------
# Review follow-up: strata_contribute snapshots (scope, skill, session_id)
# ONCE at the top and uses those locals for both authorization and the
# stamped provenance — so a rebind landing between the two can never produce
# "authorized against X, stamped as Y."
# ---------------------------------------------------------------------------


async def test_contribute_stamps_provenance_with_the_scope_it_authorized_against(
    tmp_path: Path,
) -> None:
    """A binding change that happens mid-call must not split authorize/stamp.

    Simulates the hazard directly: _check_entitled_write is monkeypatched to
    mutate mod._AGENT_SCOPE as a side effect (standing in for a concurrent
    strata_bind landing between the authorization check and the provenance
    stamp). The stamped contribution must still carry the ORIGINAL scope —
    the one strata_contribute snapshotted before calling _check_entitled_write
    — never the mutated global.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    fake_judgment = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="fine",
        new_summary=_make_summary("g_backend", "updated"),
    )

    real_check = mod._check_entitled_write

    def mutating_check(fleet, agent_scope, scope_id, **kwargs):
        # Stand-in for a concurrent strata_bind landing here.
        mod._AGENT_SCOPE = "g_arch"
        return real_check(fleet, agent_scope, scope_id, **kwargs)

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_AGENT_SKILL", "strata-developer"),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_race"),
        patch.object(mod, "_check_entitled_write", side_effect=mutating_check),
        patch("strata.scope_manager.ScopeManager.judge", return_value=fake_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        await mod.strata_contribute(
            scope_id="g_backend",
            content="Provenance must not split from authorization.",
            proposed_classification="context",
        )

    with RecordStore(db_path) as rs:
        contributions = rs.list_contributions(scope_id="g_backend")
    assert len(contributions) == 1
    # Stamped with the scope strata_contribute snapshotted at the top
    # (g_backend), NOT the value _AGENT_SCOPE was mutated to mid-call.
    assert contributions[0].contributor.scope_id == "g_backend"


# ---------------------------------------------------------------------------
# Soft-start (Change 1, dated addendum to ADR 0005 D5): a 2+ scope fleet with
# no binding resolved at startup no longer exits the process. The server
# always starts; every memory tool but strata_bind returns the aggregated
# startup-failure list (plus recovery instructions) as its error result
# until the session is bound. strata_bind is the recovery path and must work
# in this unresolved state, including re-reading a fleet fixed after
# startup.
# ---------------------------------------------------------------------------


async def test_unbound_tool_call_returns_error_with_scopes_and_bind_mention(tmp_path: Path) -> None:
    """A memory tool call while unresolved returns the startup failures, the
    fleet's scope ids, and a strata_bind mention — never a bare traceback."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend — two scopes

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(
            mod,
            "_STARTUP_ERRORS_BINDING",
            ["STRATA_AGENT_SCOPE is not set.\n  Available scope IDs: g_arch, g_backend."],
        ),
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError) as exc_info,
    ):
        await mod.strata_read_perspective()

    message = str(exc_info.value)
    assert "g_arch" in message
    assert "g_backend" in message
    assert "strata_bind" in message


async def test_unbound_tool_call_error_carries_no_issue_references(tmp_path: Path) -> None:
    """Operator-facing output must never carry internal issue numbers — the
    same rule test_register.py's next-steps text enforces, and a QA-round-2
    finding against a stale binding_errors string that used to say
    '... issue #121.'"""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_AGENT_SKILL", None),
        patch.object(mod, "_UNRESOLVED", True),
        # Exercise the real skill-unset message text, not a stand-in — this
        # is the exact code path the leaked "issue #121" reference lived in.
        patch.object(
            mod,
            "_STARTUP_ERRORS_BINDING",
            _validate_binding_skill_error(fleet),
        ),
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError) as exc_info,
    ):
        await mod.strata_read_perspective()

    assert "issue #" not in str(exc_info.value).lower()


async def test_tool_descriptions_carry_no_issue_references(tmp_path: Path) -> None:
    """FastMCP ships each tool's docstring to clients verbatim as its
    description — an agent reads these before ever calling the tool, so
    they are exactly as user-facing as any error message or --help text.
    Reviewer follow-up: 11 tool docstrings (strata_contribute,
    strata_rejudge, strata_read_scope_summary, strata_read_perspective,
    strata_read_scope_record, strata_read_contribution,
    strata_session_stats, strata_session_closeout) still carried internal
    '(issue #NN)' references.

    Drives the REAL client-visible surface — mod.mcp.list_tools(), the same
    call an MCP client makes — rather than grepping source text, so this
    catches a leak regardless of which tool it's in or how the docstring is
    wrapped.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    tools = await mod.mcp.list_tools()
    assert tools, "expected at least one registered tool"

    leaked = {
        t.name: t.description for t in tools if t.description and "issue #" in t.description.lower()
    }
    assert not leaked, f"tool description(s) leaked an issue number: {leaked}"


def _validate_binding_skill_error(fleet: FleetConfig) -> list[str]:
    """Build the real STRATA_AGENT_SKILL-unset binding_errors list for a
    2+ scope fleet — used to prove the guard test above exercises the
    actual message text, not a hand-written stand-in."""
    from strata.mcp.server import _validate_binding

    _scope, _skill, _config_errors, binding_errors = _validate_binding(
        fleet,
        scope="g_backend",
        skill="",
        project_config_found=True,
    )
    return binding_errors


def _validate_binding_scope_and_skill_errors(fleet: FleetConfig) -> list[str]:
    """Build the real binding_errors list for BOTH STRATA_AGENT_SCOPE and
    STRATA_AGENT_SKILL unset at once — the exact two-item shape from the
    live-session finding ("[1] STRATA_AGENT_SCOPE is not set. ... [2]
    STRATA_AGENT_SKILL is not set. ..."), so the guard test below exercises
    the actual per-failure strings _validate_binding builds, not
    hand-written stand-ins."""
    from strata.mcp.server import _validate_binding

    _scope, _skill, _config_errors, binding_errors = _validate_binding(
        fleet,
        scope="",
        skill="",
        project_config_found=True,
    )
    return binding_errors


async def test_startup_validator_error_items_route_through_the_user(tmp_path: Path) -> None:
    """Live-session finding: the per-failure strings _validate_binding
    builds for an unset STRATA_AGENT_SCOPE and an unset STRATA_AGENT_SKILL
    still read "Call strata_bind(scope_id=<one of the above>) to bind this
    session now" / "Call strata_bind(scope_id=..., skill=...) to bind this
    session now" — self-bind instructions that slipped past the earlier
    rewording rounds because the existing guard test
    (test_reworded_bind_texts_route_through_the_user) exercised a
    hand-written stand-in string, never the real validator output.

    This drives the REAL _validate_binding output — both failures at
    once, matching the exact two-item shape the operator saw — through an
    actual gated tool call, and asserts on the EXACT rendered surface: the
    full error text contains "ask the user" (case-insensitive), and never
    carries the old self-serve phrasing.
    """
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)
    binding_errors = _validate_binding_scope_and_skill_errors(fleet)
    assert len(binding_errors) == 2, "expected both the scope- and skill-unset items"

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", binding_errors),
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError) as exc_info,
    ):
        await mod.strata_read_perspective()

    rendered = _normalize(str(exc_info.value))
    assert "ask the user" in rendered
    # The exact self-serve phrasing the operator saw, gone from the
    # rendered surface — not just softened elsewhere in the message.
    assert "to bind this session now" not in rendered
    assert "call strata_bind(scope_id=<one of the above>)" not in rendered
    assert "call strata_bind(scope_id=..., skill=" not in rendered
    # Live incident: an unbound agent shell-read .strata/summaries/g_root.md
    # directly instead of asking the user — the exact surface an agent
    # reads at the moment of temptation must warn it off that.
    assert "never read or write files under .strata/ directly" in rendered


async def test_unbound_error_leads_with_stop_imperative_before_failure_details(
    tmp_path: Path,
) -> None:
    """Live-replay finding: an agent hit this exact error on its very first
    question and answered from the repo anyway, without asking the user
    anything — it treated the numbered failure details and recovery
    mechanics as background information rather than a blocking instruction,
    since nothing at the very top of the message said "stop." The message
    must now LEAD with an unmissable imperative — before any '[1]'-numbered
    detail — telling the agent not to answer yet and to ask the user which
    scope to act as right now."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)
    binding_errors = _validate_binding_scope_and_skill_errors(fleet)

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", binding_errors),
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError) as exc_info,
    ):
        await mod.strata_read_perspective()

    text = str(exc_info.value)
    lowered = text.lower()

    assert "stop" in lowered
    assert "do not answer" in lowered
    assert "ask the user now" in lowered

    # The imperative must come BEFORE the first numbered failure item —
    # not after it, and not only somewhere further down in the recovery
    # mechanics.
    stop_index = lowered.index("stop")
    first_item_index = text.index("[1]")
    assert stop_index < first_item_index, (
        "the STOP imperative must precede the first '[1]' failure item, "
        f"got stop at {stop_index}, '[1]' at {first_item_index}:\n{text}"
    )


def test_unbound_notice_leads_with_stop_imperative(tmp_path: Path) -> None:
    """Same finding, the other unbound surface: strata_list_scopes's
    unbound_notice already told the agent to ask the user, but the ask
    must LEAD the notice, not be buried after mechanics an agent could
    read as optional."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        result = mod.strata_list_scopes()

    notice = result["unbound_notice"]
    lowered = notice.lower()
    assert "stop" in lowered
    assert "do not answer" in lowered
    assert "ask the user now" in lowered
    assert lowered.index("stop") < lowered.index("ask the user now")


async def test_unbound_tool_call_raises_for_every_memory_tool_but_bind(tmp_path: Path) -> None:
    """Every memory tool but strata_bind is gated — not just one of them."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        with pytest.raises(RuntimeError):
            await mod.strata_read_perspective()
        with pytest.raises(RuntimeError):
            await mod.strata_read_scope_summary()
        with pytest.raises(RuntimeError):
            await mod.strata_session_stats()
        with pytest.raises(RuntimeError):
            await mod.strata_session_closeout(reason="n/a")


async def test_strata_bind_works_while_unresolved(tmp_path: Path) -> None:
    """strata_bind is THE recovery path — it must work while unresolved, and
    resolve the session so subsequent tool calls stop erroring."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
    ):
        result = await mod.strata_bind(scope_id="g_backend")
        assert result["scope_id"] == "g_backend"
        assert mod._UNRESOLVED is False

        # Bound now — a subsequent tool call must work, not error.
        listing = mod.strata_list_scopes()
        assert {s["id"] for s in listing["scopes"]} == {"g_arch", "g_backend"}


# ---------------------------------------------------------------------------
# Ungate audit follow-up: strata_list_scopes reads only fleet topology — no
# scope's summary, record, perspective, or session state — so it works
# unbound (controller ruling: topology is not scoped memory, and an agent
# helping the user bind should be able to look at the fleet). Every other
# gated tool stays gated (see test_unbound_tool_call_raises_for_every_
# memory_tool_but_bind, above, and test_unbound_tool_call_returns_error_
# with_scopes_and_bind_mention, which now exercises a genuine memory tool).
# ---------------------------------------------------------------------------


async def test_strata_list_scopes_works_unbound_and_carries_notice(tmp_path: Path) -> None:
    """strata_list_scopes must NOT be gated: it returns the fleet topology
    while unresolved, with a one-line unbound_notice appended so the state
    stays visible rather than looking like an ordinary bound call."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        # Must NOT raise — pure topology reads work unbound.
        result = mod.strata_list_scopes()

    assert {s["id"] for s in result["scopes"]} == {"g_arch", "g_backend"}
    assert "unbound_notice" in result
    assert "strata_bind(scope_id=...)" in result["unbound_notice"]


async def test_strata_list_scopes_has_no_notice_once_bound(tmp_path: Path) -> None:
    """Once resolved, strata_list_scopes carries no unbound_notice — an
    ordinary bound call must not look like it's still waiting on a bind."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_UNRESOLVED", False),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        result = mod.strata_list_scopes()

    assert "unbound_notice" not in result


async def test_unresolved_message_mentions_strata_list_scopes_works_unbound(
    tmp_path: Path,
) -> None:
    """The gated-tool error text must reflect reality: strata_list_scopes no
    longer needs strata_bind first, so the recovery text should point an
    agent at it for the full fleet picture while still unbound."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError) as exc_info,
    ):
        await mod.strata_read_perspective()

    assert "strata_list_scopes" in str(exc_info.value)


async def test_fleet_missing_at_startup_then_created_bindable_without_restart(
    tmp_path: Path,
) -> None:
    """The exact incident: fleet.yaml missing at startup (a binding-class
    failure — an unwritten fleet.yaml loads as the empty fleet, no scopes,
    so STRATA_AGENT_SCOPE can never resolve — unresolved), created
    afterward, and bindable with no server restart — strata_bind re-reads
    fleet.yaml itself via the reloader."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = tmp_path / "fleet.yaml"  # deliberately does not exist yet

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
    ):
        with pytest.raises(RuntimeError):
            await mod.strata_read_perspective()

        # Fleet created after startup, out of band — no server restart.
        fleet_data = {
            "strata": [{"id": "L0", "name": "root", "ordinal": 0}],
            "scopes": [{"id": "g_solo", "name": "Solo", "stratum_id": "L0"}],
            "edges": [],
        }
        fleet_path.write_text(yaml.dump(fleet_data, default_flow_style=False), encoding="utf-8")

        result = await mod.strata_bind(scope_id="g_solo")
        assert result["scope_id"] == "g_solo"
        assert mod._UNRESOLVED is False


# ---------------------------------------------------------------------------
# Elicitation (Change 2): a tool call while unbound, with a fleet that loads
# fine, first attempts a server-initiated MCP elicitation offering a scope
# pick, through a real (in-memory) MCP session — the SDK test seam
# mcp.shared.memory.create_connected_server_and_client_session, driven by a
# fake elicitation_callback (accept / decline) or its absence (no capability
# declared).
# ---------------------------------------------------------------------------


async def _elicit_accept_g_backend(context, params):
    from mcp import types as mcp_types

    return mcp_types.ElicitResult(action="accept", content={"scope_id": "g_backend"})


async def _elicit_decline(context, params):
    from mcp import types as mcp_types

    return mcp_types.ElicitResult(action="decline")


def _result_text(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


async def test_unbound_two_scope_start_handshake_completes_and_tools_listable(
    tmp_path: Path,
) -> None:
    """Change 1's core claim at the protocol level: an unresolved 2-scope
    fleet still completes the MCP handshake, and every tool (including
    strata_bind, the recovery path) is listable."""
    from mcp.shared.memory import create_connected_server_and_client_session

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
    ):
        async with create_connected_server_and_client_session(mod.mcp) as client:
            tools = await client.list_tools()

    names = {t.name for t in tools.tools}
    assert "strata_bind" in names
    assert "strata_list_scopes" in names
    assert "strata_contribute" in names


async def test_elicitation_accept_binds_and_continues_original_call(tmp_path: Path) -> None:
    """A client that accepts the elicitation binds via the strata_bind path
    AND the original tool call proceeds against the new binding — a single
    round trip, not a second call."""
    from mcp.shared.memory import create_connected_server_and_client_session

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
    ):
        async with create_connected_server_and_client_session(
            mod.mcp, elicitation_callback=_elicit_accept_g_backend
        ) as client:
            result = await client.call_tool("strata_read_perspective", {})

        assert result.isError is not True
        assert mod._AGENT_SCOPE == "g_backend"
        assert mod._UNRESOLVED is False


async def test_elicitation_scope_pick_prompt_routes_through_the_user(tmp_path: Path) -> None:
    """Item 3 re-verification: the scope-pick elicitation prompt sent to
    the client (Change 2, _attempt_elicit_bind) must still frame this as a
    question for the user, not an instruction the agent could satisfy on
    its own judgment — captured and asserted on directly, not just eyeballed
    in source, so a future edit can't silently drop the framing again."""
    from mcp.shared.memory import create_connected_server_and_client_session

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    captured_messages: list[str] = []

    async def _capture_and_decline(context, params):
        captured_messages.append(params.message)
        from mcp import types as mcp_types

        return mcp_types.ElicitResult(action="decline")

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
    ):
        async with create_connected_server_and_client_session(
            mod.mcp, elicitation_callback=_capture_and_decline
        ) as client:
            await client.call_tool("strata_read_perspective", {})

    assert captured_messages, "expected the elicitation prompt to have been sent"
    assert "ask the user" in captured_messages[0].lower()


async def test_elicitation_switch_confirm_prompt_is_a_direct_question(tmp_path: Path) -> None:
    """Item 3 re-verification: the switch-confirmation elicitation prompt
    (_attempt_elicit_switch_confirm) IS the ask-the-user mechanism itself —
    it should read as a direct yes/no question to whoever answers it, never
    as an instruction the agent could resolve on its own."""
    from mcp.shared.memory import create_connected_server_and_client_session

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    captured_messages: list[str] = []

    async def _capture_and_confirm(context, params):
        captured_messages.append(params.message)
        from mcp import types as mcp_types

        return mcp_types.ElicitResult(action="accept", content={"confirm": True})

    with patch.object(mod, "_AGENT_SCOPE", "g_arch"), patch.object(mod, "_AGENT_SKILL", None):
        async with create_connected_server_and_client_session(
            mod.mcp, elicitation_callback=_capture_and_confirm
        ) as client:
            await client.call_tool("strata_bind", {"scope_id": "g_backend"})

    assert captured_messages, "expected the switch-confirmation prompt to have been sent"
    prompt = captured_messages[0].lower()
    assert "confirm" in prompt
    assert "g_arch" in prompt
    assert "g_backend" in prompt
    # A direct question, not a self-bind instruction.
    assert "call strata_bind" not in prompt


async def test_elicitation_only_attempted_once_then_bound_calls_skip_it(tmp_path: Path) -> None:
    """Single-round-trip guard: after an accepted elicitation binds the
    session, a second tool call must not elicit again — it is already
    resolved, so _require_bound_or_elicit is a no-op before it ever reaches
    the elicitation machinery."""
    from mcp.shared.memory import create_connected_server_and_client_session

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    calls = 0

    async def _counting_accept(context, params):
        nonlocal calls
        calls += 1
        from mcp import types as mcp_types

        return mcp_types.ElicitResult(action="accept", content={"scope_id": "g_backend"})

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
    ):
        async with create_connected_server_and_client_session(
            mod.mcp, elicitation_callback=_counting_accept
        ) as client:
            first = await client.call_tool("strata_read_perspective", {})
            second = await client.call_tool("strata_read_perspective", {})

        assert first.isError is not True
        assert second.isError is not True
        assert calls == 1


async def test_elicitation_accept_with_invalid_scope_falls_back_binding_unchanged(
    tmp_path: Path,
) -> None:
    """An accepted pick that fails _resolve_bind (unknown scope_id) falls
    back to the plain Change-1 error — the binding is left unchanged, same
    guarantee strata_bind itself gives on a rejected bind."""
    from mcp.shared.memory import create_connected_server_and_client_session

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend — no "g_nope"

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    async def _accept_bad_scope(context, params):
        from mcp import types as mcp_types

        return mcp_types.ElicitResult(action="accept", content={"scope_id": "g_nope"})

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
    ):
        async with create_connected_server_and_client_session(
            mod.mcp, elicitation_callback=_accept_bad_scope
        ) as client:
            result = await client.call_tool("strata_read_perspective", {})

        assert result.isError is True
        text = _result_text(result)
        assert "strata_bind" in text
        # Binding left unchanged — same as any other failed bind attempt.
        assert mod._AGENT_SCOPE == ""
        assert mod._UNRESOLVED is True


async def test_elicitation_decline_falls_back_to_unresolved_error(tmp_path: Path) -> None:
    """A client that declines the elicitation falls back silently to the
    plain Change-1 error result — never a raw elicitation-protocol error."""
    from mcp.shared.memory import create_connected_server_and_client_session

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
    ):
        async with create_connected_server_and_client_session(
            mod.mcp, elicitation_callback=_elicit_decline
        ) as client:
            result = await client.call_tool("strata_read_perspective", {})

        assert result.isError is True
        text = _result_text(result)
        assert "strata_bind" in text
        assert mod._AGENT_SCOPE == ""
        assert mod._UNRESOLVED is True


async def test_elicitation_missing_capability_falls_back_to_unresolved_error(
    tmp_path: Path,
) -> None:
    """A client that never declares the elicitation capability (no callback
    given — the SDK's own signal for "not supported") gets the plain
    Change-1 error, with no elicitation round trip attempted."""
    from mcp.shared.memory import create_connected_server_and_client_session

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
    ):
        async with create_connected_server_and_client_session(mod.mcp) as client:
            result = await client.call_tool("strata_read_perspective", {})

        assert result.isError is True
        text = _result_text(result)
        assert "strata_bind" in text
        assert mod._AGENT_SCOPE == ""
        assert mod._UNRESOLVED is True


async def test_elicitation_never_attempted_from_within_strata_bind(tmp_path: Path) -> None:
    """strata_bind never elicits — it IS the elicitation's own bind target,
    so eliciting from inside it would be circular. Calling it directly
    (bypassing the guard entirely, as it always does) must not touch the
    elicitation machinery at all."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
        patch.object(
            mod,
            "_attempt_elicit_bind",
            side_effect=AssertionError("strata_bind must never elicit"),
        ),
    ):
        result = await mod.strata_bind(scope_id="g_backend")
        assert result["scope_id"] == "g_backend"


# ---------------------------------------------------------------------------
# Review follow-up (fix round): elicitation timeout, per-class gate
# clearing, and the decline-latch reset. See _validate_binding's and
# _attempt_elicit_bind's docstrings (src/strata/mcp/server.py) for the two
# incidents these close: (a) a capability-declaring client that never
# answers used to hang the tool call forever — no timeout was threaded
# through Context.elicit()/ServerSession.elicit_form(), even though the SDK
# primitive underneath (BaseSession.send_request) supports one; (b)
# strata_bind used to clear the WHOLE startup-failure list unconditionally,
# so a "successful" bind after a broken .strata/config.toml would silently
# look fully resolved while the server kept writing to the wrong
# (env-fallback) storage location.
# ---------------------------------------------------------------------------


async def test_elicitation_timeout_falls_back_and_marks_unavailable(tmp_path: Path) -> None:
    """A capability-declaring client that never answers must not hang the
    tool call forever — _ELICIT_TIMEOUT bounds the wait via
    request_read_timeout_seconds threaded into session.send_request
    (_send_scope_pick_elicitation), and expiry is treated exactly like a
    protocol-level non-accept: silent fallback to the readable error
    (never a claim about what the user decided), plus the same
    _ELICIT_UNAVAILABLE memo (no re-prompt/re-hang on the very next call)."""
    from datetime import timedelta

    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    async def _never_respond(context, params):
        # Sleeps well past the (shrunk, below) server-side timeout. The
        # server's send_request races anyio.fail_after independently of
        # this client-side task, so this never needs to actually finish —
        # create_connected_server_and_client_session cancels it on exit.
        await anyio.sleep(5)
        from mcp import types as mcp_types

        return mcp_types.ElicitResult(action="decline")  # pragma: no cover

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
        patch.object(mod, "_ELICIT_TIMEOUT", timedelta(milliseconds=50)),
    ):
        async with create_connected_server_and_client_session(
            mod.mcp, elicitation_callback=_never_respond
        ) as client:
            result = await client.call_tool("strata_read_perspective", {})

            assert result.isError is True
            text = _result_text(result)
            assert "strata_bind" in text

            # Marked non-functional exactly like a protocol decline: a
            # second unbound call must not hang out the same unresponsive
            # client again.
            second = await client.call_tool("strata_read_perspective", {})
            assert second.isError is True

        assert mod._ELICIT_UNAVAILABLE is True
        assert mod._AGENT_SCOPE == ""


async def test_elicit_unavailable_memo_cleared_by_successful_bind(tmp_path: Path) -> None:
    """The memo docstring's claim must be TRUE (review follow-up: it used
    to say 'cleared implicitly by any successful bind' while nothing in the
    code ever cleared it): a protocol-level decline marks elicitation
    non-functional (no re-ask on the very next call), and a subsequent
    successful strata_bind clears it — a client marked non-functional once
    isn't locked out of elicitation forever by that."""
    from mcp.shared.memory import create_connected_server_and_client_session

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
    ):
        async with create_connected_server_and_client_session(
            mod.mcp, elicitation_callback=_elicit_decline
        ) as client:
            result = await client.call_tool("strata_read_perspective", {})
            assert result.isError is True

        assert mod._ELICIT_UNAVAILABLE is True

        # A later successful strata_bind clears the memo.
        bind_result = await mod.strata_bind(scope_id="g_backend")
        assert bind_result["scope_id"] == "g_backend"
        assert mod._ELICIT_UNAVAILABLE is False


def test_reset_elicit_state_clears_latch_and_pending_switch_without_a_bind(
    tmp_path: Path,
) -> None:
    """Review follow-up (final fix wave, item 4): _ELICIT_UNAVAILABLE was
    process-global mutable state with no reset path OTHER than a successful
    strata_bind — a test (or a future caller) that needs to clear it without
    also performing a bind had to reach for the sledgehammer of reloading
    the whole module via sys.modules. _reset_elicit_state() is the single,
    explicit, testable reset path for both elicit-adjacent globals
    (_ELICIT_UNAVAILABLE and its sibling _PENDING_SWITCH, which strata_bind's
    own successful-bind path already clears together — see its comment)."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)
    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    mod._ELICIT_UNAVAILABLE = True
    mod._PENDING_SWITCH = mod._PendingSwitch(target_scope_id="g_backend", requested_at=0.0)

    mod._reset_elicit_state()

    assert mod._ELICIT_UNAVAILABLE is False
    assert mod._PENDING_SWITCH is None


async def test_config_class_failure_survives_a_successful_binding_class_bind(
    tmp_path: Path,
) -> None:
    """Review follow-up incident: strata_bind must clear ONLY binding-class
    startup failures. A broken .strata/config.toml means the server may
    have opened storage at the wrong (env-fallback) location — no scope
    pick fixes that, so memory tools must stay gated, naming the config
    problem, even after strata_bind otherwise succeeds at the scope/skill
    it was actually asked to fix."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_arch, g_backend

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    config_error = (
        ".strata/config.toml is invalid: TOML parse error.\n"
        "  Fix the file (or delete it and re-run `strata register`), then restart "
        "the server — this is read once, at process start."
    )

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
        patch.object(mod, "_STARTUP_ERRORS_CONFIG", [config_error]),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        # The bind itself succeeds — the scope/skill problem strata_bind was
        # asked to fix is fully valid and gets fixed.
        result = await mod.strata_bind(scope_id="g_backend")
        assert result["scope_id"] == "g_backend"
        # Told, in the same result, that memory tools remain gated anyway.
        assert "config_notice" in result

        # Binding-class cleared...
        assert mod._STARTUP_ERRORS_BINDING == []
        # ...but the config-class failure is untouched, so the session
        # stays gated overall.
        assert [config_error] == mod._STARTUP_ERRORS_CONFIG
        assert mod._UNRESOLVED is True

        # A memory tool call must still be refused, naming the config
        # problem specifically.
        with pytest.raises(RuntimeError) as exc_info:
            await mod.strata_read_perspective()

        message = str(exc_info.value)
        assert "config.toml is invalid" in message
        assert "restart" in message.lower()
        # It must NOT re-suggest strata_bind for the scope/skill — that
        # part is already fixed and re-suggesting it would be misleading.
        assert "Call strata_bind" not in message

        # Pure fleet topology stays visible regardless (Change 1's ungate
        # follow-up) — this is not a binding problem.
        listing = mod.strata_list_scopes()
        assert {s["id"] for s in listing["scopes"]} == {"g_arch", "g_backend"}


async def test_config_class_failure_skips_elicitation_entirely(tmp_path: Path) -> None:
    """A config-class failure gates a scope pick from ever being offered —
    there is no point eliciting when an answer would not unblock the
    session (_require_bound_or_elicit only attempts elicitation when
    _STARTUP_ERRORS_CONFIG is empty)."""
    from mcp.shared.memory import create_connected_server_and_client_session

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    calls = 0

    async def _counting_accept(context, params):
        nonlocal calls
        calls += 1
        from mcp import types as mcp_types

        return mcp_types.ElicitResult(action="accept", content={"scope_id": "g_backend"})

    with (
        patch.object(mod, "_AGENT_SCOPE", ""),
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["STRATA_AGENT_SCOPE is not set."]),
        patch.object(
            mod,
            "_STARTUP_ERRORS_CONFIG",
            [".strata/config.toml is invalid: bad toml.\n  Fix and restart."],
        ),
    ):
        async with create_connected_server_and_client_session(
            mod.mcp, elicitation_callback=_counting_accept
        ) as client:
            result = await client.call_tool("strata_read_perspective", {})

        assert result.isError is True
        text = _result_text(result)
        assert "config.toml is invalid" in text
        assert calls == 0  # never even asked
        assert mod._AGENT_SCOPE == ""
