"""Tests for src/strata/perspective.py — the compose_perspective library primitive.

Issue #83, primitive A / plan item S2.1: composition/ordering/precedence used
to live only inside strata.mcp.server.strata_read_perspective. This module
now owns that logic; the MCP tool delegates to it.

Tests:
1. Golden equivalence: the dict strata_read_perspective returns through the
   MCP tool path is byte-identical to what compose_perspective returns when
   called directly against the same fleet/store — and matches a pinned,
   literal expected structure (scope ids, relations, binding flags, order).
2. Importability: strata.perspective imports standalone, without pulling in
   strata.mcp (ADR 0001's "not cleanly importable" complaint, resolved).
3. extra_context_scopes (additive, library-only): appended after peer
   layers, sorted by scope id, relation="extra_context", binding=False;
   an empty default changes nothing; an unknown scope id raises ValueError.
4. compose_perspective raises ValueError for an unknown scope_id target.

Vocabulary follows CONTEXT.md verbatim: scope, stratum, perspective, scope
summary, directive, context, intra-stratum edge (peer reference).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.operator import OperatorItem
from strata.perspective import compose_perspective
from strata.record_store import RecordStore
from strata.summary_store import ScopeSummary, SummaryStore

# ---------------------------------------------------------------------------
# Fixture fleet
#
# Topology: g_exec (L0) <- g_func (L1) <- g_team (L2) — a 3-scope,
# 3-stratum chain.
#
# Intra-stratum reference edges (context only):
#   g_exec -> g_exec_peer   (referenced by an ANCESTOR of g_team)
#   g_func -> g_peer_a      (referenced by g_team's own parent, has a summary)
#   g_func -> g_peer_b      (second reference from the same chain scope,
#                            deliberately given NO summary file — exercises
#                            the synthesized-empty-summary fallback)
#
# g_sibling (L1) has no reference edge at all — an unreferenced sibling of
# g_func that must never appear in g_team's perspective.
#
# Two more disconnected, active scopes (g_note_a, g_note_b) exist purely as
# extra_context_scopes candidates — neither is on g_team's chain nor
# referenced by it, so they only ever appear when a caller asks for them
# explicitly.
# ---------------------------------------------------------------------------


def _make_fixture_fleet_yaml(tmp_path: Path) -> Path:
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
            {"id": "g_sibling", "name": "Unreferenced Sibling", "stratum_id": "L1"},
            {"id": "g_note_a", "name": "Note A", "stratum_id": "L1"},
            {"id": "g_note_b", "name": "Note B", "stratum_id": "L1"},
        ],
        "edges": [
            # Inter-stratum: child -> parent
            {"from": "g_func", "to": "g_exec"},
            {"from": "g_team", "to": "g_func"},
            {"from": "g_sibling", "to": "g_exec"},
            {"from": "g_note_a", "to": "g_exec"},
            {"from": "g_note_b", "to": "g_exec"},
            # Intra-stratum peer references (context only)
            {"from": "g_exec", "to": "g_exec_peer"},
            {"from": "g_func", "to": "g_peer_a"},
            {"from": "g_func", "to": "g_peer_b"},
        ],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return fleet_path


def _make_summary(scope_id: str, context: str) -> ScopeSummary:
    return ScopeSummary(
        scope_id=scope_id,
        directives=[],
        context=context,
        updated_at="2026-07-12T00:00:00+00:00",
    )


def _seed_summaries(summaries_dir: str) -> SummaryStore:
    """Write real summary files for every fixture scope except g_peer_b.

    g_peer_b is deliberately left without a file so its layer exercises the
    synthesized-empty-summary fallback (version=0, exists=False).
    """
    store = SummaryStore(summaries_dir)
    store.write("g_exec", _make_summary("g_exec", "executive context"))
    store.write("g_exec_peer", _make_summary("g_exec_peer", "executive peer context"))
    store.write("g_func", _make_summary("g_func", "function context"))
    store.write("g_team", _make_summary("g_team", "team context"))
    store.write("g_peer_a", _make_summary("g_peer_a", "peer a context"))
    store.write("g_sibling", _make_summary("g_sibling", "sibling context — must not appear"))
    store.write("g_note_a", _make_summary("g_note_a", "note a context"))
    store.write("g_note_b", _make_summary("g_note_b", "note b context"))
    return store


# ---------------------------------------------------------------------------
# MCP tool loader — mirrors tests/test_mcp_server.py's _load_mcp_module, kept
# local so this test file stands on its own.
# ---------------------------------------------------------------------------


def _load_mcp_module(db_path: str, summaries_dir: str, fleet_yaml_path: str):
    import importlib

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

    with (
        patch("strata.settings.get_settings", return_value=fake_settings),
        patch("strata.project_config.load_project_config", return_value=None),
    ):
        import strata.mcp.server as mod

        importlib.reload(mod)

    mod._settings = fake_settings
    mod._project_config = None
    mod._db_path = db_path
    mod._summaries_dir = summaries_dir
    mod._fleet_yaml_path = fleet_yaml_path
    mod._record_store = RecordStore(db_path)
    mod._summary_store = SummaryStore(summaries_dir)

    return mod


# ---------------------------------------------------------------------------
# Test 1: golden equivalence — MCP tool path vs. direct compose_perspective
# ---------------------------------------------------------------------------


def test_golden_equivalence_mcp_tool_matches_compose_perspective(tmp_path: Path) -> None:
    """strata_read_perspective's dict equals compose_perspective's, pinned literally."""
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)

    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    # g_peer_b has no summary file, so each compose_perspective call
    # synthesizes an empty one stamped with the current time (issue #59).
    # Freeze it so the two independent calls below (direct + via the MCP
    # tool) produce byte-identical synthesized timestamps, not just
    # byte-identical structure.
    fixed_now = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)

    # Direct library call.
    with patch("strata.perspective.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        direct_result = compose_perspective("g_team", fleet=fleet, summary_store=store)

    # Through the MCP tool (entitlement checks + delegation).
    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
        patch("strata.perspective.datetime") as mock_dt,
    ):
        mock_dt.now.return_value = fixed_now
        mod._summary_store = store
        tool_result = mod.strata_read_perspective("g_team")

    assert tool_result == direct_result

    # Pin the exact expected structure: scope ids, relations, binding, order.
    expected_scope_order = [
        ("g_exec", "ancestor", True),
        ("g_func", "ancestor", True),
        ("g_team", "self", True),
        ("g_exec_peer", "peer_reference", False),
        ("g_peer_a", "peer_reference", False),
        ("g_peer_b", "peer_reference", False),
    ]
    actual = [
        (layer["scope_id"], layer["relation"], layer["binding"])
        for layer in direct_result["layers"]
    ]
    assert actual == expected_scope_order
    assert direct_result["scope_id"] == "g_team"
    assert direct_result["_layers_count"] == 6

    # g_sibling — an unreferenced L1 scope — must never appear.
    layer_scope_ids = {layer["scope_id"] for layer in direct_result["layers"]}
    assert "g_sibling" not in layer_scope_ids
    assert "g_note_a" not in layer_scope_ids
    assert "g_note_b" not in layer_scope_ids

    # Spot-check summary content on a couple of layers.
    layers_by_id = {layer["scope_id"]: layer for layer in direct_result["layers"]}
    assert layers_by_id["g_team"]["summary"]["context"] == "team context"
    assert layers_by_id["g_peer_a"]["summary"]["context"] == "peer a context"

    # g_peer_b has no summary file on disk — synthesized empty summary.
    peer_b_summary = layers_by_id["g_peer_b"]["summary"]
    assert peer_b_summary["directives"] == []
    assert peer_b_summary["context"] == ""
    assert peer_b_summary["version"] == 0
    assert peer_b_summary["exists"] is False


# ---------------------------------------------------------------------------
# Test 2: importability — strata.perspective standalone, no strata.mcp
# ---------------------------------------------------------------------------


def test_perspective_module_imports_without_mcp() -> None:
    """strata.perspective must import cleanly without pulling in strata.mcp.

    This is the ADR 0001 complaint ("not cleanly importable") the extraction
    resolves — run in a subprocess so sys.modules from this test run (which
    may already have strata.mcp loaded by other tests) can't mask a real
    dependency.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import strata.perspective\n"
            "assert 'strata.mcp' not in sys.modules, "
            "'importing strata.perspective must not import strata.mcp'\n"
            "print('OK')\n",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# Test 3: extra_context_scopes
