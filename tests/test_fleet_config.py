"""Tests for src/strata/fleet_config.py.

Covers all 8 load-time invariants (each with a failing case and a passing
counterpart), the scope lifecycle (status defaulting and archived behaviour),
per-scope skill declaration fields, the mutation API, and ADR 0010's typed
edges (kind inference, chain canonicalization, the effective single-parent
rule, and reference edges at any stratum distance).

Vocabulary follows CONTEXT.md: stratum, scope, edge, chain edge, reference
edge, peer reference, fleet.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from strata.fleet_config import FleetConfig, FleetConfigError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, content: str, name: str = "fleet.yaml") -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


_VALID_YAML = """
strata:
  - id: L0
    name: Executive
    ordinal: 0
  - id: L1
    name: Function
    ordinal: 1
  - id: L2
    name: Team
    ordinal: 2

scopes:
  - id: g_ceo
    name: CEO
    stratum_id: L0
  - id: g_eng
    name: Engineering
    stratum_id: L1
  - id: g_arch
    name: Architect
    stratum_id: L1
  - id: g_backend
    name: Backend Dev
    stratum_id: L2

edges:
  - from: g_backend
    to: g_eng
  - from: g_arch
    to: g_eng
  - from: g_eng
    to: g_ceo
"""

# ---------------------------------------------------------------------------
# Invariant 1 — Duplicate stratum IDs
# ---------------------------------------------------------------------------


def test_invariant1_duplicate_stratum_id_rejected(tmp_path: Path) -> None:
    bad = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
          - id: L0
            name: Duplicate
            ordinal: 1
        scopes: []
        edges: []
    """
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(_write(tmp_path, bad))
    assert exc_info.value.kind == "duplicate_stratum_id"
    assert "L0" in exc_info.value.message


def test_invariant1_unique_stratum_ids_accepted(tmp_path: Path) -> None:
    FleetConfig.load(_write(tmp_path, _VALID_YAML))


# ---------------------------------------------------------------------------
# Invariant 2 — Duplicate scope IDs
# ---------------------------------------------------------------------------


def test_invariant2_duplicate_scope_id_rejected(tmp_path: Path) -> None:
    bad = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
          - id: g_ceo
            name: Duplicate CEO
            stratum_id: L0
        edges: []
    """
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(_write(tmp_path, bad))
    assert exc_info.value.kind == "duplicate_scope_id"
    assert "g_ceo" in exc_info.value.message


def test_invariant2_unique_scope_ids_accepted(tmp_path: Path) -> None:
    FleetConfig.load(_write(tmp_path, _VALID_YAML))


# ---------------------------------------------------------------------------
# Invariant 3 — Duplicate stratum ordinals
# ---------------------------------------------------------------------------


def test_invariant3_duplicate_stratum_ordinal_rejected(tmp_path: Path) -> None:
    bad = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
          - id: L1
            name: Function
            ordinal: 0
        scopes: []
        edges: []
    """
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(_write(tmp_path, bad))
    assert exc_info.value.kind == "duplicate_stratum_ordinal"
    assert "0" in exc_info.value.message


def test_invariant3_unique_ordinals_accepted(tmp_path: Path) -> None:
    FleetConfig.load(_write(tmp_path, _VALID_YAML))


# ---------------------------------------------------------------------------
# Invariant 4 — Scope stratum_id references a defined stratum
# ---------------------------------------------------------------------------


def test_invariant4_unknown_stratum_ref_rejected(tmp_path: Path) -> None:
    bad = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: LX
        edges: []
    """
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(_write(tmp_path, bad))
    assert exc_info.value.kind == "unknown_stratum_ref"
    assert "LX" in exc_info.value.message


def test_invariant4_valid_stratum_ref_accepted(tmp_path: Path) -> None:
    FleetConfig.load(_write(tmp_path, _VALID_YAML))


# ---------------------------------------------------------------------------
# Invariant 5 — Edge endpoints reference defined scopes
# ---------------------------------------------------------------------------


def test_invariant5_unknown_scope_ref_from_rejected(tmp_path: Path) -> None:
    bad = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
          - id: L1
            name: Function
            ordinal: 1
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
        edges:
          - from: g_ghost
            to: g_ceo
    """
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(_write(tmp_path, bad))
    assert exc_info.value.kind == "unknown_scope_ref"
    assert "g_ghost" in exc_info.value.message


def test_invariant5_unknown_scope_ref_to_rejected(tmp_path: Path) -> None:
    bad = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
          - id: L1
            name: Function
            ordinal: 1
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
        edges:
          - from: g_ceo
            to: g_nonexistent
    """
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(_write(tmp_path, bad))
    assert exc_info.value.kind == "unknown_scope_ref"
    assert "g_nonexistent" in exc_info.value.message


def test_invariant5_valid_edge_refs_accepted(tmp_path: Path) -> None:
    FleetConfig.load(_write(tmp_path, _VALID_YAML))


# ---------------------------------------------------------------------------
# Invariant 6 — No self-loops
# ---------------------------------------------------------------------------


def test_invariant6_self_loop_rejected(tmp_path: Path) -> None:
    bad = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
        edges:
          - from: g_ceo
            to: g_ceo
    """
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(_write(tmp_path, bad))
    assert exc_info.value.kind == "self_loop"
    assert "g_ceo" in exc_info.value.message


def test_invariant6_no_self_loops_accepted(tmp_path: Path) -> None:
    FleetConfig.load(_write(tmp_path, _VALID_YAML))


# ---------------------------------------------------------------------------
# Invariant 7 — ±1 stratum-distance constraint
# ---------------------------------------------------------------------------


def test_invariant7_stratum_distance_gt1_rejected(tmp_path: Path) -> None:
    bad = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
          - id: L1
            name: Function
            ordinal: 1
          - id: L2
            name: Team
            ordinal: 2
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
          - id: g_backend
            name: Backend
            stratum_id: L2
        edges:
          - from: g_backend
            to: g_ceo
    """
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(_write(tmp_path, bad))
    assert exc_info.value.kind == "stratum_distance_violation"


def test_invariant7_same_stratum_edge_accepted(tmp_path: Path) -> None:
    """An intra-stratum (peer) edge is valid (distance == 0)."""
    FleetConfig.load(_write(tmp_path, _VALID_YAML))


# ---------------------------------------------------------------------------
# Invariant 8 — default_skill must be in permitted_skills (skill drift)
# ---------------------------------------------------------------------------


def test_invariant8_skill_drift_rejected(tmp_path: Path) -> None:
    bad = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
            default_skill: code-writer
            permitted_skills: [evidence-summarizer]
        edges: []
    """
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(_write(tmp_path, bad))
    assert exc_info.value.kind == "skill_drift"
    assert "g_ceo" in exc_info.value.message
    assert "code-writer" in exc_info.value.message


