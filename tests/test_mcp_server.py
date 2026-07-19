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
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Make strata importable when running from the repo root.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.fleet_config import FleetConfig  # noqa: E402
from strata.migrator import run_migrations  # noqa: E402
from strata.record_store import ContributorRef, RecordStore  # noqa: E402
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
    """Write a fleet.yaml exercising ADR 0006 D3/D4 (peer-reference composition).

    Topology: g_exec (L0) ← g_func (L1) ← g_team (L2).

    Reference edges (intra-stratum, context only):
      - g_func → g_peer_a   (referenced by a *chain* scope — must appear)
      - g_func → g_peer_b   (second chain-referenced peer — ordering)
      - g_exec → g_exec_peer (referenced by an *ancestor* — must also appear)
      - g_peer_a → g_peer_of_peer (peer-of-peer — one hop only, must NOT
        appear in g_team's perspective since g_peer_a is not itself on the
        chain)

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
            {"id": "g_peer_a", "name": "Peer A", "stratum_id": "L1"},
            {"id": "g_peer_b", "name": "Peer B", "stratum_id": "L1"},
            {"id": "g_peer_of_peer", "name": "Peer Of Peer", "stratum_id": "L1"},
            {"id": "g_sibling", "name": "Unreferenced Sibling", "stratum_id": "L1"},
        ],
        "edges": [
            # Inter-stratum: child → parent
            {"from": "g_func", "to": "g_exec"},
            {"from": "g_team", "to": "g_func"},
            # Intra-stratum peer references (context only)
            {"from": "g_func", "to": "g_peer_b"},
            {"from": "g_func", "to": "g_peer_a"},
            {"from": "g_exec", "to": "g_exec_peer"},
            {"from": "g_peer_a", "to": "g_peer_of_peer"},
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
# Test 1: strata_contribute writes to RecordStore without HTTP server
# ---------------------------------------------------------------------------


def test_contribute_writes_to_record_store_without_http(tmp_path: Path) -> None:
    """strata_contribute must append a contribution to RecordStore in-process."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    # Patch _load_fleet to return our test fleet directly (avoids disk path issues).
    fleet = FleetConfig.load(fleet_path)

    # We mock the scope-manager so we don't need a real Anthropic key.
    fake_judgment = MagicMock()
    fake_judgment.decision = "accept_as_context"
    fake_judgment.reasoning = "Valid observation."
    fake_judgment.new_summary = _make_summary("g_arch", "updated context")

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_AGENT_SKILL", "strata-developer"),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=fake_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        result = mod.strata_contribute(
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


def test_read_scope_summary_reads_from_summary_store(tmp_path: Path) -> None:
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
        result = mod.strata_read_scope_summary("g_arch")

    assert result["scope_id"] == "g_arch"
    assert result["context"] == "arch context from disk"
    assert result["directives"] == []
    assert "updated_at" in result