# ---------------------------------------------------------------------------


def test_extra_context_scopes_appended_after_peers_sorted(tmp_path: Path) -> None:
    """extra_context_scopes append after peer layers, sorted by scope id."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)

    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    result = compose_perspective(
        "g_team",
        fleet=fleet,
        summary_store=store,
        extra_context_scopes=["g_note_b", "g_note_a"],
    )

    assert result["_layers_count"] == 8
    tail = [
        (layer["scope_id"], layer["relation"], layer["binding"]) for layer in result["layers"][-2:]
    ]
    assert tail == [
        ("g_note_a", "extra_context", False),
        ("g_note_b", "extra_context", False),
    ]
    layers_by_id = {layer["scope_id"]: layer for layer in result["layers"]}
    assert layers_by_id["g_note_a"]["summary"]["context"] == "note a context"
    assert layers_by_id["g_note_b"]["summary"]["context"] == "note b context"


def test_extra_context_scopes_empty_default_changes_nothing(tmp_path: Path) -> None:
    """Omitting extra_context_scopes (the default) is identical to passing ()."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)

    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    # Same reasoning as the golden-equivalence test: g_peer_b's synthesized
    # summary timestamp must be frozen so the two independent calls compare
    # byte-identical, not just structurally identical.
    fixed_now = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
    with patch("strata.perspective.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        default_result = compose_perspective("g_team", fleet=fleet, summary_store=store)
        explicit_empty_result = compose_perspective(
            "g_team", fleet=fleet, summary_store=store, extra_context_scopes=()
        )

    assert default_result == explicit_empty_result
    assert default_result["_layers_count"] == 6


def test_extra_context_scopes_unknown_id_raises(tmp_path: Path) -> None:
    """An extra_context_scopes entry outside the fleet raises ValueError."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)

    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    with pytest.raises(ValueError, match="g_does_not_exist"):
        compose_perspective(
            "g_team",
            fleet=fleet,
            summary_store=store,
            extra_context_scopes=["g_note_a", "g_does_not_exist"],
        )


# ---------------------------------------------------------------------------
# Test 4: unknown scope_id target raises ValueError
# ---------------------------------------------------------------------------


def test_compose_perspective_unknown_scope_id_raises(tmp_path: Path) -> None:
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)

    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    with pytest.raises(ValueError, match="g_does_not_exist"):
        compose_perspective("g_does_not_exist", fleet=fleet, summary_store=store)


# ---------------------------------------------------------------------------
# Test 5: operator_reader — ADR 0008 D2 operator layer composition
# ---------------------------------------------------------------------------


def _make_operator_reader(memory: dict[str, list[OperatorItem]]):
    """Build an operator_reader callable from a plain {scope_id: [items]} dict."""

    def _reader(scope_id: str) -> list[OperatorItem]:
        return memory.get(scope_id, [])

    return _reader


def test_operator_layer_inserted_immediately_above_attachment_scope(tmp_path: Path) -> None:
    """An operator layer for a chain scope sits directly above that scope's own layer."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    exec_directive = OperatorItem(
        id="op_exec1",
        kind="directive",
        content="Executive directive.",
        subject=None,
        created_at="2026-07-12T00:00:00+00:00",
    )
    team_context = OperatorItem(
        id="op_team1",
        kind="context",
        content="Team observation.",
        subject="note",
        created_at="2026-07-12T01:00:00+00:00",
    )
    reader = _make_operator_reader({"g_exec": [exec_directive], "g_team": [team_context]})

    result = compose_perspective("g_team", fleet=fleet, summary_store=store, operator_reader=reader)

    ordering = [(layer["scope_id"], layer["relation"]) for layer in result["layers"]]
    exec_idx = ordering.index(("g_exec", "ancestor"))
    exec_operator_idx = ordering.index(("g_exec", "operator"))
    team_idx = ordering.index(("g_team", "self"))
    team_operator_idx = ordering.index(("g_team", "operator"))

    # Each operator layer immediately precedes its attachment scope's own layer.
    assert exec_operator_idx == exec_idx - 1
    assert team_operator_idx == team_idx - 1