def test_invariant8_default_in_permitted_accepted(tmp_path: Path) -> None:
    good = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
            default_skill: code-writer
            permitted_skills: [code-writer, evidence-summarizer]
        edges: []
    """
    config = FleetConfig.load(_write(tmp_path, good))
    assert config.scopes[0].default_skill == "code-writer"
    assert "code-writer" in config.scopes[0].permitted_skills


# ---------------------------------------------------------------------------
# Scope lifecycle — status field
# ---------------------------------------------------------------------------


def test_status_defaults_to_active(tmp_path: Path) -> None:
    """A scope without an explicit status field defaults to 'active'."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
        edges: []
    """
    config = FleetConfig.load(_write(tmp_path, yaml))
    assert config.scopes[0].status == "active"


def test_archived_scope_excluded_from_active_scopes(tmp_path: Path) -> None:
    """Archived scopes are excluded from active_scopes()."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_active
            name: Active
            stratum_id: L0
            status: active
          - id: g_archived
            name: Archived
            stratum_id: L0
            status: archived
        edges: []
    """
    config = FleetConfig.load(_write(tmp_path, yaml))
    active = config.active_scopes()
    assert len(active) == 1
    assert active[0].id == "g_active"


def test_auto_bind_scope_returns_sole_active_scope(tmp_path: Path) -> None:
    """A fleet with exactly one active scope auto-binds to it."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_root
            name: Root
            stratum_id: L0
        edges: []
    """
    config = FleetConfig.load(_write(tmp_path, yaml))
    scope = config.auto_bind_scope()
    assert scope is not None
    assert scope.id == "g_root"


def test_auto_bind_scope_returns_none_for_multi_scope_fleet(tmp_path: Path) -> None:
    """A fleet with 2+ active scopes has no auto-bind target."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_root
            name: Root
            stratum_id: L0
          - id: g_arch
            name: Arch
            stratum_id: L0
        edges: []
    """
    config = FleetConfig.load(_write(tmp_path, yaml))
    assert config.auto_bind_scope() is None


def test_auto_bind_scope_ignores_archived_scopes(tmp_path: Path) -> None:
    """One active + one archived scope still auto-binds to the active one."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_active
            name: Active
            stratum_id: L0
            status: active
          - id: g_archived
            name: Archived
            stratum_id: L0
            status: archived
        edges: []
    """
    config = FleetConfig.load(_write(tmp_path, yaml))
    scope = config.auto_bind_scope()
    assert scope is not None
    assert scope.id == "g_active"


def test_auto_bind_scope_returns_none_for_empty_fleet(tmp_path: Path) -> None:
    """A fleet with zero scopes has no auto-bind target."""
    yaml = """
        strata: []
        scopes: []
        edges: []
    """
    config = FleetConfig.load(_write(tmp_path, yaml))
    assert config.auto_bind_scope() is None


def test_get_scope_returns_archived_scope(tmp_path: Path) -> None:
    """get_scope finds an archived scope (it still exists in the config)."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_archived
            name: Archived
            stratum_id: L0
            status: archived
        edges: []
    """
    config = FleetConfig.load(_write(tmp_path, yaml))
    scope = config.get_scope("g_archived")
    assert scope is not None
    assert scope.status == "archived"


# ---------------------------------------------------------------------------
# Skill declaration fields
# ---------------------------------------------------------------------------


def test_default_skill_alone_accepted(tmp_path: Path) -> None:
    """A scope with only default_skill (no permitted_skills) is valid."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
            default_skill: scope-manager
        edges: []
    """
    config = FleetConfig.load(_write(tmp_path, yaml))
    assert config.scopes[0].default_skill == "scope-manager"
    assert config.scopes[0].permitted_skills is None


def test_permitted_skills_alone_accepted(tmp_path: Path) -> None:
    """A scope with only permitted_skills (no default_skill) is valid."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
            permitted_skills: [scope-manager, evidence-summarizer]
        edges: []
    """
    config = FleetConfig.load(_write(tmp_path, yaml))
    assert config.scopes[0].default_skill is None
    assert "scope-manager" in config.scopes[0].permitted_skills


def test_both_skills_consistent_accepted(tmp_path: Path) -> None:
    """A scope with default_skill ∈ permitted_skills loads without error."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
            default_skill: scope-manager
            permitted_skills: [scope-manager, evidence-summarizer]
        edges: []
    """
    FleetConfig.load(_write(tmp_path, yaml))


def test_both_skills_drift_rejected(tmp_path: Path) -> None:
    """default_skill not in permitted_skills is the drift case — load-time error."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
            default_skill: code-writer
            permitted_skills: [scope-manager]
        edges: []
    """
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(_write(tmp_path, yaml))
    assert exc_info.value.kind == "skill_drift"


# ---------------------------------------------------------------------------
# Mutation API
# ---------------------------------------------------------------------------


def test_add_stratum_persists_to_disk(tmp_path: Path) -> None:
    """add_stratum mutates the YAML on disk and refreshes in-memory state."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes: []
        edges: []
    """
    path = _write(tmp_path, yaml)
    config = FleetConfig.load(path)
    assert len(config.strata) == 1

    config.add_stratum(id="L1", name="Function", ordinal=1)

    assert len(config.strata) == 2
    assert any(s.id == "L1" for s in config.strata)

    # Reload from disk to confirm persistence.
    reloaded = FleetConfig.load(path)
    assert len(reloaded.strata) == 2


def test_add_scope_persists_to_disk(tmp_path: Path) -> None:
    """add_scope mutates the YAML on disk and refreshes in-memory state."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes: []
        edges: []
    """
    path = _write(tmp_path, yaml)
    config = FleetConfig.load(path)

    config.add_scope(id="g_ceo", name="CEO", stratum_id="L0")

    assert len(config.scopes) == 1
    assert config.scopes[0].id == "g_ceo"
    assert config.scopes[0].status == "active"

    reloaded = FleetConfig.load(path)
    assert reloaded.get_scope("g_ceo") is not None


def test_add_edge_persists_to_disk(tmp_path: Path) -> None:
    """add_edge mutates the YAML on disk and refreshes in-memory state."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
          - id: L1
            name: Function
            ordinal: 1
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
          - id: g_eng
            name: Engineering
            stratum_id: L1
        edges: []
    """
    path = _write(tmp_path, yaml)
    config = FleetConfig.load(path)
    assert len(config.edges) == 0

    config.add_edge(from_scope_id="g_eng", to_scope_id="g_ceo")

    assert len(config.edges) == 1
    assert config.edges[0].from_ == "g_eng"
    assert config.edges[0].to == "g_ceo"

    reloaded = FleetConfig.load(path)
    assert len(reloaded.edges) == 1


def test_archive_scope_persists_to_disk(tmp_path: Path) -> None:
    """archive_scope sets status=archived on disk and refreshes in-memory state."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
            status: active
        edges: []
    """
    path = _write(tmp_path, yaml)
    config = FleetConfig.load(path)
    assert config.get_scope("g_ceo").status == "active"

    config.archive_scope("g_ceo")

    assert config.get_scope("g_ceo").status == "archived"
    assert config.get_scope("g_ceo") not in config.active_scopes()

    reloaded = FleetConfig.load(path)
    assert reloaded.get_scope("g_ceo").status == "archived"


def test_archive_scope_unknown_raises(tmp_path: Path) -> None:
    """archive_scope on an unknown scope_id raises FleetConfigError."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes: []
        edges: []
    """
    path = _write(tmp_path, yaml)
    config = FleetConfig.load(path)

    with pytest.raises(FleetConfigError) as exc_info:
        config.archive_scope("g_does_not_exist")
    assert exc_info.value.kind == "scope_not_found"


def test_add_invalid_edge_raises_and_does_not_persist(tmp_path: Path) -> None:
    """add_edge with a self-loop raises and leaves the file unchanged."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
        edges: []
    """
    path = _write(tmp_path, yaml)
    config = FleetConfig.load(path)

    with pytest.raises(FleetConfigError) as exc_info:
        config.add_edge(from_scope_id="g_ceo", to_scope_id="g_ceo")
    assert exc_info.value.kind == "self_loop"

    # File must be unchanged.
    reloaded = FleetConfig.load(path)
    assert len(reloaded.edges) == 0


# ---------------------------------------------------------------------------
# inter_stratum_parent helper
# ---------------------------------------------------------------------------

_CHAIN_YAML = """
strata:
  - id: L0
    name: Executive
    ordinal: 0
  - id: L1
    name: Function
    ordinal: 1
  - id: L2
    name: Team
    ordinal: 2

