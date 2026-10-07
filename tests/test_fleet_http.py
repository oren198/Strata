"""HTTP and Console fleet changes are operator-only (#247).

A request body or query string cannot propose or approve as a scope.
The routes do not call the change-event machinery.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

import strata.app as app_module
from strata.app import create_app
from strata.fleet_changes import Actor, parse_change, propose
from strata.fleet_config import FleetConfig
from strata.record_store import RecordStore
from strata.settings import Settings

_FLEET = """\
strata:
  - id: s0
    name: Root
    ordinal: 0
  - id: s1
    name: Mid
    ordinal: 1
scopes:
  - id: g_root
    name: Root
    stratum_id: s0
  - id: g_a
    name: A
    stratum_id: s1
  - id: g_other
    name: Other
    stratum_id: s0
edges:
  - from: g_a
    to: g_root
    kind: chain
"""

_REPARENT_NOTICE = (
    "g_a now inherits from g_other. Its own directives are not re-checked "
    "against the new inherited rules automatically (not built yet). Review them; "
    "the 1.17 inherited check applies to new writes only."
)

_BANNED = {"emit", "affected_scopes", "emit_restore_notice", "drain_scope"}


def _client(tmp_path: Path) -> tuple[TestClient, Path, Path]:
    db_path = tmp_path / "t.db"
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(_FLEET, encoding="utf-8")
    settings = Settings(
        db_path=str(db_path),
        summaries_dir=str(tmp_path / "summaries"),
        fleet_yaml_path=str(fleet_path),
        anthropic_api_key="test-key",
    )
    client = TestClient(create_app(settings=settings))
    return client, db_path, fleet_path


def _pending_reparent(db_path: Path, fleet_path: Path) -> str:
    fleet = FleetConfig.load(fleet_path)
    store = RecordStore(str(db_path))
    try:
        result = propose(
            fleet,
            store,
            parse_change(
                "reparent",
                {"scope_id": "g_a", "new_parent_id": "g_other"},
            ),
            proposer=Actor("g_a"),
        )
    finally:
        store.close()
    assert result.status == "pending"
    assert result.proposal_id is not None
    return result.proposal_id


def test_http_cannot_propose_or_approve_as_a_scope(tmp_path: Path) -> None:
    client, db_path, fleet_path = _client(tmp_path)
    with client:
        proposal_id = _pending_reparent(db_path, fleet_path)
        proposed = client.post(
            "/fleet/changes",
            json={
                "change_type": "describe",
                "payload": {"scope_id": "g_a", "description": "no"},
                "approver_scope_id": "g_root",
                "proposer_position": "g_root",
            },
        )
        assert proposed.status_code == 405

        listed = client.get("/fleet/changes")
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()["changes"]] == [proposal_id]
        row = listed.json()["changes"][0]
        assert row["proposer_position"] == "g_a"
        assert "changes what binds the proposer" in row["flags"]

        named = client.post(
            f"/fleet/changes/{proposal_id}/apply",
            json={"approver_scope_id": "g_root"},
        )
        assert named.status_code == 400
        assert "cannot propose or approve as a scope" in named.json()["detail"]

        queried = client.post(
            f"/fleet/changes/{proposal_id}/apply?as_scope=g_root",
            json={},
        )
        assert queried.status_code == 400

        still = client.get("/fleet/changes")
        assert [row["id"] for row in still.json()["changes"]] == [proposal_id]

        applied = client.post(f"/fleet/changes/{proposal_id}/apply", json={})
        assert applied.status_code == 200
        body = applied.json()
        assert body["approver_position"] == "operator"
        assert body["proposer_position"] == "g_a"
        assert body["notices"] == [_REPARENT_NOTICE]
        assert body["changes_proposer_binding"] is True

    store = RecordStore(str(db_path))
    try:
        acts = store.list_fleet_structure_acts()
        assert len(acts) == 1
        assert acts[0].approver_position == "operator"
        assert acts[0].proposer_position == "g_a"
        assert acts[0].change_type == "reparent"
    finally:
        store.close()
    fleet = FleetConfig.load(fleet_path)
    assert fleet.inter_stratum_parent("g_a").id == "g_other"


def test_http_reject_is_operator_only(tmp_path: Path) -> None:
    client, db_path, fleet_path = _client(tmp_path)
    with client:
        proposal_id = _pending_reparent(db_path, fleet_path)
        named = client.post(
            f"/fleet/changes/{proposal_id}/reject",
            json={"approver": "g_root"},
        )
        assert named.status_code == 400
        rejected = client.post(f"/fleet/changes/{proposal_id}/reject", json={})
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"
        assert rejected.json()["approver_position"] == "operator"
        assert rejected.json()["act_id"] is None
        assert client.get("/fleet/changes").json()["changes"] == []

    store = RecordStore(str(db_path))
    try:
        assert store.list_fleet_structure_acts() == []
    finally:
        store.close()
    fleet = FleetConfig.load(fleet_path)
    assert fleet.inter_stratum_parent("g_a").id == "g_root"


def test_fleet_http_and_console_never_emit_change_events() -> None:
    """The HTTP routes, MCP fleet tools, and Console view do not emit."""
    source = Path(app_module.__file__).read_text(encoding="utf-8")
    start = source.index("# Fleet structure changes (#247)")
    start = source.rfind("\n", 0, start) + 1
    end = source.index("# GET /staleness", start)
    _assert_no_change_event_calls(textwrap.dedent(source[start:end]))

    import strata.mcp.server as mcp_server

    for name in (
        "strata_fleet_propose",
        "strata_fleet_pending",
        "strata_fleet_approve",
        "strata_fleet_reject",
        "_fleet_result_payload",
        "_run_fleet_change",
    ):
        _assert_no_change_event_calls(textwrap.dedent(inspect_source(mcp_server, name)))

    ui = Path(app_module.__file__).parent / "_ui"
    for name in ("fleet-changes.jsx", "store.js"):
        text = (ui / name).read_text(encoding="utf-8")
        for banned in ("emit(", "affected_scopes", "emit_restore_notice", "drain_scope"):
            assert banned not in text


def inspect_source(module, name: str) -> str:
    import inspect

    return inspect.getsource(getattr(module, name))


def _assert_no_change_event_calls(source: str) -> None:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert "change_events" not in module
            assert all(alias.name not in _BANNED for alias in node.names)
        if isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else None
            if isinstance(func, ast.Attribute):
                called = func.attr
            assert called not in _BANNED