def test_operator_layer_shape_and_labels(tmp_path: Path) -> None:
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    directive = OperatorItem(
        id="op_d1",
        kind="directive",
        content="Bind this.",
        subject="binding-subj",
        created_at="2026-07-12T00:00:00+00:00",
    )
    context = OperatorItem(
        id="op_c1",
        kind="context",
        content="Inform this.",
        subject=None,
        created_at="2026-07-12T00:00:01+00:00",
    )
    reader = _make_operator_reader({"g_team": [directive, context]})

    result = compose_perspective("g_team", fleet=fleet, summary_store=store, operator_reader=reader)
    operator_layer = next(layer for layer in result["layers"] if layer["relation"] == "operator")

    assert operator_layer["scope_id"] == "g_team"
    assert operator_layer["stratum_id"] == "operator"
    assert operator_layer["binding"] is True
    assert "summary" not in operator_layer
    assert operator_layer["operator_memory"]["directives"] == [
        {
            "id": "op_d1",
            "content": "Bind this.",
            "subject": "binding-subj",
            "created_at": "2026-07-12T00:00:00+00:00",
        }
    ]
    assert operator_layer["operator_memory"]["context"] == [
        {
            "id": "op_c1",
            "content": "Inform this.",
            "subject": None,
            "created_at": "2026-07-12T00:00:01+00:00",
        }
    ]


