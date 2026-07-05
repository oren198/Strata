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
10. Peer (intra-stratum) edges are NOT traversed — peer scope absent from layers.
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
    g_peer is an intra-stratum peer of g_func (L1) — must not appear in
    the g_team perspective.
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
            # Intra-stratum peer reference (same L1 — must NOT be traversed)
            {"from": "g_func", "to": "g_peer"},
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
# Test 10: Peer (intra-stratum) edges are NOT traversed
# ---------------------------------------------------------------------------


def test_perspective_peer_edges_not_traversed(tmp_path: Path) -> None:
    """Inter-stratum-only invariant: peer (intra-stratum) scope must not appear in layers.

    The deep fleet has g_func (L1) with a peer edge to g_peer (L1).
    When reading g_team's perspective, g_peer must not appear in any layer.
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
        "Peer (intra-stratum) scope g_peer must not appear in the perspective layers"
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
# Issue #48 — entitlement-scoped reads
#
# Entitled read surface = bound scope (_AGENT_SCOPE) + its inter-stratum
# ancestors. Peer scopes are excluded — they reach an agent only through
# ratified content composed into its perspective (issue #41), never a direct
# read. Uses the deep fleet: g_exec (L0) <- g_func (L1) <- g_team (L2), with
# g_peer as an L1 peer of g_func (NOT an ancestor of g_team).
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

    assert record_result == {"contributions": [], "judgments": []}


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
    """Reading a peer (intra-stratum, non-ancestor) scope raises RuntimeError."""
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
        with pytest.raises(RuntimeError, match="entitled read surface") as exc_info:
            mod.strata_read_scope_summary("g_peer")

    message = str(exc_info.value)
    assert "g_peer" in message
    assert "g_team" in message
    assert "issue #41" in message

    # Same entitlement gate applies to perspective and record reads.
    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        with pytest.raises(RuntimeError, match="entitled read surface"):
            mod.strata_read_perspective("g_peer")
        with pytest.raises(RuntimeError, match="entitled read surface"):
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

    assert result == {"contributions": [], "judgments": []}


# ---------------------------------------------------------------------------
# Entitlement edge cases (release-review findings)
# ---------------------------------------------------------------------------


def test_descendant_read_is_denied(tmp_path: Path) -> None:
    """The entitled surface is self + ANCESTORS — descendants are not readable."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)
    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_arch"),  # the L0 parent
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError, match="entitled read surface"),
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
