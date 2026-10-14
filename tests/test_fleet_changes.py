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

# re-pinned for #244 step 2: the exception rule joined the ancestor block of the user message
_JUDGE_BUILDER_SHA256 = "ee9320c0685518f5f1794633c5100bad06bb2b5562a0cb176150078b84e5c7cb"


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


def test_fleet_change_code_calls_only_emit_structure_change() -> None:
    """Source scan: fleet_changes calls only emit_structure_change.

    ``record_fleet_structure_act`` still does not touch the change-event
    machinery. HTTP, MCP, and the Console stay on the never-emit scan.
    """
    source = Path(fleet_changes.__file__).read_text(encoding="utf-8")
    names = _change_event_call_names(source)
    assert names, "fleet_changes does not call emit_structure_change"
    assert set(names) == {"emit_structure_change"}
    _assert_no_change_event_calls(inspect.getsource(operator.record_fleet_structure_act))


def _change_event_call_names(source: str) -> list[str]:
    watched = {
        "emit",
        "emit_structure_change",
        "affected_scopes",
        "emit_restore_notice",
        "drain_scope",
    }
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert "change_events" not in module
            assert all(alias.name not in watched for alias in node.names)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            if isinstance(func, ast.Attribute):
                name = func.attr
            if name in watched:
                found.append(name)
    return found


def _assert_no_change_event_calls(source: str) -> None:
    assert _change_event_call_names(source) == []


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
    assert applied.notices == ()
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
    result = apply_as_operator(fleet, store, removed)
    assert result.notices == ()
    assert [scope.id for scope in fleet.references_from("g_a")] == ["g_other"]


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
    assert result.notices == ()
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


_FLEET_PUBLISHERS = {
    "app.py",
    "fleet_changes.py",
    "fleet_config.py",
    "fleet_export.py",
    "__main__.py",
}
_FLEET_DEST_NAMES = {"fleet_yaml", "fleet_path"}
# The only fleet.yaml write allowed outside replace_fleet_bytes: an exclusive
# create of a file that does not exist yet. Callers are the register seed and
# the env-driven start seed. The os.open(O_CREAT|O_EXCL) that implements it
# lives only in create_fleet_file.
_EXCLUSIVE_CREATE_CALLERS = {
    ("__main__.py", "cmd_register"),
    ("__main__.py", "cmd_start"),
}


def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_os_replace(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "replace"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    )


def _is_os_open(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "open"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    )


def _os_open_flag_names(node: ast.Call) -> set[str]:
    if len(node.args) < 2:
        return set()
    return {child.attr for child in ast.walk(node.args[1]) if isinstance(child, ast.Attribute)}


