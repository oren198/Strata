"""``strata fleet`` applies and reviews structure changes as the operator."""

from __future__ import annotations

from pathlib import Path

from strata.__main__ import main
from strata.fleet_changes import FLAG_BINDS, Actor, parse_change, propose
from strata.fleet_config import FleetConfig
from tests.test_fleet_changes import _world


def test_cli_pending_flags_and_apply(tmp_path: Path, capsys) -> None:
    fleet, store = _world(tmp_path)
    pending = propose(
        fleet,
        store,
        parse_change("reparent", {"scope_id": "g_a1", "new_parent_id": "g_b"}),
        proposer=Actor("g_a1"),
    )
    store.close()
    db = str(tmp_path / "strata.db")
    fleet_path = str(fleet._path)
    prefix = ["fleet", "--db", db, "--fleet", fleet_path]

    assert main([*prefix, "pending"]) == 0
    pending_out = capsys.readouterr().out
    assert pending.proposal_id in pending_out
    assert FLAG_BINDS in pending_out
    assert "proposer g_a1" in pending_out
    assert "owner g_root" in pending_out

    assert main([*prefix, "apply", pending.proposal_id]) == 0
    applied_out = capsys.readouterr().out
    assert "applied" in applied_out
    assert "not built yet" not in applied_out
    assert FleetConfig.load(fleet._path).inter_stratum_parent("g_a1").id == "g_b"

    assert main([*prefix, "remove-edge", "g_a", "g_b"]) == 0
    edge_out = capsys.readouterr().out
    assert "not built yet" not in edge_out
    assert FleetConfig.load(fleet._path).references_from("g_a") == []

    assert main([*prefix, "remove-scope", "g_a1"]) == 0
    remove_out = capsys.readouterr().out
    assert "not built yet" not in remove_out
    assert FleetConfig.load(fleet._path).get_scope("g_a1") is None
