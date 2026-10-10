"""Fleet structure changes (#247): ownership, pending approval, notices, record.

No judge is called. A structure change is an authority act.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import shutil
from pathlib import Path

import pytest

import strata.fleet_changes as fleet_changes
import strata.operator as operator
from strata.fleet_changes import (
    FLAG_BINDS,
    FLAG_WIDENS,
    Actor,
    FleetChangeError,
    actor_qualifies,
    apply_as_operator,
    approve,
    change_flags,
    limit_notices,
    owner_scope,
    parse_change,
    propose,
    reject,
)
from strata.fleet_config import FleetConfig
from strata.migrator import _default_migrations_dir, run_migrations
from strata.record_store import ContributorRef, RecordStore

_FLEET = """\
strata:
  - id: s0
    name: Root
    ordinal: 0
  - id: s1
    name: Mid
    ordinal: 1
  - id: s2
    name: Leaf
    ordinal: 2
scopes:
  - id: g_root
    name: Root
    stratum_id: s0
  - id: g_a
    name: A
    stratum_id: s1
  - id: g_b
    name: B
    stratum_id: s1
  - id: g_a1
    name: A1
    stratum_id: s2
  - id: g_other
    name: Other
    stratum_id: s0
edges:
  - from: g_a
    to: g_root
    kind: chain
  - from: g_b
    to: g_root
    kind: chain
  - from: g_a1
    to: g_a
    kind: chain
  - from: g_a
    to: g_b
    kind: reference
