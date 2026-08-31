"""Tests for src/strata/perspective.py — the compose_perspective library primitive.

Issue #83, primitive A / plan item S2.1: composition/ordering/precedence used
to live only inside strata.mcp.server.strata_read_perspective. This module
now owns that logic; the MCP tool delegates to it.

ADR 0013 (publication as the only sharing channel) rewrites the composition
rule this module implements:

- D1: chain edges carry only the ancestor's DIRECTIVES, full fidelity, every
  ancestor, root-first. An ancestor's context never composes for a
  descendant.
- D2/D3: publication is the only channel for non-binding knowledge, and it
  travels exactly ONE edge — chain or reference. A perspective therefore
  carries: the scope's own summary; every ancestor's directives; the
  immediate parent's publication (one hop via the chain edge); and the
  publications of scopes the scope ITSELF references (one hop via a
  reference edge) — never a grandparent's publication, and never a
  publication reached only through an ancestor's own reference edge.
- D5/D7: operator memory composes as directives only — a stored legacy
  `context`-kind operator item stays on disk but stops composing.

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
5. ADR 0008 D2 operator layer composition, narrowed by ADR 0013 D5/D7: a
   legacy `context`-kind operator item stops composing while a
   `directive`-kind one still binds.
6. ADR 0013 D1/D2/D3: an ancestor's context never reaches a descendant;
   ancestor directives reach every descendant at any depth; a grandparent's
   publication does not appear while the parent's does; a scope's own
   reference publication still appears while an ancestor's reference does
   not; nothing on disk is rewritten by composing a perspective.

Vocabulary follows CONTEXT.md verbatim: scope, stratum, perspective, scope
summary, directive, context, chain edge, reference edge, peer reference.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.operator import OperatorItem
from strata.perspective import compose_perspective
from strata.publication import PublishedItem
from strata.record_store import RecordStore
from strata.summary_store import Directive, ScopeSummary, SummaryStore

# ---------------------------------------------------------------------------
# Fixture fleet
#
# Topology: g_exec (L0) <- g_func (L1) <- g_team (L2) — a 3-scope,
# 3-stratum chain.
#
# Reference edges (ADR 0013 D3 — a scope's OWN reference edges only compose
# for it; an ancestor's reference edges do not):
#   g_exec -> g_exec_peer   (the ROOT's own reference — never reaches g_team)
#   g_func -> g_peer_a      (g_team's PARENT's own reference — never reaches
#                            g_team either; it would reach g_func's own
#                            perspective, one hop from g_func itself)
#   g_func -> g_peer_b      (second reference from g_func, deliberately given
#                            NO summary/publication — exercises the honestly
#                            empty face)
#   g_team -> g_team_peer   (g_team's OWN reference — this one composes)
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
            {"id": "g_team_peer", "name": "Team Peer", "stratum_id": "L2"},
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
            # Reference edges — each scope's OWN, per the topology comment above.
            {"from": "g_exec", "to": "g_exec_peer"},
            {"from": "g_func", "to": "g_peer_a"},
            {"from": "g_func", "to": "g_peer_b"},
            {"from": "g_team", "to": "g_team_peer"},
        ],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return fleet_path


def _make_summary(
    scope_id: str, context: str, directives: list[Directive] | None = None
) -> ScopeSummary:
    return ScopeSummary(
        scope_id=scope_id,
        directives=directives or [],
        context=context,
        updated_at="2026-07-12T00:00:00+00:00",
    )


def _make_directive(item_id: str, content: str) -> Directive:
    return Directive(
        id=item_id,
        content=content,
        subject=None,
        source_scope_id="operator",
        source_skill="operator",
        created_at="2026-07-12T00:00:00+00:00",
    )


def _seed_summaries(summaries_dir: str) -> SummaryStore:
    """Write real summary files for every fixture scope except g_peer_b.

    g_peer_b is deliberately left without a file so its layer exercises the
    synthesized-empty-summary/empty-publication fallback.
    """
    store = SummaryStore(summaries_dir)
    store.write("g_exec", _make_summary("g_exec", "executive context — must never leak"))
    store.write("g_exec_peer", _make_summary("g_exec_peer", "executive peer context"))
    store.write("g_func", _make_summary("g_func", "function context — must never leak"))
    store.write("g_team", _make_summary("g_team", "team context"))
    store.write("g_team_peer", _make_summary("g_team_peer", "team peer context"))
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


def _make_publication_reader(publications: dict[str, list[PublishedItem]]):
    """Build a publication_reader callable from a plain {scope_id: [items]} dict."""

    def _reader(scope_id: str) -> list[PublishedItem]:
        return publications.get(scope_id, [])

    return _reader


def _make_operator_reader(memory: dict[str, list[OperatorItem]]):
    """Build an operator_reader callable from a plain {scope_id: [items]} dict."""

    def _reader(scope_id: str) -> list[OperatorItem]:
        return memory.get(scope_id, [])

    return _reader


# ---------------------------------------------------------------------------
# Test 1: golden equivalence — MCP tool path vs. direct compose_perspective
# ---------------------------------------------------------------------------


async def test_golden_equivalence_mcp_tool_matches_compose_perspective(tmp_path: Path) -> None:
    """strata_read_perspective's dict equals compose_perspective's, pinned literally."""
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)

    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    from strata.publication import read_publication

    def _publication_reader(scope_id: str) -> list:
        return read_publication(scope_id, summaries_dir=summaries_dir)

    # g_peer_b has no summary file, so its layer's publication is honestly
    # empty regardless of timestamps — no synthesis/freezing needed for it
    # any more (ADR 0013: a reference layer never carries a summary).
    direct_result = compose_perspective(
        "g_team", fleet=fleet, summary_store=store, publication_reader=_publication_reader
    )

    # Through the MCP tool (entitlement checks + delegation).
    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
    ):
        mod._summary_store = store
        tool_result = await mod.strata_read_perspective("g_team")

    assert tool_result == direct_result

    # Pin the exact expected structure: scope ids, relations, binding, order.
    # Ancestors (directives only) root-first, self, the parent's publication
    # (one hop via the chain edge), then g_team's OWN reference (g_team_peer)
    # — g_exec's and g_func's own references (g_exec_peer, g_peer_a,
    # g_peer_b) never reach g_team; they are not g_team's own edges.
    expected_scope_order = [
        ("g_exec", "ancestor", True),
        ("g_func", "ancestor", True),
        ("g_team", "self", True),
        ("g_func", "parent_publication", False),
        ("g_team_peer", "peer_reference", False),
    ]
    actual = [
        (layer["scope_id"], layer["relation"], layer["binding"])
        for layer in direct_result["layers"]
    ]
    assert actual == expected_scope_order
    assert direct_result["scope_id"] == "g_team"
    assert direct_result["_layers_count"] == 5

    # Never-referenced-by-g_team scopes must never appear.
    layer_scope_ids = {layer["scope_id"] for layer in direct_result["layers"]}
    for absent in ("g_sibling", "g_note_a", "g_note_b", "g_exec_peer", "g_peer_a", "g_peer_b"):
        assert absent not in layer_scope_ids

    # Spot-check layer payloads. Self layer still carries a full "summary";
    # ancestor layers carry "directives" only, never "context" or "summary".
    team_layer = next(layer for layer in direct_result["layers"] if layer["relation"] == "self")
    assert team_layer["summary"]["context"] == "team context"
    exec_layer = next(
        layer
        for layer in direct_result["layers"]
        if layer["scope_id"] == "g_exec" and layer["relation"] == "ancestor"
    )
    assert "context" not in exec_layer
    assert "summary" not in exec_layer
    assert exec_layer["directives"] == []

    parent_pub_layer = next(
        layer for layer in direct_result["layers"] if layer["relation"] == "parent_publication"
    )
    assert "summary" not in parent_pub_layer
    assert parent_pub_layer["publication"] == {"items": []}

    peer_layer = next(
        layer for layer in direct_result["layers"] if layer["relation"] == "peer_reference"
    )
    assert peer_layer["scope_id"] == "g_team_peer"
    assert peer_layer["publication"] == {"items": []}


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

    default_result = compose_perspective("g_team", fleet=fleet, summary_store=store)
    explicit_empty_result = compose_perspective(
        "g_team", fleet=fleet, summary_store=store, extra_context_scopes=()
    )

    assert default_result == explicit_empty_result
    # With no publication_reader, no publication layers compose at all
    # (ADR 0013 D7 — no legacy full-summary fallback either).
    assert default_result["_layers_count"] == 3


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
# Test 5: operator_reader — ADR 0008 D2 operator layer composition, narrowed
# by ADR 0013 D5/D7 to directives only.
# ---------------------------------------------------------------------------


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
    team_directive = OperatorItem(
        id="op_team1",
        kind="directive",
        content="Team directive.",
        subject="note",
        created_at="2026-07-12T01:00:00+00:00",
    )
    reader = _make_operator_reader({"g_exec": [exec_directive], "g_team": [team_directive]})

    result = compose_perspective("g_team", fleet=fleet, summary_store=store, operator_reader=reader)

    ordering = [(layer["scope_id"], layer["relation"]) for layer in result["layers"]]
    exec_idx = ordering.index(("g_exec", "ancestor"))
    exec_operator_idx = ordering.index(("g_exec", "operator"))
    team_idx = ordering.index(("g_team", "self"))
    team_operator_idx = ordering.index(("g_team", "operator"))

    # Each operator layer immediately precedes its attachment scope's own layer.
    assert exec_operator_idx == exec_idx - 1
    assert team_operator_idx == team_idx - 1


def test_operator_layer_shape_is_directives_only(tmp_path: Path) -> None:
    """The operator layer carries a directives list and no 'context' key at all (ADR 0013 D5)."""
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
    reader = _make_operator_reader({"g_team": [directive]})

    result = compose_perspective("g_team", fleet=fleet, summary_store=store, operator_reader=reader)
    operator_layer = next(layer for layer in result["layers"] if layer["relation"] == "operator")

    assert operator_layer["scope_id"] == "g_team"
    assert operator_layer["stratum_id"] == "operator"
    assert operator_layer["binding"] is True
    assert "summary" not in operator_layer
    assert operator_layer["operator_memory"] == {
        "directives": [
            {
                "id": "op_d1",
                "content": "Bind this.",
                "subject": "binding-subj",
                "created_at": "2026-07-12T00:00:00+00:00",
            }
        ]
    }
    assert "context" not in operator_layer["operator_memory"]


def test_legacy_operator_context_item_stops_composing_directive_still_binds(
    tmp_path: Path,
) -> None:
    """ADR 0013 D5/D7: a stored legacy context-kind operator item never composes;
    a directive-kind item at the same attachment scope still binds."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    legacy_context = OperatorItem(
        id="op_c1",
        kind="context",
        content="Old operator observation — pre-ADR-0013 write.",
        subject=None,
        created_at="2026-07-12T00:00:00+00:00",
    )
    directive = OperatorItem(
        id="op_d1",
        kind="directive",
        content="Still binds.",
        subject=None,
        created_at="2026-07-12T00:00:01+00:00",
    )
    reader = _make_operator_reader({"g_team": [legacy_context, directive]})

    result = compose_perspective("g_team", fleet=fleet, summary_store=store, operator_reader=reader)
    operator_layer = next(layer for layer in result["layers"] if layer["relation"] == "operator")

    directive_ids = {d["id"] for d in operator_layer["operator_memory"]["directives"]}
    assert directive_ids == {"op_d1"}
    assert "Old operator observation" not in str(result)


