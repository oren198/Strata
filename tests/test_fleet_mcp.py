"""MCP fleet tools take their position from the session binding only."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

import strata.mcp.server as mcp_server
from strata.fleet_changes import Actor, parse_change, propose
from tests.test_fleet_changes import _world


def _bind(monkeypatch: pytest.MonkeyPatch, fleet, store, scope_id: str) -> None:
    monkeypatch.setattr(mcp_server, "_AGENT_SCOPE", scope_id)
    monkeypatch.setattr(mcp_server, "_UNRESOLVED", False)
    monkeypatch.setattr(mcp_server, "_record_store", store)
    monkeypatch.setattr(mcp_server, "_load_fleet", lambda: fleet)
    monkeypatch.setattr(mcp_server, "_session_store", None)


def test_fleet_tool_signatures_have_no_actor_argument() -> None:
    for name in (
        "strata_fleet_propose",
        "strata_fleet_pending",
        "strata_fleet_approve",
        "strata_fleet_reject",
    ):
        params = set(inspect.signature(getattr(mcp_server, name)).parameters)
        assert params.isdisjoint(
            {
                "scope_id",
                "proposer",
                "approver",
                "proposer_position",
                "approver_position",
                "as_scope",
            }
        )


async def test_propose_records_the_bound_scope_and_refuses_an_actor_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet, store = _world(tmp_path)
    _bind(monkeypatch, fleet, store, "g_a1")
    with pytest.raises(RuntimeError, match="cannot name who is acting"):
        await mcp_server.strata_fleet_propose(
            "describe",
            json.dumps(
                {
                    "scope_id": "g_a1",
                    "description": "nope",
                    "proposer_position": "g_root",
                }
            ),
        )
    result = await mcp_server.strata_fleet_propose(
        "describe",
        json.dumps({"scope_id": "g_a1", "description": "from the binding"}),
    )
    assert result["status"] == "pending"
    assert result["proposer_position"] == "g_a1"
    assert result["owner_scope_id"] == "g_a"
    assert fleet.get_scope("g_a1").description is None


async def test_approve_uses_the_binding_not_a_request_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet, store = _world(tmp_path)
    pending = propose(
        fleet,
        store,
        parse_change("describe", {"scope_id": "g_a1", "description": "owned above"}),
        proposer=Actor("g_a1"),
    )
    _bind(monkeypatch, fleet, store, "g_a1")
    with pytest.raises(RuntimeError, match="cannot approve"):
        await mcp_server.strata_fleet_approve(pending.proposal_id)
    _bind(monkeypatch, fleet, store, "g_a")
    listed = await mcp_server.strata_fleet_pending()
    assert [row["id"] for row in listed["changes"]] == [pending.proposal_id]
    applied = await mcp_server.strata_fleet_approve(pending.proposal_id)
    assert applied["status"] == "applied"
    assert applied["approver_position"] == "g_a"
    assert applied["proposer_position"] == "g_a1"
    assert fleet.get_scope("g_a1").description == "owned above"