def test_read_scope_summary_no_summary_yet_reports_version_zero_and_not_exists(
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
        result = mod.strata_read_scope_summary("g_arch")

    assert result["version"] == 0
    assert result["exists"] is False


def test_read_scope_summary_after_first_write_reports_version_one_and_exists(
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
        result = mod.strata_read_scope_summary("g_arch")

    assert result["version"] == 1
    assert result["exists"] is True


# ---------------------------------------------------------------------------
# Test 3: strata_read_perspective returns layers in root-first order
# ---------------------------------------------------------------------------


def test_read_perspective_returns_layers_root_first(tmp_path: Path) -> None:
    """strata_read_perspective returns a layered perspective (Decision 3).

    For g_backend (L1, child of g_arch L0) the perspective must have two
    layers: g_arch first (root), then g_backend (requested scope).
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
        result = mod.strata_read_perspective("g_backend")

    assert result["scope_id"] == "g_backend"
    assert result["_layers_count"] == 2
    layers = result["layers"]
    assert len(layers) == 2
    # Root-first ordering
    assert layers[0]["scope_id"] == "g_arch"
    assert layers[1]["scope_id"] == "g_backend"
    # Summary content is preserved per layer
    assert layers[0]["summary"]["context"] == "arch context"
    assert layers[1]["summary"]["context"] == "backend context"


def test_read_perspective_includes_operator_layers_for_bound_chain(tmp_path: Path) -> None:
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
        "directive",
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
        result = mod.strata_read_perspective("g_backend")

    layers = result["layers"]
    # Operator layer for g_arch immediately precedes g_arch's own layer;
    # g_backend has no operator memory, so it gets no operator layer.
    assert [layer["relation"] for layer in layers] == ["operator", "ancestor", "self"]
    operator_layer = layers[0]
    assert operator_layer["scope_id"] == "g_arch"
    assert operator_layer["stratum_id"] == "operator"
    assert operator_layer["binding"] is True
    assert operator_layer["operator_memory"]["directives"][0]["content"] == (
        "All services must use TLS 1.3."
    )
    assert operator_layer["operator_memory"]["context"] == []


# ---------------------------------------------------------------------------
# Test 4: strata_list_scopes re-reads fleet.yaml on each call
# ---------------------------------------------------------------------------


def test_list_scopes_re_reads_fleet_yaml_each_call(tmp_path: Path) -> None:
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


def test_read_scope_record_reads_from_record_store(tmp_path: Path) -> None:
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
        result = mod.strata_read_scope_record("g_arch")

    assert len(result["contributions"]) == 1
    assert result["contributions"][0]["content"] == "Use WAL mode for SQLite."
    assert len(result["judgments"]) == 1
    assert result["judgments"][0]["decision"] == "accept_as_directive"


# ---------------------------------------------------------------------------
# Test 6: strata_contribute raises RuntimeError for unknown scope
# ---------------------------------------------------------------------------


def test_contribute_raises_for_unknown_scope(tmp_path: Path) -> None:
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
        mod.strata_contribute(
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


def test_perspective_root_scope_returns_one_layer(tmp_path: Path) -> None:
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
        result = mod.strata_read_perspective("g_arch")

    assert result["scope_id"] == "g_arch"
    assert result["_layers_count"] == 1
    assert len(result["layers"]) == 1
    assert result["layers"][0]["scope_id"] == "g_arch"
    assert result["layers"][0]["summary"]["context"] == "root context"


# ---------------------------------------------------------------------------
# Test 9: Deep scope returns N+1 layers (root-first), correct order
# ---------------------------------------------------------------------------


def test_perspective_deep_scope_returns_layers_root_first(tmp_path: Path) -> None:
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
        result = mod.strata_read_perspective("g_team")

    assert result["scope_id"] == "g_team"
    assert result["_layers_count"] == 3
    layers = result["layers"]
    assert [layer["scope_id"] for layer in layers] == ["g_exec", "g_func", "g_team"]
    assert layers[0]["summary"]["context"] == "executive context"
    assert layers[1]["summary"]["context"] == "function context"
    assert layers[2]["summary"]["context"] == "team context"


# ---------------------------------------------------------------------------
# Test 10: an UNREFERENCED peer (intra-stratum, no reference edge) never
# appears — renamed from test_perspective_peer_edges_not_traversed now that
# ADR 0006 D3 composes *referenced* peers as context-only layers (see the
# "ADR 0006 D3/D4" section below for the referenced-peer tests).
# ---------------------------------------------------------------------------


def test_perspective_unreferenced_peer_never_appears(tmp_path: Path) -> None:
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
        result = mod.strata_read_perspective("g_team")

    layer_scope_ids = {layer["scope_id"] for layer in result["layers"]}
    assert "g_peer" not in layer_scope_ids, (
        "Unreferenced peer scope g_peer must not appear in the perspective layers"
    )
    # Exactly the inter-stratum chain: exec, func, team
    assert layer_scope_ids == {"g_exec", "g_func", "g_team"}


# ---------------------------------------------------------------------------
# Test 11: Missing ancestor summary → layer still present with empty content
# ---------------------------------------------------------------------------


def test_perspective_missing_ancestor_summary_produces_empty_layer(tmp_path: Path) -> None:
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
        result = mod.strata_read_perspective("g_backend")

    assert result["_layers_count"] == 2
    layers = result["layers"]
    # Root layer (g_arch) must be present even though no summary file exists
    root_layer = next(layer for layer in layers if layer["scope_id"] == "g_arch")
    assert root_layer["summary"]["directives"] == []
    assert root_layer["summary"]["context"] == ""
    # Issue #59: the synthesized layer is honest about being synthesized —
    # version=0/exists=False — not a look-alike for a real first write.
    assert root_layer["summary"]["version"] == 0
    assert root_layer["summary"]["exists"] is False

    # The child layer (g_backend) has a real on-disk summary, so it reports
    # a real first write: version=1, exists=True.
    child_layer = next(layer for layer in layers if layer["scope_id"] == "g_backend")
    assert child_layer["summary"]["version"] == 1
    assert child_layer["summary"]["exists"] is True


# ---------------------------------------------------------------------------
# Test 12: _v1_limitation key is absent (regression guard)
# ---------------------------------------------------------------------------


def test_perspective_no_v1_limitation_key(tmp_path: Path) -> None:
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
        result = mod.strata_read_perspective("g_arch")

    assert "_v1_limitation" not in result, (
        "_v1_limitation must be removed now that real perspective composition is implemented"
    )


# ---------------------------------------------------------------------------
# ADR 0006 D3 — peer-reference composition
#
# strata_read_perspective appends one layer per peer referenced (one hop)
# from any scope on the chain, labelled relation="peer_reference" and
# binding=False. Self/ancestor layers gain relation="self"/"ancestor" and
# binding=True. Uses _make_peer_composition_fleet_yaml: g_exec (L0) <-
# g_func (L1) <- g_team (L2), with g_func referencing g_peer_a and g_peer_b,
# g_exec referencing g_exec_peer, g_peer_a referencing g_peer_of_peer
# (two hops from g_team — must not appear), and g_sibling as an unreferenced
# L1 scope.
# ---------------------------------------------------------------------------


def test_perspective_self_and_ancestor_layers_are_binding(tmp_path: Path) -> None:
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
        result = mod.strata_read_perspective("g_team")

    chain_layers = {
        layer["scope_id"]: layer
        for layer in result["layers"]
        if layer["scope_id"] in {"g_exec", "g_func", "g_team"}
    }
    assert chain_layers["g_exec"]["relation"] == "ancestor"
    assert chain_layers["g_exec"]["binding"] is True
    assert chain_layers["g_func"]["relation"] == "ancestor"
    assert chain_layers["g_func"]["binding"] is True
    assert chain_layers["g_team"]["relation"] == "self"
    assert chain_layers["g_team"]["binding"] is True


def test_perspective_referenced_peer_appears_as_context_layer(tmp_path: Path) -> None:
    """A peer referenced by a chain scope appears with relation="peer_reference", binding=False.

    ADR 0007 D4: the peer layer carries that peer's PUBLICATION, not its
    internal summary — writing only a summary (no publication artifact)
    leaves the layer with an honestly empty ``publication.items`` list.
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
        result = mod.strata_read_perspective("g_team")

    peer_layer = next(layer for layer in result["layers"] if layer["scope_id"] == "g_peer_a")
    assert peer_layer["relation"] == "peer_reference"
    assert peer_layer["binding"] is False
    # Never the peer's internal summary — no "summary" key at all.
    assert "summary" not in peer_layer
    assert peer_layer["publication"] == {"items": []}


def test_perspective_referenced_peer_publication_composed_verbatim(tmp_path: Path) -> None:
    """A peer's PUBLISHED items compose into the peer layer, verbatim and labelled."""
    from strata.publication import PublishedItem, _write_publication

    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_peer_composition_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    ss = SummaryStore(summaries_dir)
    ss.write("g_peer_a", _make_summary("g_peer_a", "peer a internal context — must NOT appear"))
    _write_publication(
        "g_peer_a",
        [
            PublishedItem(
                id="pub_a1",
                kind="context",
                content="Peer A's outward status update.",
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
        result = mod.strata_read_perspective("g_team")

    peer_layer = next(layer for layer in result["layers"] if layer["scope_id"] == "g_peer_a")
    assert peer_layer["publication"]["items"] == [
        {
            "id": "pub_a1",
            "kind": "context",
            "content": "Peer A's outward status update.",
            "subject": "status",
            "anchors": ["subject:status"],
            "published_at": "2026-07-12T00:00:00+00:00",
        }
    ]
    # The internal summary's content never leaks into the composed layer.
    rendered = str(result)
    assert "must NOT appear" not in rendered


def test_perspective_ancestor_referenced_peer_appears(tmp_path: Path) -> None:
    """A peer referenced by an ANCESTOR (not the scope itself) still appears."""
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
        result = mod.strata_read_perspective("g_team")

    layer_scope_ids = {layer["scope_id"] for layer in result["layers"]}
    assert "g_exec_peer" in layer_scope_ids
    peer_layer = next(layer for layer in result["layers"] if layer["scope_id"] == "g_exec_peer")
    assert peer_layer["relation"] == "peer_reference"
    assert peer_layer["binding"] is False


def test_perspective_peer_of_peer_not_traversed(tmp_path: Path) -> None:
    """Only one hop is followed — a peer's own peer reference is not composed in."""
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
        result = mod.strata_read_perspective("g_team")

    layer_scope_ids = {layer["scope_id"] for layer in result["layers"]}
    assert "g_peer_of_peer" not in layer_scope_ids


def test_perspective_unreferenced_sibling_absent_alongside_referenced_peers(
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
        result = mod.strata_read_perspective("g_team")

    layer_scope_ids = {layer["scope_id"] for layer in result["layers"]}
    assert "g_sibling" not in layer_scope_ids


def test_perspective_peer_without_publication_reports_honestly_empty_face(tmp_path: Path) -> None:
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
        result = mod.strata_read_perspective("g_team")

    peer_layer = next(layer for layer in result["layers"] if layer["scope_id"] == "g_peer_a")
    assert "summary" not in peer_layer
    assert peer_layer["publication"] == {"items": []}


def test_perspective_peer_layers_sorted_by_scope_id(tmp_path: Path) -> None:
    """Peer layers are ordered by scope id for deterministic output, after self/ancestors."""
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
        result = mod.strata_read_perspective("g_team")

    layers = result["layers"]
    # Chain first (root-first: g_exec, g_func, g_team), then peer layers
    # sorted by scope id: g_exec_peer, g_peer_a, g_peer_b.
    assert [layer["scope_id"] for layer in layers] == [
        "g_exec",
        "g_func",
        "g_team",
        "g_exec_peer",
        "g_peer_a",
        "g_peer_b",
    ]
    peer_relations = [layer["relation"] for layer in layers[3:]]
    assert peer_relations == ["peer_reference", "peer_reference", "peer_reference"]


# ---------------------------------------------------------------------------
# ADR 0006 D4 — read surface reconciliation
#
# strata_read_scope_summary widens to the context surface (chain + peers
# referenced by that chain); strata_read_scope_record and the
# strata_read_perspective *target* stay chain-only. Uses the same
# peer-composition fleet as the D3 tests above.
# ---------------------------------------------------------------------------


def test_summary_read_of_referenced_peer_succeeds(tmp_path: Path) -> None:
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
        result = mod.strata_read_scope_summary("g_peer_a")

    assert result["scope_id"] == "g_peer_a"
    assert result["relation"] == "peer_reference"
    assert result["publication"] == {"items": []}
    assert "context" not in result


def test_summary_read_of_referenced_peer_returns_its_publication(tmp_path: Path) -> None:
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
        result = mod.strata_read_scope_summary("g_peer_a")

    assert result["scope_id"] == "g_peer_a"
    assert result["relation"] == "peer_reference"
    assert result["publication"]["items"][0]["id"] == "pub_a1"
    assert result["publication"]["items"][0]["content"] == (
        "Peer A's published directive (non-binding to us)."
    )


def test_summary_read_of_unreferenced_sibling_still_refused(tmp_path: Path) -> None:
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
        mod.strata_read_scope_summary("g_sibling")

    message = str(exc_info.value)
    assert "g_sibling" in message
    assert "g_team" in message


def test_record_read_of_referenced_peer_refused_chain_only(tmp_path: Path) -> None:
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
        mod.strata_read_scope_record("g_peer_a")

    message = str(exc_info.value)
    assert "g_peer_a" in message
    assert "chain-only" in message


def test_perspective_target_of_referenced_peer_refused(tmp_path: Path) -> None:
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
        mod.strata_read_perspective("g_peer_a")

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


def test_entitled_no_argument_returns_bound_scope_data(tmp_path: Path) -> None:
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
        summary_result = mod.strata_read_scope_summary()
        perspective_result = mod.strata_read_perspective()
        record_result = mod.strata_read_scope_record()

    assert summary_result["scope_id"] == "g_team"
    assert summary_result["context"] == "team context"

    assert perspective_result["scope_id"] == "g_team"
    assert perspective_result["layers"][-1]["scope_id"] == "g_team"

    assert record_result == {"contributions": [], "judgments": [], "judgment_attempts": []}


def test_entitled_ancestor_read_allowed(tmp_path: Path) -> None:
    """Reading an inter-stratum ancestor of the bound scope is allowed."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_deep_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    ss = SummaryStore(summaries_dir)
    ss.write("g_exec", _make_summary("g_exec", "executive context"))

    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = ss
        result = mod.strata_read_scope_summary("g_exec")

    assert result["scope_id"] == "g_exec"
    assert result["context"] == "executive context"


def test_entitled_peer_read_raises_with_entitlement_message(tmp_path: Path) -> None:
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
            mod.strata_read_scope_summary("g_peer")

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
            mod.strata_read_perspective("g_peer")
        with pytest.raises(RuntimeError, match="entitled surface"):
            mod.strata_read_scope_record("g_peer")


def test_entitled_own_empty_record_returns_empty_shape(tmp_path: Path) -> None:
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
        result = mod.strata_read_scope_record("g_team")

    assert result == {"contributions": [], "judgments": [], "judgment_attempts": []}


# ---------------------------------------------------------------------------
# Entitlement edge cases (release-review findings)
# ---------------------------------------------------------------------------


def test_descendant_read_is_denied(tmp_path: Path) -> None:
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
        mod.strata_read_scope_summary("g_backend")  # its L1 child


def test_stale_bound_scope_gets_distinct_error(tmp_path: Path) -> None:
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
        mod.strata_read_perspective()


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


@pytest.mark.parametrize(
    "target_scope_id",
    ["g_team", "g_func", "g_exec"],
    ids=["own-scope", "parent", "root-grandparent"],
)
def test_contribute_within_write_surface_allowed(tmp_path: Path, target_scope_id: str) -> None:
    """Own scope, parent, and root/grandparent are all within the write surface."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_write_surface_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    fake_judgment = MagicMock()
    fake_judgment.decision = "accept_as_context"
    fake_judgment.reasoning = "Valid observation."
    fake_judgment.new_summary = _make_summary(target_scope_id, "updated context")

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_team")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=fake_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        result = mod.strata_contribute(
            scope_id=target_scope_id,
            content="within the write surface",
            proposed_classification="context",
        )

    assert result["judgment"]["decision"] == "accept_as_context"
    with RecordStore(db_path) as rs:
        contributions = rs.list_contributions(scope_id=target_scope_id)
    assert len(contributions) == 1
    assert contributions[0].content == "within the write surface"


def test_contribute_to_sibling_refused(tmp_path: Path) -> None:
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
        mod.strata_contribute(
            scope_id="g_team2",
            content="sideways contribution",
            proposed_classification="context",
        )

    message = str(exc_info.value)
    assert "g_team2" in message
    assert "g_team" in message


def test_contribute_to_descendant_refused(tmp_path: Path) -> None:
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
        mod.strata_contribute(
            scope_id="g_team",
            content="downward contribution",
            proposed_classification="context",
        )

    message = str(exc_info.value)
    assert "g_team" in message
    assert "g_func" in message


def test_refused_write_leaves_no_record_row(tmp_path: Path) -> None:
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
        mod.strata_contribute(
            scope_id="g_team2",
            content="sideways contribution",
            proposed_classification="context",
        )

    with RecordStore(db_path) as rs:
        assert rs.list_contributions(scope_id="g_team2") == []
        assert rs.list_judgments(scope_id="g_team2") == []


def test_refused_write_emits_warning_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
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
        mod.strata_contribute(
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


def test_contribute_raises_for_unknown_scope_before_entitlement_check(tmp_path: Path) -> None:
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
        mod.strata_contribute(
            scope_id="g_nonexistent",
            content="This should fail.",
            proposed_classification="context",
        )


def test_contribute_raises_for_archived_scope_before_entitlement_check(tmp_path: Path) -> None:
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
        mod.strata_contribute(
            scope_id="g_archived",
            content="This should fail.",
            proposed_classification="context",
        )

    # Must be the archived-scope error, not the write-entitlement error.
    assert "entitled write surface" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# ADR 0006 Decision D2 — the judge gets an entitlement signal
# ---------------------------------------------------------------------------


def test_contribute_passes_entitlement_view_to_judge(tmp_path: Path) -> None:
    """strata_contribute must compute and pass a non-None entitlement view to judge."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    fake_judgment = MagicMock()
    fake_judgment.decision = "accept_as_context"
    fake_judgment.reasoning = "Valid observation."
    fake_judgment.new_summary = _make_summary("g_arch", "updated context")

    judge_spy = MagicMock(return_value=fake_judgment)

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_AGENT_SKILL", "strata-developer"),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", judge_spy),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        mod.strata_contribute(
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


def test_contribute_judge_failure_records_attempt_and_points_to_rejudge(tmp_path: Path) -> None:
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
        mod.strata_contribute(
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


def test_strata_rejudge_recovers_pending_then_idempotent(tmp_path: Path) -> None:
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
        mod.strata_contribute(
            scope_id="g_backend",
            content="recover me",
            proposed_classification="context",
        )

    with RecordStore(db_path) as rs:
        contribution_id = rs.list_contributions(scope_id="g_backend")[0].id
        assert rs.list_judgments(scope_id="g_backend") == []

    # 2. First re-judge: the scope-manager is back — it judges and updates state.
    good_judgment = MagicMock()
    good_judgment.decision = "accept_as_context"
    good_judgment.reasoning = "recovered"
    good_judgment.new_summary = _make_summary("g_backend", "recovered context")

    scope_p2, skill_p2, session_p2 = _patch_agent_binding(mod, scope="g_backend")
    with (
        scope_p2,
        skill_p2,
        session_p2,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=good_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        result = mod.strata_rejudge(contribution_id)

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
        result2 = mod.strata_rejudge(contribution_id)

    assert result2["judgment"]["decision"] == "accept_as_context"
    assert result2["judgment"]["summary_updated"] is False
    with RecordStore(db_path) as rs:
        assert len(rs.list_judgments(scope_id="g_backend")) == 1


def test_strata_rejudge_unknown_contribution_raises(tmp_path: Path) -> None:
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
        mod.strata_rejudge("c_does_not_exist")


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


def test_strata_publish_acts_on_bound_scope_with_own_provenance(tmp_path: Path) -> None:
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
        result = mod.strata_publish(
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


def test_strata_publish_zero_anchors_raises_runtimeerror(tmp_path: Path) -> None:
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
        mod.strata_publish(content="x", kind="context", anchors=[])

    with RecordStore(db_path) as rs:
        assert rs.list_publication_acts(scope_id="g_backend") == []


def test_strata_withdraw_acts_on_bound_scope_with_own_provenance(tmp_path: Path) -> None:
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
        result = mod.strata_withdraw(act.id)

    assert result["judgment"]["decision"] == "accept"
    assert result["judgment"]["artifact_updated"] is True
    assert read_publication("g_backend", summaries_dir=summaries_dir) == []

    with RecordStore(db_path) as rs:
        acts = rs.list_publication_acts(scope_id="g_backend")
        withdraw_act = next(a for a in acts if a.act == "withdraw")
        assert withdraw_act.proposer.scope_id == "g_backend"
        assert withdraw_act.proposer.session_id == "sess_wd"


def test_strata_withdraw_unknown_item_raises_runtimeerror(tmp_path: Path) -> None:
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
        mod.strata_withdraw("pub_does_not_exist")


# ---------------------------------------------------------------------------
# Issue #110: per-session asymmetry counters + read receipts (mechanical)
# ---------------------------------------------------------------------------


def test_read_scope_summary_increments_session_reads(tmp_path: Path) -> None:
    """A summary read increments the session's reads counter and per-scope receipt."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    SummaryStore(summaries_dir).write("g_arch", _make_summary("g_arch", "ctx"))
    fleet = FleetConfig.load(fleet_path)

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend", session_id="sess_r")
    with scope_p, skill_p, session_p, patch.object(mod, "_load_fleet", return_value=fleet):
        mod.strata_read_scope_summary("g_arch")
        mod.strata_read_scope_summary("g_arch")

    # The state file exists, is readable, and records both reads of g_arch.
    state = mod._session_store.read("sess_r")
    assert state is not None
    assert state.reads == 2
    assert state.contributions == 0
    assert state.declines == 0
    assert state.reads_by_scope["g_arch"].count == 2
    assert state.reads_by_scope["g_arch"].last_read_at != ""


def test_read_perspective_records_read_for_target_scope(tmp_path: Path) -> None:
    """A perspective read is attributed to its target scope only, not its ancestors."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend", session_id="sess_p")
    with scope_p, skill_p, session_p, patch.object(mod, "_load_fleet", return_value=fleet):
        mod.strata_read_perspective("g_backend")

    state = mod._session_store.read("sess_p")
    assert state is not None
    assert state.reads == 1
    # g_backend is the target; g_arch (its ancestor layer) is NOT attributed a read.
    assert set(state.reads_by_scope) == {"g_backend"}


def test_session_stats_tool_returns_counters(tmp_path: Path) -> None:
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
        empty = mod.strata_session_stats()
        assert empty["reads"] == 0
        assert empty["session_id"] == "sess_s"

        mod.strata_read_scope_summary("g_arch")
        stats = mod.strata_session_stats()

    assert stats["reads"] == 1
    assert stats["contributions"] == 0
    assert stats["reads_by_scope"]["g_arch"]["count"] == 1


def test_accepted_contribution_increments_session_counter(tmp_path: Path) -> None:
    """An accepted contribution bumps the session's contributions counter (release valve)."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    fake_judgment = MagicMock()
    fake_judgment.decision = "accept_as_context"
    fake_judgment.reasoning = "ok"
    fake_judgment.new_summary = _make_summary("g_arch", "updated")

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend", session_id="sess_c")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=fake_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        mod.strata_contribute(
            scope_id="g_arch",
            content="Use structured logging.",
            proposed_classification="context",
            subject=None,
            supersedes=None,
        )

    state = mod._session_store.read("sess_c")
    assert state is not None
    assert state.contributions == 1


def test_declined_contribution_does_not_increment_counter(tmp_path: Path) -> None:
    """A scope-manager decline is not an accepted contribution — no counter bump."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    fake_judgment = MagicMock()
    fake_judgment.decision = "decline"
    fake_judgment.reasoning = "not memory-worthy"
    fake_judgment.new_summary = None

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend", session_id="sess_d")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=fake_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        mod.strata_contribute(
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


def _seed_and_read(mod, fleet, *, scope: str, session_id: str, times: int) -> list[dict]:
    """Read g_arch's summary *times* times as *scope*/*session_id*; return the results.

    Every read increments the session's reads counter, so this walks the session
    up to (and past) the nudge threshold deterministically.
    """
    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope=scope, session_id=session_id)
    results: list[dict] = []
    with scope_p, skill_p, session_p, patch.object(mod, "_load_fleet", return_value=fleet):
        for _ in range(times):
            results.append(mod.strata_read_scope_summary("g_arch"))
    return results


def test_closeout_records_decline_without_building_judge(tmp_path: Path) -> None:
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
        result = mod.strata_session_closeout(reason="read-only investigation, nothing decided")

    assert result["session_id"] == "sess_co"
    assert result["declines"] == 1
    assert result["contributions"] == 0

    state = mod._session_store.read("sess_co")
    assert state is not None
    assert state.declines == 1


def test_no_nudge_below_threshold(tmp_path: Path) -> None:
    """Reads below the threshold carry no nudge — the early-read silence (#111)."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    SummaryStore(summaries_dir).write("g_arch", _make_summary("g_arch", "ctx"))
    fleet = FleetConfig.load(fleet_path)

    results = _seed_and_read(mod, fleet, scope="g_backend", session_id="sess_nb", times=2)

    assert all("nudge" not in r for r in results)


def test_nudge_appears_at_threshold_with_current_counts(tmp_path: Path) -> None:
    """At the threshold the nudge fires and names the CURRENT read count."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    SummaryStore(summaries_dir).write("g_arch", _make_summary("g_arch", "ctx"))
    fleet = FleetConfig.load(fleet_path)

    results = _seed_and_read(mod, fleet, scope="g_backend", session_id="sess_th", times=3)

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


def test_nudge_escalates_at_higher_threshold(tmp_path: Path) -> None:
    """Once reads reach the escalation threshold the wording sharpens."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    SummaryStore(summaries_dir).write("g_arch", _make_summary("g_arch", "ctx"))
    fleet = FleetConfig.load(fleet_path)

    results = _seed_and_read(mod, fleet, scope="g_backend", session_id="sess_esc", times=6)

    base_nudge = results[2]["nudge"]  # reads == 3, base tier
    escalated = results[5]["nudge"]  # reads == 6, escalated tier

    assert "6" in escalated
    assert "stale" in escalated  # escalation marker, absent from the base tier
    assert escalated != base_nudge


def test_nudge_silent_after_contribution(tmp_path: Path) -> None:
    """An accepted contribution resets the asymmetry and quiets the nudge."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    SummaryStore(summaries_dir).write("g_arch", _make_summary("g_arch", "ctx"))
    fleet = FleetConfig.load(fleet_path)

    # Read to the threshold: the last read carries a nudge.
    pre = _seed_and_read(mod, fleet, scope="g_backend", session_id="sess_ac", times=3)
    assert "nudge" in pre[-1]

    fake_judgment = MagicMock()
    fake_judgment.decision = "accept_as_context"
    fake_judgment.reasoning = "ok"
    fake_judgment.new_summary = _make_summary("g_arch", "updated")

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend", session_id="sess_ac")
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=fake_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        mod.strata_contribute(
            scope_id="g_arch",
            content="Structured logging is the standard.",
            proposed_classification="context",
        )
        after = mod.strata_read_scope_summary("g_arch")

    assert "nudge" not in after


def test_nudge_silent_after_closeout(tmp_path: Path) -> None:
    """A mechanical closeout resets the asymmetry and quiets the nudge."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    SummaryStore(summaries_dir).write("g_arch", _make_summary("g_arch", "ctx"))
    fleet = FleetConfig.load(fleet_path)

    # Read to the threshold: the last read carries a nudge.
    pre = _seed_and_read(mod, fleet, scope="g_backend", session_id="sess_cn", times=3)
    assert "nudge" in pre[-1]

    scope_p, skill_p, session_p = _patch_agent_binding(mod, scope="g_backend", session_id="sess_cn")
    with scope_p, skill_p, session_p, patch.object(mod, "_load_fleet", return_value=fleet):
        closeout = mod.strata_session_closeout(reason="nothing to record")
        after = mod.strata_read_scope_summary("g_arch")

    assert closeout["declines"] == 1
    assert "nudge" not in after


def test_nudge_rides_perspective_and_record_reads(tmp_path: Path) -> None:
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
        persp = [mod.strata_read_perspective("g_backend") for _ in range(3)]
        record = mod.strata_read_scope_record("g_backend")

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