"""

_JUDGE_BUILDER_SHA256 = "b703d4582bfd827cf8ba2a9ca6e90a8279ee771d24034b47be771a66d70c7a7e"


def _world(tmp_path: Path) -> tuple[FleetConfig, RecordStore]:
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(_FLEET, encoding="utf-8")
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    return FleetConfig.load(fleet_path), RecordStore(db_path)


def _change(change_type: str, **payload):
    return parse_change(change_type, payload)


def test_judge_message_builders_are_byte_identical() -> None:
    """Part 2 does not change what the judge is shown."""
    import strata.scope_manager as scope_manager

    digest = hashlib.sha256()
    for name in ("_build_judge_preamble", "_build_user_message", "_build_batch_user_message"):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(inspect.getsource(getattr(scope_manager, name)).encode())
    assert digest.hexdigest() == _JUDGE_BUILDER_SHA256
    assert "fleet_changes" not in inspect.getsource(scope_manager)


def test_fleet_change_code_never_emits_change_events() -> None:
    """Source scan: structure changes do not call the change-event machinery."""
    sources = [
        Path(fleet_changes.__file__).read_text(encoding="utf-8"),
        inspect.getsource(operator.record_fleet_structure_act),
    ]
    for source in sources:
        _assert_no_change_event_calls(source)


def _assert_no_change_event_calls(source: str) -> None:
    banned = {"emit", "affected_scopes", "emit_restore_notice", "drain_scope"}
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert "change_events" not in module
            assert all(alias.name not in banned for alias in node.names)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            if isinstance(func, ast.Attribute):
                name = func.attr
            assert name not in banned


def test_owner_rules_and_separate_roots(tmp_path: Path) -> None:
    fleet, _store = _world(tmp_path)
    add_child = _change("add_scope", id="g_new", name="New", stratum_id="s2", parent_id="g_a")
    assert owner_scope(fleet, add_child) == "g_a"
    assert owner_scope(fleet, _change("remove_scope", scope_id="g_a1")) == "g_a"
    described = _change("describe", scope_id="g_a", description="purpose")
    assert owner_scope(fleet, described) == "g_root"
    edge = _change("add_edge", **{"from": "g_a", "to": "g_other"})
    assert owner_scope(fleet, edge) is None
    reparent = _change("reparent", scope_id="g_a1", new_parent_id="g_b")
    assert owner_scope(fleet, reparent) == "g_root"


def test_binding_flag_is_always_owned_above_the_proposer(tmp_path: Path) -> None:
    fleet, _store = _world(tmp_path)
    proposer = Actor("g_a1")
    cases = [
        _change("reparent", scope_id="g_a1", new_parent_id="g_b"),
        _change("reparent", scope_id="g_a", new_parent_id="g_other"),
        _change("remove_scope", scope_id="g_a"),
    ]
    for change in cases:
        _widens, binds = change_flags(fleet, change, proposer.scope_id)
        assert binds is True
        owner = owner_scope(fleet, change)
        assert owner != proposer.scope_id
        assert actor_qualifies(fleet, proposer, owner) is False


def test_scope_under_itself_applies_immediately(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    change = _change(
        "add_scope",
        id="g_a2",
        name="A2",
        stratum_id="s2",
        parent_id="g_a",
        references=["g_b"],
    )
    result = propose(fleet, store, change, proposer=Actor("g_a"))
    assert result.status == "applied"
    assert result.approver_position == "g_a"
    assert result.owner_scope_id == "g_a"
    assert result.notices == ()
    assert fleet.get_scope("g_a2") is not None
    assert fleet.inter_stratum_parent("g_a2").id == "g_a"
    assert [scope.id for scope in fleet.references_from("g_a2")] == ["g_b"]
    act = store.get_fleet_structure_act(result.act_id)
    assert act.change_type == "add_scope"
    assert act.proposer_position == "g_a"
    assert act.approver_position == "g_a"
    assert act.owner_scope_id == "g_a"
    assert act.widens_proposer_reach is False
    assert act.changes_proposer_binding is False
    assert act.before_topology["g_a2"] is None
    assert act.after_topology["g_a2"]["parent"] == "g_a"
    assert act.after_topology["g_a2"]["references"] == ["g_b"]
    assert list(Path(fleet._path).parent.glob("fleet.yaml.bak.*"))


def test_wider_reach_is_refused_at_proposal(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    change = _change(
        "add_scope",
        id="g_a2",
        name="A2",
        stratum_id="s2",
        parent_id="g_a1",
        references=["g_other"],
    )
    with pytest.raises(FleetChangeError, match="cannot reach") as raised:
        propose(fleet, store, change, proposer=Actor("g_a1"))
    assert raised.value.kind == "wider_reach"
    assert fleet.get_scope("g_a2") is None
    assert store.list_fleet_change_proposals() == []


def test_change_owned_above_stays_pending_until_an_ancestor_approves(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    change = _change("describe", scope_id="g_a1", description="what A1 is for")
    result = propose(fleet, store, change, proposer=Actor("g_a1"))
    assert result.status == "pending"
    assert result.owner_scope_id == "g_a"
    assert fleet.get_scope("g_a1").description is None
    with pytest.raises(FleetChangeError, match="cannot approve"):
        approve(fleet, store, result.proposal_id, approver=Actor("g_a1"))
    applied = approve(fleet, store, result.proposal_id, approver=Actor("g_a"))
    assert applied.status == "applied"
    assert applied.approver_position == "g_a"
    assert applied.notices == ()
    assert fleet.get_scope("g_a1").description == "what A1 is for"
    act = store.get_fleet_structure_act(applied.act_id)
    assert act.proposer_position == "g_a1"
    assert act.approver_position == "g_a"
    assert act.owner_scope_id == "g_a"
    assert act.before_topology == act.after_topology
    assert act.before_topology["g_a1"]["parent"] == "g_a"


def test_removing_ones_own_scope_is_owned_above(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    change = _change("remove_scope", scope_id="g_a1")
    result = propose(fleet, store, change, proposer=Actor("g_a1"))
    assert result.status == "pending"
    assert result.owner_scope_id == "g_a"
    with pytest.raises(FleetChangeError, match="cannot approve"):
        approve(fleet, store, result.proposal_id, approver=Actor("g_a1"))


def test_reparent_limit_and_binding_flag_round_trip(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    change = _change("reparent", scope_id="g_a1", new_parent_id="g_b")
    pending = propose(fleet, store, change, proposer=Actor("g_a1"))
    assert pending.status == "pending"
    assert pending.changes_proposer_binding is True
    assert FLAG_BINDS
    with pytest.raises(FleetChangeError, match="cannot approve"):
        approve(fleet, store, pending.proposal_id, approver=Actor("g_a1"))
    applied = approve(fleet, store, pending.proposal_id, approver=Actor("g_root"))
    assert applied.notices == (
        "g_a1 now inherits from g_root → g_b. Its own directives are not re-checked "
        "against the new inherited rules automatically (not built yet). Review them; "
        "the 1.17 inherited check applies to new writes only.",
    )
    assert fleet.inter_stratum_parent("g_a1").id == "g_b"
    act = store.get_fleet_structure_act(applied.act_id)
    assert act.changes_proposer_binding is True
    assert act.widens_proposer_reach is False
    assert act.before_topology["g_a1"]["parent"] == "g_a"
    assert act.after_topology["g_a1"]["parent"] == "g_b"
    assert act.proposer_position == "g_a1"
    assert act.approver_position == "g_root"
    assert act.owner_scope_id == "g_root"


def test_reference_edge_notices_and_widen_flag(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    added = _change("add_edge", **{"from": "g_a", "to": "g_other"})
    pending = propose(fleet, store, added, proposer=Actor("g_a"))
    assert pending.status == "pending"
    assert pending.widens_proposer_reach is True
    assert pending.owner_scope_id is None
    assert FLAG_WIDENS
    with pytest.raises(FleetChangeError, match="cannot approve"):
        approve(fleet, store, pending.proposal_id, approver=Actor("g_root"))
    applied = apply_as_operator(fleet, store, added)
    assert applied.notices == ()
    assert [scope.id for scope in fleet.references_from("g_a")] == ["g_b", "g_other"]
    act = store.get_fleet_structure_act(applied.act_id)
    assert act.approver_position == "operator"
    assert act.proposer_position == "operator"
    assert act.widens_proposer_reach is False
    assert act.owner_scope_id is None
    assert "g_other" in act.after_topology["g_a"]["references"]

    removed = _change("remove_edge", **{"from": "g_a", "to": "g_b"})
    assert limit_notices(fleet, removed) == [
        "g_a no longer reads g_b's publication. Items in g_a's memory attributed to g_b "
        "are not re-checked automatically (not built yet)."
    ]
    result = apply_as_operator(fleet, store, removed)
    assert result.notices[0] == (
        "g_a no longer reads g_b's publication. Items in g_a's memory attributed to g_b "
        "are not re-checked automatically (not built yet)."
    )


def test_remove_scope_refuses_children_and_edges_and_keeps_memory(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "g_a1.md").write_text("scope memory stays", encoding="utf-8")
    contribution = store.append_contribution(
        scope_id="g_a1",
        content="kept",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_a1",
            skill=None,
            session_id="s",
            ts="2026-10-07T00:00:00+00:00",
        ),
    )
    with pytest.raises(FleetChangeError, match="chain children") as children:
        apply_as_operator(fleet, store, _change("remove_scope", scope_id="g_a"))
    assert children.value.kind == "scope_has_children"
    with pytest.raises(FleetChangeError, match="still has edges") as edges:
        apply_as_operator(fleet, store, _change("remove_scope", scope_id="g_b"))
    assert edges.value.kind == "scope_has_edges"

    result = apply_as_operator(fleet, store, _change("remove_scope", scope_id="g_a1"))
    assert result.notices == (
        "g_a1's memory is kept. remove_edge already gave notice to any reader; "
        "attributed items are not re-checked automatically (not built yet).",
    )
    assert fleet.get_scope("g_a1") is None
    assert (summaries / "g_a1.md").read_text(encoding="utf-8") == "scope memory stays"
    assert store.get_contribution(contribution.id).content == "kept"
    act = store.get_fleet_structure_act(result.act_id)
    assert act.before_topology["g_a1"]["parent"] == "g_a"
    assert act.after_topology["g_a1"] is None


def test_reject_pending(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    pending = propose(
        fleet,
        store,
        _change("describe", scope_id="g_root", description="root purpose"),
        proposer=Actor("g_a"),
    )
    assert pending.owner_scope_id is None
    rejected = reject(fleet, store, pending.proposal_id, approver=Actor(None))
    assert rejected.status == "rejected"
    assert rejected.approver_position == "operator"
    assert store.list_fleet_structure_acts() == []
    with pytest.raises(FleetChangeError, match="not pending"):
        approve(fleet, store, pending.proposal_id, approver=Actor(None))


def test_payload_cannot_name_the_actor() -> None:
    with pytest.raises(FleetChangeError, match="cannot name who is acting") as raised:
        parse_change(
            "describe",
            {"scope_id": "g_a", "description": "x", "approver_scope_id": "g_root"},
        )
    assert raised.value.kind == "actor_not_in_payload"


def test_migration_0023_applies_on_a_populated_database(tmp_path: Path) -> None:
    without = tmp_path / "migrations"
    without.mkdir()
    for path in _default_migrations_dir().glob("*.sql"):
        if path.name != "0023_fleet_structure_act.sql":
            shutil.copy(path, without / path.name)
    db_path = str(tmp_path / "populated.db")
    run_migrations(db_path, migrations_dir=without)
    store = RecordStore(db_path)
    act = store.append_operator_act(
        act="publish",
        target_scope_id="g_root",
        kind="directive",
        content="keep this",
    )
    contribution = store.append_contribution(
        scope_id="g_root",
        content="kept contribution",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_root",
            skill=None,
            session_id="s",
            ts="2026-10-07T00:00:00+00:00",
        ),
    )
    store.close()

    applied = run_migrations(db_path)
    assert applied == ["0023_fleet_structure_act.sql"]
    store = RecordStore(db_path)
    assert store.get_operator_act(act.id).content == "keep this"
    assert store.get_contribution(contribution.id).content == "kept contribution"
    assert store.list_fleet_change_proposals(status=None) == []
    assert store.list_fleet_structure_acts() == []
    store.close()


def _parallel_apply_edge(fleet_path: str, db_path: str, target: str) -> None:
    """One process, one edge. Top-level so a spawned interpreter can import it."""
    from strata.fleet_changes import apply_as_operator, parse_change
    from strata.fleet_config import FleetConfig
    from strata.record_store import RecordStore

    fleet = FleetConfig.load(Path(fleet_path))
    store = RecordStore(db_path)
    try:
        apply_as_operator(
            fleet,
            store,
            parse_change("add_edge", {"from": "g_src", "to": target}),
        )
    finally:
        store.close()


def test_parallel_applies_keep_every_edge_and_act(tmp_path: Path) -> None:
    import multiprocessing

    n = 12
    scopes = "\n".join(f"  - id: g_t{i}\n    name: T{i}\n    stratum_id: s0" for i in range(n))
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(
        "strata:\n"
        "  - id: s0\n"
        "    name: Root\n"
        "    ordinal: 0\n"
        "scopes:\n"
        "  - id: g_src\n"
        "    name: Src\n"
        "    stratum_id: s0\n"
        f"{scopes}\n"
        "edges: []\n",
        encoding="utf-8",
    )
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_parallel_apply_edge, args=(str(fleet_path), db_path, f"g_t{i}"))
        for i in range(n)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
    codes = [proc.exitcode for proc in procs]
    assert codes == [0] * n, codes
    reloaded = FleetConfig.load(fleet_path)
    edge_count = sum(1 for edge in reloaded.edges if edge.from_ == "g_src")
    store = RecordStore(db_path)
    try:
        act_count = len(store.list_fleet_structure_acts())
    finally:
        store.close()
    print(f"N={n} edges={edge_count} acts={act_count}")
    assert edge_count == n
    assert act_count == n
    assert not (tmp_path / "fleet.yaml.tmp").exists()


def test_failed_act_restores_the_fleet_file(tmp_path: Path, monkeypatch) -> None:
    fleet, store = _world(tmp_path)
    before = fleet._path.read_bytes()

    def _boom(**_kwargs: object) -> None:
        raise RuntimeError("act store down")

    monkeypatch.setattr(fleet_changes, "record_fleet_structure_act", _boom)
    with pytest.raises(RuntimeError, match="act store down"):
        apply_as_operator(fleet, store, _change("describe", scope_id="g_a", description="nope"))
    assert fleet._path.read_bytes() == before
    assert fleet.get_scope("g_a").description is None
    assert not (tmp_path / "fleet.yaml.tmp").exists()


def test_proposer_cannot_approve_or_reject_their_own_proposal(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    proposal = store.insert_fleet_change_proposal(
        change_type="describe",
        payload={"scope_id": "g_a1", "description": "from its parent"},
        proposer_position="g_a",
        owner_scope_id="g_a",
        widens_proposer_reach=False,
        changes_proposer_binding=False,
    )
    with pytest.raises(FleetChangeError, match="cannot approve") as approve_error:
        approve(fleet, store, proposal.id, approver=Actor("g_a"))
    assert approve_error.value.kind == "approver_is_proposer"
    with pytest.raises(FleetChangeError, match="cannot reject") as reject_error:
        reject(fleet, store, proposal.id, approver=Actor("g_a"))
    assert reject_error.value.kind == "approver_is_proposer"
    assert store.get_fleet_change_proposal(proposal.id).status == "pending"
    decided = approve(fleet, store, proposal.id, approver=Actor(None))
    assert decided.status == "applied"


def test_reject_recomputes_the_owner(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    proposal = store.insert_fleet_change_proposal(
        change_type="describe",
        payload={"scope_id": "g_a1", "description": "still pending"},
        proposer_position="g_a1",
        owner_scope_id="g_b",
        widens_proposer_reach=False,
        changes_proposer_binding=False,
    )
    with pytest.raises(FleetChangeError, match="owned by g_a") as stale:
        reject(fleet, store, proposal.id, approver=Actor("g_b"))
    assert stale.value.kind == "not_authorized"
    rejected = reject(fleet, store, proposal.id, approver=Actor("g_a"))
    assert rejected.status == "rejected"
    assert rejected.owner_scope_id == "g_a"
