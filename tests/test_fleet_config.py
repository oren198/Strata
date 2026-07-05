"""Tests for src/strata/fleet_config.py.

Covers all 8 load-time invariants (each with a failing case and a passing
counterpart), the scope lifecycle (status defaulting and archived behaviour),
per-scope skill declaration fields, and the mutation API.

Vocabulary follows CONTEXT.md: stratum, scope, edge, fleet.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

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


def test_entitlement_view_excludes_archived_scopes(tmp_path: Path) -> None:
    """Archived scopes never appear, whether referenced or not."""
    config = FleetConfig.load(_write(tmp_path, _ENTITLEMENT_YAML))

    view = config.entitlement_view("g_teamX")
    all_ids = {s.id for s in (*view.chain, *view.referenced_peers, *view.others)}

    assert "g_funcD" not in all_ids, "referenced-but-archived scope must be excluded"
    assert "g_funcE" not in all_ids, "unreferenced archived scope must be excluded"


def test_entitlement_view_root_scope_works(tmp_path: Path) -> None:
    """A root (L0) scope with no ancestors still produces a valid view."""
    config = FleetConfig.load(_write(tmp_path, _ENTITLEMENT_YAML))

    view = config.entitlement_view("g_exec")

    assert [s.id for s in view.chain] == ["g_exec"]
    assert view.referenced_peers == []
    other_ids = {s.id for s in view.others}
    assert other_ids == {"g_funcA", "g_funcB", "g_funcC", "g_teamX", "g_teamSibling"}