def test_scope_with_only_legacy_context_operator_item_gets_no_layer(tmp_path: Path) -> None:
    """A chain scope whose only operator memory is a legacy context item composes no layer."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    legacy_context = OperatorItem(
        id="op_c1",
        kind="context",
        content="Old operator observation.",
        subject=None,
        created_at="2026-07-12T00:00:00+00:00",
    )
    reader = _make_operator_reader({"g_team": [legacy_context]})

    result = compose_perspective("g_team", fleet=fleet, summary_store=store, operator_reader=reader)
    operator_layers = [layer for layer in result["layers"] if layer["relation"] == "operator"]
    assert operator_layers == []


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

    reader = _make_operator_reader({})

    result = compose_perspective("g_team", fleet=fleet, summary_store=store, operator_reader=reader)
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
    reader = _make_operator_reader({"g_team_peer": [peer_item], "g_note_a": [note_item]})

    result = compose_perspective(
        "g_team",
        fleet=fleet,
        summary_store=store,
        extra_context_scopes=["g_note_a"],
        publication_reader=_make_publication_reader({}),
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

    default_result = compose_perspective("g_team", fleet=fleet, summary_store=store)
    explicit_none_result = compose_perspective(
        "g_team", fleet=fleet, summary_store=store, operator_reader=None
    )
    assert default_result == explicit_none_result
    assert all(layer["relation"] != "operator" for layer in default_result["layers"])


# ---------------------------------------------------------------------------
# ADR 0013 D1 — chain edges carry directives only, full walk, never context.
# ---------------------------------------------------------------------------


def test_ancestor_context_never_reaches_descendant(tmp_path: Path) -> None:
    """An ancestor's context (root or immediate parent) never appears anywhere
    in a descendant's composed perspective."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    result = compose_perspective("g_team", fleet=fleet, summary_store=store)

    for layer in result["layers"]:
        if layer["relation"] in ("ancestor",):
            assert "context" not in layer
            assert "summary" not in layer

    # Belt and suspenders: the literal context strings never appear anywhere
    # in the composed structure.
    serialized = str(result)
    assert "executive context — must never leak" not in serialized
    assert "function context — must never leak" not in serialized