scopes:
  - id: g_exec
    name: Executive
    stratum_id: L0
  - id: g_func
    name: Function
    stratum_id: L1
  - id: g_team
    name: Team
    stratum_id: L2
  - id: g_peer
    name: Peer Function
    stratum_id: L1

edges:
  # Inter-stratum: child → parent (from=child, to=parent)
  - from: g_func
    to: g_exec
  - from: g_team
    to: g_func
  - from: g_peer
    to: g_exec
  # Intra-stratum peer reference (same L1 — must NOT be returned as parent)
  - from: g_func
    to: g_peer
"""


def test_inter_stratum_parent_returns_single_parent(tmp_path: Path) -> None:
    """inter_stratum_parent returns the inter-stratum parent for a non-root scope."""
    config = FleetConfig.load(_write(tmp_path, _CHAIN_YAML))

    parent = config.inter_stratum_parent("g_team")

    assert parent is not None
    assert parent.id == "g_func"


def test_inter_stratum_parent_root_scope_returns_none(tmp_path: Path) -> None:
    """inter_stratum_parent returns None for a root (L0) scope."""
    config = FleetConfig.load(_write(tmp_path, _CHAIN_YAML))

    parent = config.inter_stratum_parent("g_exec")

    assert parent is None


def test_inter_stratum_parent_ignores_peer_edges(tmp_path: Path) -> None:
    """inter_stratum_parent must not follow intra-stratum (peer) edges.

    g_func has a peer edge to g_peer (both L1). inter_stratum_parent("g_func")
    must return g_exec (L0), not g_peer (L1).
    """
    config = FleetConfig.load(_write(tmp_path, _CHAIN_YAML))

    parent = config.inter_stratum_parent("g_func")

    assert parent is not None
    assert parent.id == "g_exec"
    assert parent.id != "g_peer"


def test_inter_stratum_ancestors_returns_root_first(tmp_path: Path) -> None:
    """inter_stratum_ancestors returns ancestor chain ordered root-first."""
    config = FleetConfig.load(_write(tmp_path, _CHAIN_YAML))

    ancestors = config.inter_stratum_ancestors("g_team")

    assert [a.id for a in ancestors] == ["g_exec", "g_func"]


def test_inter_stratum_ancestors_root_scope_returns_empty(tmp_path: Path) -> None:
    """inter_stratum_ancestors returns an empty list for a root (L0) scope."""
    config = FleetConfig.load(_write(tmp_path, _CHAIN_YAML))

    ancestors = config.inter_stratum_ancestors("g_exec")

    assert ancestors == []


_DOWNWARD_EDGE_YAML = """
strata:
  - id: L0
    name: Executive
    ordinal: 0
  - id: L1
    name: Function
    ordinal: 1
  - id: L2
    name: Team
    ordinal: 2

scopes:
  - id: g_root
    name: Root
    stratum_id: L0
  - id: g_mid
    name: Mid
    stratum_id: L1
  - id: g_leaf
    name: Leaf
    stratum_id: L2

edges:
  # Inverted downward edge listed FIRST so a buggy `!= ordinal` resolver
  # would return g_leaf (the descendant) before reaching the upward edge.
  # g_leaf is NOT g_mid's parent; only a strict `< ordinal` resolver skips it.
  # The ±1 stratum invariant (#7) is direction-agnostic so this passes load.
  - from: g_mid
    to: g_leaf
  # Proper upward edge: g_mid (L1) → g_root (L0). g_root is g_mid's true parent.
  - from: g_mid
    to: g_root
"""


def test_inter_stratum_parent_ignores_downward_edges(tmp_path: Path) -> None:
    """inter_stratum_parent must not follow edges to *higher*-ordinal scopes.

    Per ADR 0002, parents have lower stratum ordinals than children
    (ordinal 0 is the broadest). An edge from a scope to a higher-ordinal
    scope is a descendant reference and must be ignored when resolving
    the parent. Regression test for the bug where `!= current_ordinal`
    would silently return the descendant.
    """
    config = FleetConfig.load(_write(tmp_path, _DOWNWARD_EDGE_YAML))

    parent = config.inter_stratum_parent("g_mid")

    assert parent is not None, "g_mid has a valid upward edge to g_root"
    assert parent.id == "g_root", (
        f"expected g_mid's parent to be g_root (lower ordinal), got {parent.id!r}"
    )


# ---------------------------------------------------------------------------
# entitlement_view (ADR 0006 D2)
# ---------------------------------------------------------------------------

_ENTITLEMENT_YAML = """
strata:
  - id: L0
    name: Executive
    ordinal: 0
  - id: L1
    name: Function
    ordinal: 1
  - id: L2
    name: Team
    ordinal: 2

