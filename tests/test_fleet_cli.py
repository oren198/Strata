"""``strata fleet`` applies and reviews structure changes as the operator."""

from __future__ import annotations

from pathlib import Path

from strata.__main__ import main
from strata.fleet_changes import FLAG_BINDS, Actor, parse_change, propose
from strata.fleet_config import FleetConfig
from tests.test_fleet_changes import _world


def test_cli_pending_flags_and_stated_limits(tmp_path: Path, capsys) -> None:
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
    assert "g_a1 now inherits from g_root → g_b" in applied_out
    assert "not re-checked against the new inherited rules automatically (not built yet)" in (
        applied_out
    )
    assert FleetConfig.load(fleet._path).inter_stratum_parent("g_a1").id == "g_b"

    assert main([*prefix, "remove-edge", "g_a", "g_b"]) == 0
    edge_out = capsys.readouterr().out
    assert (
        "g_a no longer reads g_b's publication. Items in g_a's memory attributed to g_b "
        "are not re-checked automatically (not built yet)."
    ) in edge_out

    assert main([*prefix, "remove-scope", "g_a1"]) == 0
    remove_out = capsys.readouterr().out
    assert "remove_edge already gave notice" in remove_out
    assert "g_a1's memory is kept." in remove_out
    assert "not re-checked automatically (not built yet)" in remove_out
    assert FleetConfig.load(fleet._path).get_scope("g_a1") is None