def test_ancestor_directives_reach_descendant_at_any_depth(tmp_path: Path) -> None:
    """A directive at the ROOT (two strata up) reaches a grandchild's perspective."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)

    root_directive = _make_directive("d_root1", "Root directive — binds everyone.")
    store.write("g_exec", _make_summary("g_exec", "executive context", [root_directive]))

    fleet = FleetConfig.load(fleet_path)

    result = compose_perspective("g_team", fleet=fleet, summary_store=store)

    exec_layer = next(
        layer
        for layer in result["layers"]
        if layer["scope_id"] == "g_exec" and layer["relation"] == "ancestor"
    )
    assert exec_layer["binding"] is True
    directive_ids = {d["id"] for d in exec_layer["directives"]}
    assert "d_root1" in directive_ids
    assert exec_layer["directives"][0]["content"] == "Root directive — binds everyone."


def test_grandparent_publication_absent_parent_publication_present(tmp_path: Path) -> None:
    """A grandchild receives its parent's publication, never the root's (ADR 0013 D3)."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    root_item = PublishedItem(
        id="pub_root1",
        kind="context",
        content="Root's outward face — must not reach the grandchild.",
        subject=None,
        anchors=[],
        published_at="2026-07-12T00:00:00+00:00",
    )
    parent_item = PublishedItem(
        id="pub_parent1",
        kind="context",
        content="Parent's outward face — reaches the child.",
        subject=None,
        anchors=[],
        published_at="2026-07-12T00:00:01+00:00",
    )
    reader = _make_publication_reader({"g_exec": [root_item], "g_func": [parent_item]})

    result = compose_perspective(
        "g_team", fleet=fleet, summary_store=store, publication_reader=reader
    )

    serialized = str(result)
    assert "Root's outward face" not in serialized
    assert "Parent's outward face" in serialized

    parent_pub_layer = next(
        layer for layer in result["layers"] if layer["relation"] == "parent_publication"
    )
    assert parent_pub_layer["scope_id"] == "g_func"
    assert parent_pub_layer["publication"]["items"][0]["id"] == "pub_parent1"

    # And no layer at all carries the root's publication.
    assert not any(
        layer.get("scope_id") == "g_exec" and "publication" in layer for layer in result["layers"]
    )