scopes:
  - id: g_exec
    name: Executive
    stratum_id: L0
  - id: g_funcA
    name: Function A
    stratum_id: L1
  - id: g_funcB
    name: Function B
    stratum_id: L1
  - id: g_funcC
    name: Function C
    stratum_id: L1
  - id: g_funcD
    name: Function D
    stratum_id: L1
    status: archived
  - id: g_funcE
    name: Function E
    stratum_id: L1
    status: archived
  - id: g_teamX
    name: Team X
    stratum_id: L2
  - id: g_teamSibling
    name: Team Sibling
    stratum_id: L2

edges:
  # Inter-stratum: child -> parent.
  - from: g_funcA
    to: g_exec
  - from: g_teamX
    to: g_funcA
  - from: g_teamSibling
    to: g_funcA
  # Intra-stratum: g_funcA references g_funcB (one hop -> referenced peer).
  - from: g_funcA
    to: g_funcB
  # Intra-stratum: g_funcA references archived g_funcD -> must be excluded.
  - from: g_funcA
    to: g_funcD
  # Intra-stratum: g_funcB references g_funcC -> peer-of-peer, must NOT
  # appear as a referenced peer of g_teamX (only chain-sourced edges count).
  - from: g_funcB
    to: g_funcC
"""


def test_entitlement_view_chain_is_ancestors_plus_self(tmp_path: Path) -> None:
    """chain is the ancestor chain (root-first) with the scope itself last."""
    config = FleetConfig.load(_write(tmp_path, _ENTITLEMENT_YAML))

    view = config.entitlement_view("g_teamX")

    assert [s.id for s in view.chain] == ["g_exec", "g_funcA", "g_teamX"]


def test_entitlement_view_referenced_peer_one_hop_only(tmp_path: Path) -> None:
    """A peer referenced by a chain scope appears; a peer-of-peer does not."""
    config = FleetConfig.load(_write(tmp_path, _ENTITLEMENT_YAML))

    view = config.entitlement_view("g_teamX")
    peer_ids = {s.id for s in view.referenced_peers}

    assert "g_funcB" in peer_ids
    assert "g_funcC" not in peer_ids, "peer-of-peer must not be a referenced peer"


def test_entitlement_view_peer_referenced_by_ancestor_appears(tmp_path: Path) -> None:
    """A peer referenced by an ANCESTOR (not the judged scope itself) still appears."""
    config = FleetConfig.load(_write(tmp_path, _ENTITLEMENT_YAML))

    view = config.entitlement_view("g_teamX")

    # g_funcA (an ancestor of g_teamX) references g_funcB, not g_teamX itself.
    assert any(s.id == "g_funcB" for s in view.referenced_peers)


def test_entitlement_view_unreferenced_sibling_lands_in_others(tmp_path: Path) -> None:
    """A sibling scope with no reference edge lands in 'others'."""
    config = FleetConfig.load(_write(tmp_path, _ENTITLEMENT_YAML))

    view = config.entitlement_view("g_teamX")

    assert any(s.id == "g_teamSibling" for s in view.others)
    assert not any(s.id == "g_teamSibling" for s in view.chain)
    assert not any(s.id == "g_teamSibling" for s in view.referenced_peers)


def test_entitlement_view_peer_of_peer_lands_in_others(tmp_path: Path) -> None:
    """The excluded peer-of-peer (g_funcC) still shows up somewhere — in 'others'."""
    config = FleetConfig.load(_write(tmp_path, _ENTITLEMENT_YAML))

    view = config.entitlement_view("g_teamX")

    assert any(s.id == "g_funcC" for s in view.others)


def test_entitlement_view_archived_scopes_land_in_others(tmp_path: Path) -> None:
    """Archived scopes are enumerated in 'others', never as entitled groups.

    They must not vanish from the view entirely: the judge distinguishes
    fleet-internal origins from external material by exact name matching,
    and an archived origin scope that disappeared from the enumeration
    would read as external and slip past the admission rule (fresh-eyes
    review finding F2).
    """
    config = FleetConfig.load(_write(tmp_path, _ENTITLEMENT_YAML))

    view = config.entitlement_view("g_teamX")
    entitled_ids = {s.id for s in (*view.chain, *view.descendants, *view.referenced_peers)}
    other_ids = {s.id for s in view.others}

    assert "g_funcD" not in entitled_ids, "archived scope must not be an entitled peer"
    assert "g_funcD" in other_ids, "referenced-but-archived scope must still be enumerated"
    assert "g_funcE" in other_ids, "unreferenced archived scope must still be enumerated"


def test_entitlement_view_descendants_are_entitled_not_others(tmp_path: Path) -> None:
    """A scope's descendants land in 'descendants', never in 'others'.

    This is the F1 regression pin: ADR 0006 D1 permits descendant-bound
    agents to propose upward, so the judge must see descendants as entitled
    evidence sources — a descendant listed as NOT entitled would instruct
    the judge to decline the legitimate upward-evidence flow.
    """
    config = FleetConfig.load(_write(tmp_path, _ENTITLEMENT_YAML))

    view = config.entitlement_view("g_funcA")

    descendant_ids = {s.id for s in view.descendants}
    other_ids = {s.id for s in view.others}
    assert descendant_ids == {"g_teamX", "g_teamSibling"}
    assert not descendant_ids & other_ids


def test_entitlement_view_descendants_any_depth_and_leaf_empty(tmp_path: Path) -> None:
    """Descendants include grandchildren (any depth); a leaf scope has none."""
    config = FleetConfig.load(_write(tmp_path, _ENTITLEMENT_YAML))

    exec_view = config.entitlement_view("g_exec")
    assert {s.id for s in exec_view.descendants} == {"g_funcA", "g_teamX", "g_teamSibling"}

    leaf_view = config.entitlement_view("g_teamX")
    assert leaf_view.descendants == []


def test_entitlement_view_root_scope_works(tmp_path: Path) -> None:
    """A root (L0) scope with no ancestors still produces a valid view."""
    config = FleetConfig.load(_write(tmp_path, _ENTITLEMENT_YAML))

    view = config.entitlement_view("g_exec")

    assert [s.id for s in view.chain] == ["g_exec"]
    assert view.referenced_peers == []
    other_ids = {s.id for s in view.others}
    assert other_ids == {"g_funcB", "g_funcC", "g_funcD", "g_funcE"}


# ---------------------------------------------------------------------------
# references_from (ADR 0013 D3 — publication travels exactly one edge; a
# perspective composes only the scope's OWN reference edges, never an
# ancestor's).
# ---------------------------------------------------------------------------


def test_references_from_returns_only_the_scope_s_own_reference(tmp_path: Path) -> None:
    """g_funcA's own reference (g_funcB) is returned; g_funcB's own reference (g_funcC) is not."""
    config = FleetConfig.load(_write(tmp_path, _ENTITLEMENT_YAML))

    assert [s.id for s in config.references_from("g_funcA")] == ["g_funcB"]


def test_references_from_excludes_ancestor_s_reference(tmp_path: Path) -> None:
    """g_teamX has no reference edges of its own — g_funcA's reference to g_funcB
    does not belong to g_teamX just because g_funcA is its ancestor."""
    config = FleetConfig.load(_write(tmp_path, _ENTITLEMENT_YAML))

    assert config.references_from("g_teamX") == []


def test_references_from_excludes_archived_targets(tmp_path: Path) -> None:
    """A reference to an archived scope is not returned."""
    config = FleetConfig.load(_write(tmp_path, _ENTITLEMENT_YAML))

    ids = [s.id for s in config.references_from("g_funcA")]
    assert "g_funcD" not in ids


def test_references_from_sorted_and_deterministic(tmp_path: Path) -> None:
    """Multiple references from the same scope come back sorted by scope id."""
    yaml_text = """
    strata:
      - id: L0
        name: Executive
        ordinal: 0
      - id: L1
        name: Function
        ordinal: 1

    scopes:
      - id: g_exec
        name: Executive
        stratum_id: L0
      - id: g_funcZ
        name: Function Z
        stratum_id: L1
      - id: g_funcA
        name: Function A
        stratum_id: L1

    edges:
      - from: g_exec
        to: g_funcZ
        kind: reference
      - from: g_exec
        to: g_funcA
        kind: reference
    """
    config = FleetConfig.load(_write(tmp_path, yaml_text))

    assert [s.id for s in config.references_from("g_exec")] == ["g_funcA", "g_funcZ"]


def test_references_from_root_scope_with_no_references_is_empty(tmp_path: Path) -> None:
    config = FleetConfig.load(_write(tmp_path, _ENTITLEMENT_YAML))

    assert config.references_from("g_exec") == []


# ---------------------------------------------------------------------------
# ADR 0008 — reserved "operator" stratum label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stratum_id", ["operator", "Operator", "OPERATOR", "OpErAtOr"])
def test_reserved_operator_stratum_id_rejected_case_insensitive(
    tmp_path: Path, stratum_id: str
) -> None:
    """A fleet stratum may not claim the reserved 'operator' label, in any case."""
    bad = f"""
        strata:
          - id: {stratum_id}
            name: Bad Stratum
            ordinal: 0
        scopes: []
        edges: []
    """
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(_write(tmp_path, bad))
    assert exc_info.value.kind == "reserved_stratum_id"
    assert stratum_id in exc_info.value.message


def test_non_reserved_stratum_ids_accepted(tmp_path: Path) -> None:
    """A stratum named anything other than 'operator' loads fine."""
    FleetConfig.load(_write(tmp_path, _VALID_YAML))


def test_reserved_stratum_check_runs_before_duplicate_check(tmp_path: Path) -> None:
    """The reserved-label check is first — it fires even alongside other violations."""
    bad = """
        strata:
          - id: operator
            name: First
            ordinal: 0
          - id: operator
            name: Second
            ordinal: 0
        scopes: []
        edges: []
    """
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(_write(tmp_path, bad))
    assert exc_info.value.kind == "reserved_stratum_id"


# ---------------------------------------------------------------------------
# ADR 0010 — typed edges (issue #127, closing issue #123)
# ---------------------------------------------------------------------------

_TYPED_EDGE_STRATA = """
strata:
  - id: L0
    name: Executive
    ordinal: 0
  - id: L1
    name: Function
    ordinal: 1
  - id: L2
    name: Team
    ordinal: 2