def _is_byte_publish(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in {"write_text", "write_bytes"}:
        return True
    if _is_os_replace(node):
        return True
    if isinstance(func, ast.Attribute) and func.attr in {"copy", "copyfile", "copy2"}:
        return isinstance(func.value, ast.Name) and func.value.id == "shutil"
    return isinstance(func, ast.Name) and func.id in {"copy", "copyfile", "copy2"}


def _name_ids(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


class _PublishVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.calls: list[tuple[str | None, ast.Call]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append((self.stack[-1] if self.stack else None, node))
        self.generic_visit(node)


def test_fleet_yaml_writes_go_through_replace_fleet_bytes() -> None:
    """Fleet.yaml bytes are published only by replace_fleet_bytes.

    The one exception is create_fleet_file: an O_CREAT|O_EXCL create of a
    file that does not exist yet, and it takes no flock. Only cmd_register
    and cmd_start may call it. The os.open that implements that create lives
    only inside create_fleet_file. Every replacement of an existing file
    still goes through replace_fleet_bytes.
    """
    import strata.fleet_config as fleet_config

    root = Path(fleet_config.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(root))
        if "fleet.yaml.tmp" in source:
            offenders.append(f"{rel}: contains fleet.yaml.tmp")
        if path.name in _FLEET_PUBLISHERS and '+ ".tmp"' in source:
            offenders.append(f"{rel}: fixed .tmp suffix")
        visitor = _PublishVisitor()
        visitor.visit(ast.parse(source))
        for func_name, call in visitor.calls:
            callee = _callee_name(call)
            if callee == "replace_fleet_bytes":
                continue
            if callee == "create_fleet_file":
                if (path.name, func_name) not in _EXCLUSIVE_CREATE_CALLERS:
                    offenders.append(
                        f"{rel}:{call.lineno}: create_fleet_file is only the "
                        "missing-file seed in cmd_register and cmd_start "
                        "(the only exception to replace_fleet_bytes)"
                    )
                continue
            if (
                path.name in _FLEET_PUBLISHERS
                and _is_os_replace(call)
                and func_name != "replace_fleet_bytes"
            ):
                offenders.append(f"{rel}:{call.lineno}: os.replace outside replace_fleet_bytes")
            if path.name in _FLEET_PUBLISHERS and _is_os_open(call):
                flags = _os_open_flag_names(call)
                exclusive = "O_CREAT" in flags and "O_EXCL" in flags
                if (
                    exclusive
                    and func_name == "create_fleet_file"
                    and path.name == "fleet_config.py"
                ):
                    continue
                if exclusive or (_name_ids(call) & _FLEET_DEST_NAMES):
                    offenders.append(
                        f"{rel}:{call.lineno}: os.open of a fleet file outside "
                        "create_fleet_file (the only exception to replace_fleet_bytes)"
                    )
            if _is_byte_publish(call) and (_name_ids(call) & _FLEET_DEST_NAMES):
                offenders.append(
                    f"{rel}:{call.lineno}: fleet file write outside replace_fleet_bytes"
                )
    assert offenders == []


def _console_settings(fleet_path: str, db_path: str):
    from strata.settings import Settings

    return Settings(
        db_path=db_path,
        summaries_dir=str(Path(fleet_path).parent / "summaries"),
        fleet_yaml_path=fleet_path,
        manager_model="claude-haiku-4-5",
        anthropic_api_key="test-key",
    )


def _apply_hold_lock(fleet_path: str, db_path: str, held_path: str, release_path: str) -> None:
    """Apply one describe while holding the fleet lock at the test hook."""
    import time

    import strata.fleet_changes as fleet_changes
    from strata.fleet_changes import apply_as_operator, parse_change
    from strata.fleet_config import FleetConfig
    from strata.record_store import RecordStore

    def _hook() -> None:
        Path(held_path).write_text("held", encoding="utf-8")
        deadline = time.monotonic() + 30
        while not Path(release_path).exists():
            if time.monotonic() > deadline:
                raise TimeoutError("lock release did not arrive")
            time.sleep(0.01)

    fleet_changes._apply_lock_hook = _hook
    fleet = FleetConfig.load(Path(fleet_path))
    store = RecordStore(db_path)
    try:
        apply_as_operator(
            fleet,
            store,
            parse_change("describe", {"scope_id": "g_a", "description": "held under the lock"}),
        )
    finally:
        store.close()


def _put_once(
    fleet_path: str,
    db_path: str,
    yaml_text: str,
    etag: str,
    requesting_path: str,
    response_path: str,
) -> None:
    """One PUT /fleet. Writes *requesting_path* immediately before the call."""
    from fastapi.testclient import TestClient

    from strata.app import create_app

    application = create_app(settings=_console_settings(fleet_path, db_path))
    try:
        with TestClient(application) as client:
            Path(requesting_path).write_text("requesting", encoding="utf-8")
            resp = client.put("/fleet", json={"yaml": yaml_text, "etag": etag})
            error = ""
            if resp.status_code != 200:
                error = resp.json()["detail"]["error"]
            Path(response_path).write_text(
                f"status={resp.status_code} error={error}",
                encoding="utf-8",
            )
    except Exception as exc:
        Path(response_path).write_text(
            f"error={type(exc).__name__}: {exc}",
            encoding="utf-8",
        )
        raise


def _put_document(n: int) -> str:
    scopes = "\n".join(
        ["  - id: g_src\n    name: Src\n    stratum_id: s0"]
        + [f"  - id: g_t{i}\n    name: T{i}\n    stratum_id: s0" for i in range(n)]
        + ["  - id: g_from_put\n    name: FromPut\n    stratum_id: s0"]
    )
    return f"strata:\n  - id: s0\n    name: Root\n    ordinal: 0\nscopes:\n{scopes}\nedges: []\n"


_PUT_BODY = (
    "strata:\n"
    "  - id: s0\n"
    "    name: Root\n"
    "    ordinal: 0\n"
    "scopes:\n"
    "  - id: g_from_put\n"
    "    name: FromPut\n"
    "    stratum_id: s0\n"
    "edges: []\n"
)


def _edge_fleet(tmp_path: Path, n: int = 12) -> tuple[Path, str]:
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
    return fleet_path, db_path


def _run_parallel_edges(fleet_path: Path, db_path: str, n: int) -> None:
    import multiprocessing

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


def _edge_and_act_counts(fleet_path: Path, db_path: str) -> tuple[int, int]:
    reloaded = FleetConfig.load(fleet_path)
    edges = sum(1 for edge in reloaded.edges if edge.from_ == "g_src")
    store = RecordStore(db_path)
    try:
        acts = len(store.list_fleet_structure_acts())
    finally:
        store.close()
    return edges, acts


def test_put_blocks_while_an_apply_holds_the_lock_then_gets_409(tmp_path: Path) -> None:
    """The PUT is fired while an apply still holds the fleet lock.

    The apply writes the file, then the test hook waits. The PUT process
    announces that it is inside ``client.put`` and does not return until the
    hook is released. The tag was taken before the apply, so the compare
    inside the lock answers 409 and the apply's bytes stay.
    """
    import hashlib
    import multiprocessing
    import time

    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(_FLEET, encoding="utf-8")
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    etag = hashlib.sha256(fleet_path.read_bytes()).hexdigest()
    held_path = tmp_path / "apply-held"
    release_path = tmp_path / "apply-release"
    requesting_path = tmp_path / "put-requesting"
    response_path = tmp_path / "put-response"
    ctx = multiprocessing.get_context("spawn")
    apply_proc = ctx.Process(
        target=_apply_hold_lock,
        args=(str(fleet_path), db_path, str(held_path), str(release_path)),
    )
    apply_proc.start()
    deadline = time.monotonic() + 30
    while not held_path.exists():
        assert apply_proc.is_alive(), "apply exited before it held the lock"
        assert time.monotonic() < deadline, "apply never held the lock"
        time.sleep(0.01)
    post_apply = fleet_path.read_bytes()
    put_proc = ctx.Process(
        target=_put_once,
        args=(
            str(fleet_path),
            db_path,
            _PUT_BODY,
            etag,
            str(requesting_path),
            str(response_path),
        ),
    )
    put_proc.start()
    while not requesting_path.exists():
        detail = response_path.read_text(encoding="utf-8") if response_path.exists() else "put died"
        assert put_proc.is_alive(), detail
        assert time.monotonic() < deadline, "PUT never reached client.put"
        time.sleep(0.01)
    time.sleep(0.5)
    assert not response_path.exists(), response_path.read_text(encoding="utf-8")
    release_path.write_text("go", encoding="utf-8")
    apply_proc.join(60)
    put_proc.join(60)
    assert apply_proc.exitcode == 0
    assert put_proc.exitcode == 0, response_path.read_text(encoding="utf-8")
    outcome = response_path.read_text(encoding="utf-8")
    print(f"overlap blocked_until_release=True {outcome} bytes_unchanged=True")
    assert outcome == "status=409 error=fleet_changed"
    assert fleet_path.read_bytes() == post_apply
    assert b"held under the lock" in post_apply
    assert b"g_from_put" not in fleet_path.read_bytes()


def test_stale_etag_fetched_before_applies_changes_nothing(tmp_path: Path) -> None:
    """A tag taken before the applies is stale, and the PUT must not land.

    The tag is hashed from the file before any apply starts. After all 12
    applies, the PUT carries that same tag. The compare inside the lock
    re-reads the post-apply bytes, returns 409, and leaves them in place:
    12 acts, 12 edges, nothing superseded.
    """
    import hashlib

    from fastapi.testclient import TestClient

    from strata.app import create_app

    n = 12
    fleet_path, db_path = _edge_fleet(tmp_path, n)
    etag = hashlib.sha256(fleet_path.read_bytes()).hexdigest()
    _run_parallel_edges(fleet_path, db_path, n)
    post_apply = fleet_path.read_bytes()
    edges, acts = _edge_and_act_counts(fleet_path, db_path)
    superseded = n - edges
    with TestClient(create_app(settings=_console_settings(str(fleet_path), db_path))) as client:
        resp = client.put("/fleet", json={"yaml": _PUT_BODY, "etag": etag})
    error = resp.json()["detail"]["error"]
    print(
        f"stale_etag status={resp.status_code} error={error} "
        f"acts={acts} edges={edges} superseded={superseded}"
    )
    assert resp.status_code == 409
    assert error == "fleet_changed"
    assert acts == n
    assert edges == n
    assert superseded == 0
    assert fleet_path.read_bytes() == post_apply
    assert b"g_from_put" not in fleet_path.read_bytes()


def test_fresh_etag_after_applies_saves(tmp_path: Path) -> None:
    """A tag read after the applies matches the file, so the PUT lands."""
    from fastapi.testclient import TestClient

    from strata.app import create_app

    n = 12
    fleet_path, db_path = _edge_fleet(tmp_path, n)
    _run_parallel_edges(fleet_path, db_path, n)
    with TestClient(create_app(settings=_console_settings(str(fleet_path), db_path))) as client:
        etag = client.get("/fleet").json()["etag"]
        resp = client.put("/fleet", json={"yaml": _put_document(n), "etag": etag})
    print(f"fresh_etag status={resp.status_code} saved={resp.json().get('saved')}")
    assert resp.status_code == 200
    assert resp.json()["saved"] is True
    assert fleet_path.read_bytes() == _put_document(n).encode("utf-8")
    assert b"g_from_put" in fleet_path.read_bytes()


def test_fleet_yaml_keeps_its_file_mode_across_the_replace(tmp_path: Path) -> None:
    """The rename must not replace a 0o644 fleet.yaml with the temp file's 0o600."""
    import stat

    fleet, store = _world(tmp_path)
    assert fleet._path is not None
    fleet._path.chmod(0o640)
    apply_as_operator(fleet, store, _change("describe", scope_id="g_a", description="mode stays"))
    assert stat.S_IMODE(fleet._path.stat().st_mode) == 0o640
    assert fleet.get_scope("g_a").description == "mode stays"


def test_missing_fcntl_refuses_the_fleet_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No flock means no write. The file stays as it was."""
    import strata.fleet_config as fleet_config
    from strata.fleet_config import FLEET_FILE_LOCK_UNAVAILABLE, FleetFileLockUnavailable

    monkeypatch.setattr(fleet_config, "fcntl", None)
    fleet, store = _world(tmp_path)
    before = fleet._path.read_bytes()
    with pytest.raises(FleetFileLockUnavailable) as raised:
        apply_as_operator(fleet, store, _change("describe", scope_id="g_a", description="nope"))
    assert str(raised.value) == FLEET_FILE_LOCK_UNAVAILABLE
    assert raised.value.kind == "fleet_lock_unavailable"
    assert fleet._path.read_bytes() == before
    assert fleet.get_scope("g_a").description is None


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