def test_own_reference_publication_present_ancestor_reference_absent(tmp_path: Path) -> None:
    """A scope's own reference's publication composes; an ancestor's reference's does not."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    own_ref_item = PublishedItem(
        id="pub_teampeer1",
        kind="context",
        content="Team peer's outward face — g_team's own reference.",
        subject=None,
        anchors=[],
        published_at="2026-07-12T00:00:00+00:00",
    )
    ancestor_ref_item = PublishedItem(
        id="pub_peera1",
        kind="context",
        content="Peer A's outward face — only g_func's own reference, not g_team's.",
        subject=None,
        anchors=[],
        published_at="2026-07-12T00:00:01+00:00",
    )
    reader = _make_publication_reader(
        {"g_team_peer": [own_ref_item], "g_peer_a": [ancestor_ref_item]}
    )

    result = compose_perspective(
        "g_team", fleet=fleet, summary_store=store, publication_reader=reader
    )

    layer_scope_ids = {layer["scope_id"] for layer in result["layers"]}
    assert "g_team_peer" in layer_scope_ids
    assert "g_peer_a" not in layer_scope_ids

    serialized = str(result)
    assert "Team peer's outward face" in serialized
    assert "Peer A's outward face" not in serialized

    # g_func's OWN perspective, though, does receive g_peer_a's publication —
    # one hop from g_func itself.
    func_result = compose_perspective(
        "g_func", fleet=fleet, summary_store=store, publication_reader=reader
    )
    func_layer_ids = {layer["scope_id"] for layer in func_result["layers"]}
    assert "g_peer_a" in func_layer_ids


def test_composing_a_perspective_never_rewrites_disk(tmp_path: Path) -> None:
    """Reading a perspective is read-only: on-disk files are byte-identical before and after."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    from strata.operator import operator_publish
    from strata.publication import PublishedItem, _write_publication
    from strata.record_store import RecordStore

    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    record_store = RecordStore(db_path)

    operator_publish(
        "g_exec",
        "Some directive.",
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    _write_publication(
        "g_func",
        [
            PublishedItem(
                id="pub_func1",
                kind="context",
                content="Function's own publication.",
                subject=None,
                anchors=[],
                published_at="2026-07-12T00:00:00+00:00",
            )
        ],
        summaries_dir=summaries_dir,
    )

    def _operator_reader(scope_id: str) -> list:
        from strata.operator import read_operator_layer

        return read_operator_layer(scope_id, summaries_dir=summaries_dir)

    def _publication_reader(scope_id: str) -> list:
        from strata.publication import read_publication

        return read_publication(scope_id, summaries_dir=summaries_dir)

    summaries_snapshot = {p: p.read_bytes() for p in Path(summaries_dir).rglob("*") if p.is_file()}
    assert summaries_snapshot, "fixture should have written at least one file to disk"

    compose_perspective(
        "g_team",
        fleet=fleet,
        summary_store=store,
        operator_reader=_operator_reader,
        publication_reader=_publication_reader,
    )

    after = {p: p.read_bytes() for p in Path(summaries_dir).rglob("*") if p.is_file()}
    assert after == summaries_snapshot

    record_store.close()


# ---------------------------------------------------------------------------
# ADR 0007 D4 — publication_reader: peer/parent layers carry publications,
# never internal summaries. extra_context_scopes layers are unaffected.
# ADR 0013 D7: with no publication_reader, publication layers are simply
# omitted — there is no legacy full-summary fallback path.
# ---------------------------------------------------------------------------


def test_publication_reader_peer_layers_carry_publication_payload(tmp_path: Path) -> None:
    """With publication_reader given, peer layers carry items and no "summary" key."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    item = PublishedItem(
        id="pub_a1",
        kind="context",
        content="Team peer's outward face.",
        subject="status",
        anchors=["subject:status"],
        published_at="2026-07-12T00:00:00+00:00",
    )
    reader = _make_publication_reader({"g_team_peer": [item]})

    result = compose_perspective(
        "g_team", fleet=fleet, summary_store=store, publication_reader=reader
    )

    peer_layer = next(layer for layer in result["layers"] if layer["scope_id"] == "g_team_peer")
    assert peer_layer["relation"] == "peer_reference"
    assert peer_layer["binding"] is False
    assert "summary" not in peer_layer
    assert peer_layer["publication"] == {
        "items": [
            {
                "id": "pub_a1",
                "kind": "context",
                "content": "Team peer's outward face.",
                "subject": "status",
                "anchors": ["subject:status"],
                "published_at": "2026-07-12T00:00:00+00:00",
            }
        ]
    }


def test_publication_reader_none_default_composes_no_publication_layers(tmp_path: Path) -> None:
    """No publication_reader means no publication layers at all — never a full-summary fallback."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    result = compose_perspective("g_team", fleet=fleet, summary_store=store)

    assert all(
        layer["relation"] not in ("peer_reference", "parent_publication")
        for layer in result["layers"]
    )
    assert result["_layers_count"] == 3


def test_publication_reader_does_not_affect_extra_context_scopes(tmp_path: Path) -> None:
    """extra_context_scopes layers always carry a full summary, regardless of publication_reader."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fixture_fleet_yaml(tmp_path)
    store = _seed_summaries(summaries_dir)
    fleet = FleetConfig.load(fleet_path)

    reader = _make_publication_reader({})  # never returns anything — proves it's never consulted

    result = compose_perspective(
        "g_team",
        fleet=fleet,
        summary_store=store,
        extra_context_scopes=["g_note_a"],
        publication_reader=reader,
    )

    extra_layer = next(layer for layer in result["layers"] if layer["scope_id"] == "g_note_a")
    assert extra_layer["relation"] == "extra_context"
    assert "publication" not in extra_layer
    assert "summary" in extra_layer


# ---------------------------------------------------------------------------
# ADR 0010 — typed edges (issue #127, closing issue #123)
#
# Composition itself did not change here: cross-stratum reference layers
# arrive in the same referenced-scopes block, with the same
# publication-only, non-binding payload. These tests hold that line, and pin
# the headline #123 fix live — a fleet whose every edge is authored top-down
# now composes ancestor layers instead of nothing.
# ---------------------------------------------------------------------------


def _make_typed_fleet_yaml(tmp_path: Path, edges: list[dict], name: str) -> Path:
    """Write a 3-stratum fleet with *edges* verbatim and return its path."""
    fleet = {
        "strata": [
            {"id": "L0", "name": "executive", "ordinal": 0},
            {"id": "L1", "name": "function", "ordinal": 1},
            {"id": "L2", "name": "team", "ordinal": 2},
        ],
        "scopes": [
            {"id": "g_exec", "name": "Executive", "stratum_id": "L0"},
            {"id": "g_funcA", "name": "Function A", "stratum_id": "L1"},
            {"id": "g_funcB", "name": "Function B", "stratum_id": "L1"},
            {"id": "g_teamX", "name": "Team X", "stratum_id": "L2"},
            {"id": "g_teamY", "name": "Team Y", "stratum_id": "L2"},
        ],
        "edges": edges,
    }
    fleet_path = tmp_path / name
    fleet_path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return fleet_path


def test_inverted_authored_fleet_composes_ancestor_layers(tmp_path: Path) -> None:
    """The issue #123 shape composes: every edge authored top-down, ancestors still arrive.

    Before ADR 0010 each of these edges derived nothing at all — no ancestry,
    no reference, no layer — so every scope's perspective was silently
    self-only and a directive published at g_exec never reached anyone.
    """
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_typed_fleet_yaml(
        tmp_path,
        [
            {"from": "g_exec", "to": "g_funcA"},
            {"from": "g_funcA", "to": "g_teamX"},
        ],
        "inverted-fleet.yaml",
    )
    store = SummaryStore(summaries_dir)
    store.write("g_exec", _make_summary("g_exec", "executive context"))
    store.write("g_funcA", _make_summary("g_funcA", "function A context"))
    store.write("g_teamX", _make_summary("g_teamX", "team X context"))
    fleet = FleetConfig.load(fleet_path)

    result = compose_perspective("g_teamX", fleet=fleet, summary_store=store)

    assert [
        (layer["scope_id"], layer["relation"], layer["binding"]) for layer in result["layers"]
    ] == [
        ("g_exec", "ancestor", True),
        ("g_funcA", "ancestor", True),
        ("g_teamX", "self", True),
    ]
    layers_by_id = {layer["scope_id"]: layer for layer in result["layers"]}
    assert layers_by_id["g_teamX"]["summary"]["context"] == "team X context"
    assert "context" not in layers_by_id["g_exec"]
    assert "context" not in layers_by_id["g_funcA"]

    # The middle scope inherits its own ancestor from the same inverted edge.
    mid = compose_perspective("g_funcA", fleet=fleet, summary_store=store)
    assert [(layer["scope_id"], layer["relation"]) for layer in mid["layers"]] == [
        ("g_exec", "ancestor"),
        ("g_funcA", "self"),
    ]


def test_cross_stratum_reference_composes_publication_only(tmp_path: Path) -> None:
    """The uncle case composes into the referenced-scopes block, non-binding, no summary."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_typed_fleet_yaml(
        tmp_path,
        [
            {"from": "g_funcA", "to": "g_exec"},
            {"from": "g_teamX", "to": "g_funcA"},
            # The uncle: two strata up, not on g_teamX's chain, but IS g_teamX's own reference.
            {"from": "g_teamX", "to": "g_funcB", "kind": "reference"},
        ],
        "uncle-fleet.yaml",
    )
    store = SummaryStore(summaries_dir)
    for scope_id in ("g_exec", "g_funcA", "g_funcB", "g_teamX"):
        store.write(scope_id, _make_summary(scope_id, f"{scope_id} internal context"))
    fleet = FleetConfig.load(fleet_path)

    item = PublishedItem(
        id="pub_b1",
        kind="context",
        content="Function B's outward face.",
        subject="interfaces",
        anchors=["subject:interfaces"],
        published_at="2026-07-30T00:00:00+00:00",
    )
    result = compose_perspective(
        "g_teamX",
        fleet=fleet,
        summary_store=store,
        publication_reader=_make_publication_reader({"g_funcB": [item]}),
    )

    assert [
        (layer["scope_id"], layer["relation"], layer["binding"]) for layer in result["layers"]
    ] == [
        ("g_exec", "ancestor", True),
        ("g_funcA", "ancestor", True),
        ("g_teamX", "self", True),
        ("g_funcA", "parent_publication", False),
        ("g_funcB", "peer_reference", False),
    ]

    uncle_layer = result["layers"][-1]
    # Provenance carries the referenced scope's own stratum, not the reader's.
    assert uncle_layer["stratum_id"] == "L1"
    assert "summary" not in uncle_layer
    assert uncle_layer["publication"]["items"][0]["content"] == "Function B's outward face."
    assert "g_funcB internal context" not in str(result)