scopes:
  - id: g_exec
    name: Executive
    stratum_id: L0
  - id: g_funcA
    name: Function A
    stratum_id: L1
  - id: g_funcB
    name: Function B
    stratum_id: L1
  - id: g_teamX
    name: Team X
    stratum_id: L2
  - id: g_teamY
    name: Team Y
    stratum_id: L2
"""

#: The issue #123 shape: every edge authored top-down (from = the broader
#: scope), which used to derive nothing at all.
_INVERTED_EDGES = """
edges:
  - from: g_exec
    to: g_funcA
  - from: g_funcA
    to: g_teamX
"""


def _typed_fleet(tmp_path: Path, edges: str, name: str = "fleet.yaml") -> Path:
    """Write a fleet.yaml combining the shared typed-edge scopes with *edges*."""
    return _write(tmp_path, _TYPED_EDGE_STRATA + textwrap.dedent(edges), name)


def test_untyped_adjacent_edge_is_a_chain_edge(tmp_path: Path) -> None:
    """An untyped edge between adjacent strata means a chain edge (ADR 0010 D4)."""
    config = FleetConfig.load(
        _typed_fleet(
            tmp_path,
            """
            edges:
              - from: g_funcA
                to: g_exec
            """,
        )
    )

    assert [(e.from_, e.to, e.kind) for e in config.edges] == [("g_funcA", "g_exec", "chain")]


def test_untyped_same_stratum_edge_is_a_reference_edge(tmp_path: Path) -> None:
    """An untyped edge within one stratum means a reference edge — the peer reference."""
    config = FleetConfig.load(
        _typed_fleet(
            tmp_path,
            """
            edges:
              - from: g_funcA
                to: g_funcB
            """,
        )
    )

    assert [(e.from_, e.to, e.kind) for e in config.edges] == [("g_funcA", "g_funcB", "reference")]
    # A reference edge is never ancestry, and its direction is preserved.
    assert config.inter_stratum_parent("g_funcA") is None
    assert [s.id for s in config.entitlement_view("g_funcA").referenced_peers] == ["g_funcB"]


def test_untyped_distance_two_edge_rejected_naming_reference(tmp_path: Path) -> None:
    """An untyped edge spanning two strata has no default — the error names the fix."""
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(
            _typed_fleet(
                tmp_path,
                """
                edges:
                  - from: g_teamX
                    to: g_exec
                """,
            )
        )

    assert exc_info.value.kind == "stratum_distance_violation"
    assert "g_teamX" in exc_info.value.message
    assert "g_exec" in exc_info.value.message
    assert "kind: reference" in exc_info.value.message


def test_inverted_authored_chain_edge_is_canonicalized(tmp_path: Path) -> None:
    """A top-down authored adjacent edge loads as a chain edge oriented child→parent.

    This is issue #123's headline fix at the config layer: the authored
    direction of a chain edge carries no meaning, so an inverted fleet
    derives exactly what a correctly authored one derives.
    """
    config = FleetConfig.load(_typed_fleet(tmp_path, _INVERTED_EDGES))

    assert [(e.from_, e.to, e.kind) for e in config.edges] == [
        ("g_funcA", "g_exec", "chain"),
        ("g_teamX", "g_funcA", "chain"),
    ]
    assert config.inter_stratum_parent("g_teamX").id == "g_funcA"
    assert [s.id for s in config.inter_stratum_ancestors("g_teamX")] == ["g_exec", "g_funcA"]
    # The inverted authoring never made the broader scope anyone's child.
    assert config.inter_stratum_parent("g_exec") is None


def test_explicit_chain_edge_spanning_two_strata_rejected(tmp_path: Path) -> None:
    """A declared chain edge is legal only between adjacent strata."""
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(
            _typed_fleet(
                tmp_path,
                """
                edges:
                  - from: g_teamX
                    to: g_exec
                    kind: chain
                """,
            )
        )

    assert exc_info.value.kind == "chain_edge_not_adjacent"
    assert "kind: reference" in exc_info.value.message


def test_explicit_chain_edge_within_one_stratum_rejected(tmp_path: Path) -> None:
    """A declared chain edge between two scopes on one stratum has no parent to name."""
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(
            _typed_fleet(
                tmp_path,
                """
                edges:
                  - from: g_funcA
                    to: g_funcB
                    kind: chain
                """,
            )
        )

    assert exc_info.value.kind == "chain_edge_not_adjacent"


def test_reference_edge_spans_any_distance_in_both_directions(tmp_path: Path) -> None:
    """A declared reference edge is legal upward at any distance, and downward."""
    config = FleetConfig.load(
        _typed_fleet(
            tmp_path,
            """
            edges:
              - from: g_teamX
                to: g_funcA
              - from: g_teamX
                to: g_exec
                kind: reference
              - from: g_exec
                to: g_teamY
                kind: reference
            """,
        )
    )

    assert [(e.from_, e.to, e.kind) for e in config.edges] == [
        ("g_teamX", "g_funcA", "chain"),
        ("g_teamX", "g_exec", "reference"),
        ("g_exec", "g_teamY", "reference"),
    ]
    # Neither reference is ancestry: g_teamX's parent is still its chain edge.
    assert config.inter_stratum_parent("g_teamX").id == "g_funcA"
    assert config.inter_stratum_parent("g_teamY") is None


def test_downward_reference_entitles_the_referencing_scope(tmp_path: Path) -> None:
    """A downward reference entitles the referencing scope to the referenced publication."""
    config = FleetConfig.load(
        _typed_fleet(
            tmp_path,
            """
            edges:
              - from: g_funcA
                to: g_exec
              - from: g_teamX
                to: g_funcA
              - from: g_exec
                to: g_teamY
                kind: reference
            """,
        )
    )

    assert [s.id for s in config.entitlement_view("g_exec").referenced_peers] == ["g_teamY"]
    # Direction is load-bearing: g_teamY references nothing back.
    assert config.entitlement_view("g_teamY").referenced_peers == []


def test_reference_cycle_is_legal(tmp_path: Path) -> None:
    """Reference edges may form cycles — nothing binds, so there is nothing to resolve."""
    config = FleetConfig.load(
        _typed_fleet(
            tmp_path,
            """
            edges:
              - from: g_funcA
                to: g_funcB
              - from: g_funcB
                to: g_funcA
              - from: g_teamX
                to: g_exec
                kind: reference
              - from: g_exec
                to: g_teamX
                kind: reference
            """,
        )
    )

    assert [s.id for s in config.entitlement_view("g_funcA").referenced_peers] == ["g_funcB"]
    assert [s.id for s in config.entitlement_view("g_funcB").referenced_peers] == ["g_funcA"]
    assert [s.id for s in config.entitlement_view("g_teamX").referenced_peers] == ["g_exec"]
    assert [s.id for s in config.entitlement_view("g_exec").referenced_peers] == ["g_teamX"]


@pytest.mark.parametrize("kind_line", ["", "    kind: chain", "    kind: reference"])
def test_self_edge_rejected_for_every_kind(tmp_path: Path, kind_line: str) -> None:
    """A scope may not edge to itself, typed or untyped, chain or reference."""
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(
            _typed_fleet(
                tmp_path,
                "\nedges:\n  - from: g_funcA\n    to: g_funcA\n" + kind_line + "\n",
            )
        )

    assert exc_info.value.kind == "self_loop"
    assert "g_funcA" in exc_info.value.message


def test_two_explicit_chain_edges_rejected_naming_both(tmp_path: Path) -> None:
    """Two DECLARED chain edges onto one child are two parents, and the error names both.

    Strictness lives where intent is declared (ADR 0010 D5): an untyped edge
    that cannot have the slot demotes silently, but `kind: chain` said what it
    meant and the fleet cannot honour both.
    """
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(
            _typed_fleet(
                tmp_path,
                """
                edges:
                  - from: g_funcA
                    to: g_teamX
                    kind: chain
                  - from: g_teamX
                    to: g_funcB
                    kind: chain
                """,
            )
        )

    assert exc_info.value.kind == "multiple_inter_stratum_parents"
    message = exc_info.value.message
    assert "g_teamX" in message
    # Both offending edges are named as written, so the author can find them.
    assert "g_funcA -> g_teamX" in message
    assert "g_teamX -> g_funcB" in message


def test_chain_parent_plus_several_references_all_resolve(tmp_path: Path) -> None:
    """One scope carries one chain parent and any number of references at once."""
    config = FleetConfig.load(
        _typed_fleet(
            tmp_path,
            """
            edges:
              - from: g_funcA
                to: g_exec
              - from: g_funcA
                to: g_teamX
              - from: g_funcA
                to: g_funcB
              - from: g_funcA
                to: g_teamY
                kind: reference
            """,
        )
    )

    # The edge to g_teamX canonicalizes into g_teamX's chain edge to a parent;
    # only the upward one is g_funcA's own parent.
    assert config.inter_stratum_parent("g_funcA").id == "g_exec"
    assert config.inter_stratum_parent("g_teamX").id == "g_funcA"
    view = config.entitlement_view("g_funcA")
    assert [s.id for s in view.referenced_peers] == ["g_funcB", "g_teamY"]


def test_cross_stratum_reference_reaches_the_whole_chain(tmp_path: Path) -> None:
    """The uncle case: a reference on the chain entitles the scope reading that chain."""
    config = FleetConfig.load(
        _typed_fleet(
            tmp_path,
            """
            edges:
              - from: g_funcA
                to: g_exec
              - from: g_teamX
                to: g_funcA
              - from: g_teamX
                to: g_funcB
                kind: reference
            """,
        )
    )

    view = config.entitlement_view("g_teamX")
    assert [s.id for s in view.chain] == ["g_exec", "g_funcA", "g_teamX"]
    assert [s.id for s in view.referenced_peers] == ["g_funcB"]
    assert not any(s.id == "g_funcB" for s in view.others)


def test_referenced_descendant_is_both_referenced_and_a_descendant(tmp_path: Path) -> None:
    """A referenced descendant keeps both entitlements — read access and upward standing."""
    config = FleetConfig.load(
        _typed_fleet(
            tmp_path,
            """
            edges:
              - from: g_funcA
                to: g_exec
              - from: g_teamX
                to: g_funcA
              - from: g_exec
                to: g_teamX
                kind: reference
            """,
        )
    )

    view = config.entitlement_view("g_exec")
    assert [s.id for s in view.referenced_peers] == ["g_teamX"]
    assert "g_teamX" in {s.id for s in view.descendants}
    assert not any(s.id == "g_teamX" for s in view.others)


def test_add_edge_writes_a_chain_edge_child_to_parent(tmp_path: Path) -> None:
    """add_edge canonicalizes on the way to disk, whichever order it was called with."""
    path = _typed_fleet(tmp_path, "\nedges: []\n")
    config = FleetConfig.load(path)

    config.add_edge(from_scope_id="g_exec", to_scope_id="g_funcA")

    assert [(e.from_, e.to, e.kind) for e in config.edges] == [("g_funcA", "g_exec", "chain")]
    reloaded = FleetConfig.load(path)
    assert [(e.from_, e.to) for e in reloaded.edges] == [("g_funcA", "g_exec")]


def test_add_edge_declares_the_kind_it_was_given(tmp_path: Path) -> None:
    """An explicit kind reaches disk and survives a reload."""
    path = _typed_fleet(tmp_path, "\nedges: []\n")
    config = FleetConfig.load(path)

    config.add_edge(from_scope_id="g_teamX", to_scope_id="g_exec", kind="reference")

    reloaded = FleetConfig.load(path)
    assert [(e.from_, e.to, e.kind) for e in reloaded.edges] == [("g_teamX", "g_exec", "reference")]


def test_any_mutation_canonicalizes_an_inverted_edge_on_disk(tmp_path: Path) -> None:
    """The file catches up with the loaded meaning the next time anything writes it."""
    path = _typed_fleet(tmp_path, _INVERTED_EDGES)
    config = FleetConfig.load(path)

    config.add_scope(id="g_teamZ", name="Team Z", stratum_id="L2")

    reloaded = FleetConfig.load(path)
    assert [(e.from_, e.to) for e in reloaded.edges] == [
        ("g_funcA", "g_exec"),
        ("g_teamX", "g_funcA"),
    ]
    # Canonicalizing orientation never invents a `kind` the author did not write.
    assert "kind:" not in path.read_text(encoding="utf-8")


def test_adjacent_strata_reference_edge_is_not_a_parent(tmp_path: Path) -> None:
    """`kind: reference` on an adjacent pair references — it does not bind.

    The distance alone would have inferred a chain edge, so this is the case
    where the declared kind is the only thing distinguishing "informs me"
    from "binds me".
    """
    config = FleetConfig.load(
        _typed_fleet(
            tmp_path,
            """
            edges:
              - from: g_teamX
                to: g_funcA
              - from: g_teamX
                to: g_funcB
                kind: reference
            """,
        )
    )

    assert config.inter_stratum_parent("g_teamX").id == "g_funcA"
    view = config.entitlement_view("g_teamX")
    assert [s.id for s in view.chain] == ["g_funcA", "g_teamX"]
    assert [s.id for s in view.referenced_peers] == ["g_funcB"]


def test_second_parent_is_avoidable_by_declaring_a_reference(tmp_path: Path) -> None:
    """Two adjacent edges out of one scope are legal once one declares itself a reference.

    Untyped, this pair is the two-parents error; declaring the second one a
    reference is the fix the error message names.
    """
    untyped = """
        edges:
          - from: g_teamX
            to: g_funcA
          - from: g_teamX
            to: g_funcB
        """
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(_typed_fleet(tmp_path, untyped))
    assert exc_info.value.kind == "multiple_inter_stratum_parents"
    assert "kind: reference" in exc_info.value.message

    config = FleetConfig.load(
        _typed_fleet(
            tmp_path,
            """
            edges:
              - from: g_teamX
                to: g_funcA
              - from: g_teamX
                to: g_funcB
                kind: reference
            """,
            name="typed.yaml",
        )
    )
    assert [(e.from_, e.to, e.kind) for e in config.edges] == [
        ("g_teamX", "g_funcA", "chain"),
        ("g_teamX", "g_funcB", "reference"),
    ]


# ---------------------------------------------------------------------------
# ADR 0010 D5 — load-lenient, write-strict.
#
# The upgrade invariant: data that LOADED yesterday must LOAD today. An engine
# upgrade may add meaning to stored data, never make it unreadable. Resolving
# untyped adjacent edges per CHILD rather than per edge is what holds that
# line — a legacy fleet carrying a formerly-inert inverted edge alongside a
# correct parent edge kept loading, because the inverted one demotes to a
# reference instead of becoming a second parent.
# ---------------------------------------------------------------------------


def test_legacy_correct_plus_inverted_onto_one_child_still_loads(tmp_path: Path) -> None:
    """Shape (a): the correct edge keeps the chain slot; the inverted one demotes.

    This exact fleet loaded fine before ADR 0010 (the old counter saw only the
    correctly authored edge) and must keep loading.
    """
    config = FleetConfig.load(
        _typed_fleet(
            tmp_path,
            """
            edges:
              - from: g_teamX
                to: g_funcA
              - from: g_funcB
                to: g_teamX
            """,
        )
    )

    assert [(e.from_, e.to, e.kind) for e in config.edges] == [
        ("g_teamX", "g_funcA", "chain"),
        # Demoted, and oriented so the flow the author drew survives: g_teamX
        # reads g_funcB's publication rather than the reverse.
        ("g_teamX", "g_funcB", "reference"),
    ]
    assert config.inter_stratum_parent("g_teamX").id == "g_funcA"
    view = config.entitlement_view("g_teamX")
    assert [s.id for s in view.chain] == ["g_funcA", "g_teamX"]
    assert [s.id for s in view.referenced_peers] == ["g_funcB"]


def test_legacy_two_inverted_onto_one_child_still_loads(tmp_path: Path) -> None:
    """Shape (b): with no correct edge and two candidates, both demote and neither guesses.

    The child stays parentless — exactly its pre-ADR-0010 chain behaviour —
    while gaining both publications, which is strictly more than the inert
    edges delivered and never wrong.
    """
    config = FleetConfig.load(
        _typed_fleet(
            tmp_path,
            """
            edges:
              - from: g_funcA
                to: g_teamX
              - from: g_funcB
                to: g_teamX
            """,
        )
    )

    assert [(e.from_, e.to, e.kind) for e in config.edges] == [
        ("g_teamX", "g_funcA", "reference"),
        ("g_teamX", "g_funcB", "reference"),
    ]
    assert config.inter_stratum_parent("g_teamX") is None
    assert config.inter_stratum_ancestors("g_teamX") == []
    view = config.entitlement_view("g_teamX")
    assert [s.id for s in view.chain] == ["g_teamX"]
    assert [s.id for s in view.referenced_peers] == ["g_funcA", "g_funcB"]


def test_legacy_single_inverted_still_promotes_to_chain(tmp_path: Path) -> None:
    """Shape (c): a sole inverted candidate into an empty slot still promotes (#123)."""
    config = FleetConfig.load(_typed_fleet(tmp_path, _INVERTED_EDGES))

    assert [(e.from_, e.to, e.kind) for e in config.edges] == [
        ("g_funcA", "g_exec", "chain"),
        ("g_teamX", "g_funcA", "chain"),
    ]
    assert [s.id for s in config.inter_stratum_ancestors("g_teamX")] == ["g_exec", "g_funcA"]


def test_declared_chain_keeps_the_slot_against_an_inverted_untyped_edge(tmp_path: Path) -> None:
    """A declared chain occupies the slot, so an inverted untyped candidate demotes."""
    config = FleetConfig.load(
        _typed_fleet(
            tmp_path,
            """
            edges:
              - from: g_teamX
                to: g_funcA
                kind: chain
              - from: g_funcB
                to: g_teamX
            """,
        )
    )

    assert [(e.from_, e.to, e.kind) for e in config.edges] == [
        ("g_teamX", "g_funcA", "chain"),
        ("g_teamX", "g_funcB", "reference"),
    ]
    assert config.inter_stratum_parent("g_teamX").id == "g_funcA"


def test_demoted_edge_composes_as_a_reference_layer(tmp_path: Path) -> None:
    """The demoted edge is a real reference: it reaches the entitled context surface."""
    config = FleetConfig.load(
        _typed_fleet(
            tmp_path,
            """
            edges:
              - from: g_funcA
                to: g_exec
              - from: g_teamX
                to: g_funcA
              - from: g_funcB
                to: g_teamX
            """,
        )
    )

    view = config.entitlement_view("g_teamX")
    assert [s.id for s in view.chain] == ["g_exec", "g_funcA", "g_teamX"]
    assert [s.id for s in view.referenced_peers] == ["g_funcB"]
    # The demoted scope is entitled, so it never reads as foreign material.
    assert not any(s.id == "g_funcB" for s in view.others)


def test_mutation_materializes_a_demoted_kind_on_disk(tmp_path: Path) -> None:
    """A write through any mutator persists `kind: reference` on the demoted edge.

    The file stops depending on the resolution rules to mean what it means:
    reloading it yields the same shape from an explicit declaration.
    """
    path = _typed_fleet(
        tmp_path,
        """
        edges:
          - from: g_teamX
            to: g_funcA
          - from: g_funcB
            to: g_teamX
        """,
    )
    config = FleetConfig.load(path)

    config.add_scope(id="g_teamZ", name="Team Z", stratum_id="L2")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["edges"] == [
        {"from": "g_teamX", "to": "g_funcA"},
        {"from": "g_teamX", "to": "g_funcB", "kind": "reference"},
    ]
    reloaded = FleetConfig.load(path)
    assert [(e.from_, e.to, e.kind) for e in reloaded.edges] == [
        ("g_teamX", "g_funcA", "chain"),
        ("g_teamX", "g_funcB", "reference"),
    ]
    assert reloaded.inter_stratum_parent("g_teamX").id == "g_funcA"


def test_reported_upgrade_hazard_fleet_loads(tmp_path: Path) -> None:
    """The reported shape: a formerly-inert inverted edge no longer bricks the load.

    Two root-stratum scopes, one child; the child has a correct parent edge and
    is also the target of an inverted edge from the other root scope. Under
    per-edge inference this raised multiple_inter_stratum_parents and every
    fleet-backed read failed.
    """
    path = _write(
        tmp_path,
        """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
          - id: L1
            name: Function
            ordinal: 1
        scopes:
          - id: g_a
            name: A
            stratum_id: L0
          - id: g_b
            name: B
            stratum_id: L0
          - id: g_c
            name: C
            stratum_id: L1
        edges:
          - from: g_c
            to: g_a
          - from: g_b
            to: g_c
        """,
    )

    config = FleetConfig.load(path)

    assert config.inter_stratum_parent("g_c").id == "g_a"
    assert [s.id for s in config.entitlement_view("g_c").referenced_peers] == ["g_b"]


def test_add_edge_untyped_onto_a_taken_slot_becomes_a_reference(tmp_path: Path) -> None:
    """add_edge follows the same per-child resolution, and writes the demotion out.

    A caller that means "this binds" says so with ``kind="chain"`` and gets the
    two-parents error instead.
    """
    path = _typed_fleet(
        tmp_path,
        """
        edges:
          - from: g_teamX
            to: g_funcA
        """,
    )
    config = FleetConfig.load(path)

    config.add_edge(from_scope_id="g_funcB", to_scope_id="g_teamX")

    assert [(e.from_, e.to, e.kind) for e in config.edges] == [
        ("g_teamX", "g_funcA", "chain"),
        ("g_teamX", "g_funcB", "reference"),
    ]
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["edges"][1] == {"from": "g_teamX", "to": "g_funcB", "kind": "reference"}

    with pytest.raises(FleetConfigError) as exc_info:
        config.add_edge(from_scope_id="g_teamX", to_scope_id="g_funcB", kind="chain")
    assert exc_info.value.kind == "multiple_inter_stratum_parents"