def test_operator_layer_verbatim_content_preserved(tmp_path: Path) -> None:
    """Item content composes byte-identical — no rewriting, no truncation."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    tricky = "Line one.\nLine two with **markdown** and a trailing colon:\nLine three."
    item = OperatorItem(
        id="op_v1",
        kind="directive",
        content=tricky,
        subject=None,
        created_at="2026-07-12T00:00:00+00:00",
    )
    reader = _make_operator_reader({"g_team": [item]})

    result = compose_perspective("g_team", fleet=fleet, summary_store=store, operator_reader=reader)
    operator_layer = next(layer for layer in result["layers"] if layer["relation"] == "operator")
    assert operator_layer["operator_memory"]["directives"][0]["content"] == tricky


def test_scopes_without_operator_memory_get_no_layer(tmp_path: Path) -> None:
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    # Reader that returns operator memory for nothing at all.
    reader = _make_operator_reader({})

    # g_peer_b has no summary file, so its layer synthesizes a fresh
    # timestamp each call (issue #59) — freeze it so the two calls below
    # compare byte-identical, not just structurally identical.
    fixed_now = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
    with patch("strata.perspective.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        result = compose_perspective(
            "g_team", fleet=fleet, summary_store=store, operator_reader=reader
        )
        # Layer count/order is identical to the no-operator-reader case.
        baseline = compose_perspective("g_team", fleet=fleet, summary_store=store)

    operator_layers = [layer for layer in result["layers"] if layer["relation"] == "operator"]
    assert operator_layers == []
    assert result == baseline


def test_peer_and_extra_context_layers_never_get_operator_layers(tmp_path: Path) -> None:
    """Operator memory binds a chain; peer/extra layers are not this reader's chain."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    # Attach operator memory at a PEER scope (g_peer_a) and an extra-context
    # scope (g_note_a) — neither is on g_team's own chain.
    peer_item = OperatorItem(
        id="op_peer1",
        kind="directive",
        content="peer-attached",
        subject=None,
        created_at="2026-07-12T00:00:00+00:00",
    )
    note_item = OperatorItem(
        id="op_note1",
        kind="directive",
        content="note-attached",
        subject=None,
        created_at="2026-07-12T00:00:00+00:00",
    )
    reader = _make_operator_reader({"g_peer_a": [peer_item], "g_note_a": [note_item]})

    result = compose_perspective(
        "g_team",
        fleet=fleet,
        summary_store=store,
        extra_context_scopes=["g_note_a"],
        operator_reader=reader,
    )
    operator_layers = [layer for layer in result["layers"] if layer["relation"] == "operator"]
    assert operator_layers == []


def test_operator_reader_none_default_changes_nothing(tmp_path: Path) -> None:
    """Omitting operator_reader (the default None) composes zero operator layers."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    fixed_now = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
    with patch("strata.perspective.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        default_result = compose_perspective("g_team", fleet=fleet, summary_store=store)
        explicit_none_result = compose_perspective(
            "g_team", fleet=fleet, summary_store=store, operator_reader=None
        )
    assert default_result == explicit_none_result
    assert default_result["_layers_count"] == 6
    assert all(layer["relation"] != "operator" for layer in default_result["layers"])