def test_chain_parent_and_several_references_compose_sorted_after_self(tmp_path: Path) -> None:
    """One chain parent's publication plus g_funcA's own same/cross-stratum references, sorted."""
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_typed_fleet_yaml(
        tmp_path,
        [
            {"from": "g_funcA", "to": "g_exec"},
            # Same-stratum (the peer reference), untyped — g_funcA's OWN.
            {"from": "g_funcA", "to": "g_funcB"},
            # Downward, to a scope on no chain of g_funcA's — g_funcA's OWN.
            {"from": "g_funcA", "to": "g_teamY", "kind": "reference"},
            {"from": "g_teamX", "to": "g_exec", "kind": "reference"},
        ],
        "fanout-fleet.yaml",
    )
    store = SummaryStore(summaries_dir)
    for scope_id in ("g_exec", "g_funcA", "g_funcB", "g_teamY"):
        store.write(scope_id, _make_summary(scope_id, f"{scope_id} internal context"))
    fleet = FleetConfig.load(fleet_path)

    result = compose_perspective(
        "g_funcA",
        fleet=fleet,
        summary_store=store,
        publication_reader=_make_publication_reader({}),
    )

    # g_funcA's own chain parent is g_exec, so g_exec's publication composes
    # as a parent_publication layer, then every one of g_funcA's OWN
    # references sorted by id.
    assert [
        (layer["scope_id"], layer["relation"], layer["binding"]) for layer in result["layers"]
    ] == [
        ("g_exec", "ancestor", True),
        ("g_funcA", "self", True),
        ("g_exec", "parent_publication", False),
        ("g_funcB", "peer_reference", False),
        ("g_teamY", "peer_reference", False),
    ]
    layers_by_id = {layer["scope_id"]: layer for layer in result["layers"]}
    assert layers_by_id["g_funcB"]["stratum_id"] == "L1"
    assert layers_by_id["g_teamY"]["stratum_id"] == "L2"
    for referenced in ("g_funcB", "g_teamY"):
        assert layers_by_id[referenced]["publication"] == {"items": []}
        assert "summary" not in layers_by_id[referenced]
    # g_teamX references g_exec, not g_funcA — direction is referencer→referenced.
    assert "g_teamX" not in layers_by_id


def test_demoted_legacy_edge_composes_as_a_reference_layer(tmp_path: Path) -> None:
    """A legacy fleet's formerly-inert edge composes as a reference layer (ADR 0010 D5).

    The fleet holds a correct parent edge and, onto the same child, an edge
    authored the other way — inert before ADR 0010, and a load error under
    per-edge inference. Per-child resolution keeps it loading: the correct
    edge is the chain, the other demotes to a reference, and the child reads
    that scope's publication instead of inheriting from it.
    """
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_typed_fleet_yaml(
        tmp_path,
        [
            {"from": "g_funcA", "to": "g_exec"},
            {"from": "g_teamX", "to": "g_funcA"},
            {"from": "g_funcB", "to": "g_teamX"},
        ],
        "legacy-fleet.yaml",
    )
    store = SummaryStore(summaries_dir)
    for scope_id in ("g_exec", "g_funcA", "g_funcB", "g_teamX"):
        store.write(scope_id, _make_summary(scope_id, f"{scope_id} internal context"))
    fleet = FleetConfig.load(fleet_path)

    item = PublishedItem(
        id="pub_b1",
        kind="context",
        content="Function B's outward face.",
        subject="status",
        anchors=["subject:status"],
        published_at="2026-07-30T00:00:00+00:00",
    )
    result = compose_perspective(
        "g_teamX",
        fleet=fleet,
        summary_store=store,
        publication_reader=_make_publication_reader({"g_funcB": [item]}),
    )

    assert [
        (layer["scope_id"], layer["relation"], layer["binding"]) for layer in result["layers"]
    ] == [
        ("g_exec", "ancestor", True),
        ("g_funcA", "ancestor", True),
        ("g_teamX", "self", True),
        ("g_funcA", "parent_publication", False),
        ("g_funcB", "peer_reference", False),
    ]
    demoted_layer = result["layers"][-1]
    assert "summary" not in demoted_layer
    assert demoted_layer["publication"]["items"][0]["content"] == "Function B's outward face."
    assert "g_funcB internal context" not in str(result)
