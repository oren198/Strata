"""ADR 0014 D8, peripheral half.

The core's ``emit_structure_change`` has not landed. These tests cover the
guard path (the operation is absent: stated-limit line kept, apply still
succeeds) and a stub standing in for the core (called once after the act is
committed, under the fleet lock; a raising emit does not roll the apply
back; reemit is idempotent; doctor lists acts with no events).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import strata.change_events as change_events
from strata.__main__ import cmd_doctor, main
from strata.app import create_app
from strata.fleet_changes import (
    Actor,
    apply_as_operator,
    approve,
    parse_change,
    propose,
)
from strata.fleet_config import FleetConfig, fleet_file_lock
from strata.record_store import ContributorRef, RecordStore
from strata.settings import Settings
from strata.summary_store import Directive, ScopeSummary, SummaryStore
from tests.test_fleet_changes import _world

_DEEP = """\
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
  - id: s3
    name: Deep
    ordinal: 3
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
  - id: g_a1a
    name: A1a
    stratum_id: s3
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
  - from: g_a1a
    to: g_a1
    kind: chain
  - from: g_a
    to: g_b
    kind: reference
"""

_CONFLICT = (
    "Own directive c_own conflicts with newly inherited c_inh (g_root): "
    "opposite. Where they conflict, the inherited directive governs. "
    "c_own remains in g_a's memory until a session bound to g_a revises or retires it."
)


def _deep(tmp_path: Path) -> tuple[FleetConfig, RecordStore]:
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(_DEEP, encoding="utf-8")
    from strata.migrator import run_migrations

    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    return FleetConfig.load(fleet_path), RecordStore(db_path)


def _notice(store: RecordStore, act_id: str, *, line: str = _CONFLICT) -> None:
    """A change event whose payload carries *act_id*. Kind stays in today's CHECK."""
    store.append_change_notice(
        scope_id="g_a",
        content=line,
        contributor=ContributorRef(
            scope_id="g_a",
            skill=None,
            session_id="s",
            ts="2026-10-11T00:00:00+00:00",
        ),
        change_id=f"chg_{act_id}",
        source_scope_id="g_root",
        item_id="c_own",
        kind="published",
        after=json.dumps({"act_id": act_id, "conflicts": [line]}),
    )


def _doctor_line(capsys: pytest.CaptureFixture[str]) -> str:
    cmd_doctor(argparse.Namespace())
    captured = capsys.readouterr()
    text = captured.out + captured.err
    lines = [line for line in text.splitlines() if "Structure emission" in line]
    assert len(lines) == 1, captured.out + captured.err
    return lines[0]


def test_reparent_of_two_descendant_levels_records_chain_and_descendants(tmp_path: Path) -> None:
    fleet, store = _deep(tmp_path)
    result = apply_as_operator(
        fleet,
        store,
        parse_change("reparent", {"scope_id": "g_a", "new_parent_id": "g_other"}),
    )
    act = store.get_fleet_structure_act(result.act_id)
    before = act.before_topology["g_a"]
    after = act.after_topology["g_a"]
    assert before["parent"] == "g_root"
    assert after["parent"] == "g_other"
    assert before["references"] == ["g_b"]
    assert after["references"] == ["g_b"]
    assert before["chain"] == ["g_root"]
    assert after["chain"] == ["g_other"]
    assert before["descendants"] == ["g_a1", "g_a1a"]
    assert after["descendants"] == ["g_a1", "g_a1a"]
    assert "g_a" not in before["chain"]
    assert "g_a" not in before["descendants"]
    store.close()


def test_non_reparent_topology_omits_chain_and_descendants(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    described = apply_as_operator(
        fleet,
        store,
        parse_change("describe", {"scope_id": "g_a", "description": "purpose"}),
    )
    removed = apply_as_operator(
        fleet,
        store,
        parse_change("remove_edge", {"from": "g_a", "to": "g_b"}),
    )
    describe_act = store.get_fleet_structure_act(described.act_id)
    edge_act = store.get_fleet_structure_act(removed.act_id)
    assert set(describe_act.before_topology["g_a"]) == {"parent", "references"}
    assert set(describe_act.after_topology["g_a"]) == {"parent", "references"}
    for snapshot in edge_act.before_topology.values():
        assert set(snapshot) == {"parent", "references"}
    assert edge_act.after_topology["g_a"]["references"] == []
    store.close()


def test_apply_without_core_keeps_stated_limit_and_succeeds(tmp_path: Path) -> None:
    assert not hasattr(change_events, "emit_structure_change")
    fleet, store = _world(tmp_path)
    result = apply_as_operator(
        fleet,
        store,
        parse_change("reparent", {"scope_id": "g_a1", "new_parent_id": "g_b"}),
    )
    assert result.status == "applied"
    assert result.notices == (
        "g_a1 now inherits from g_root → g_b. Its own directives are not re-checked "
        "against the new inherited rules automatically (not built yet). Review them; "
        "the 1.17 inherited check applies to new writes only.",
    )
    assert fleet.inter_stratum_parent("g_a1").id == "g_b"
    assert store.get_fleet_structure_act(result.act_id).after_topology["g_a1"]["parent"] == "g_b"
    store.close()


def test_apply_with_core_emits_once_after_commit_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet, store = _world(tmp_path)
    seen: dict = {}

    def emit(act_id: str) -> dict:
        seen["calls"] = seen.get("calls", []) + [act_id]
        seen["committed"] = store.get_fleet_structure_act(act_id).id
        seen["depth"] = fleet_file_lock(fleet._path)._depth
        seen["file_parent"] = FleetConfig.load(fleet._path).inter_stratum_parent("g_a1").id
        return {"emitted": act_id}

    monkeypatch.setattr(change_events, "emit_structure_change", emit, raising=False)
    result = apply_as_operator(
        fleet,
        store,
        parse_change("reparent", {"scope_id": "g_a1", "new_parent_id": "g_b"}),
    )
    assert seen["calls"] == [result.act_id]
    assert seen["committed"] == result.act_id
    assert seen["depth"] == 1
    assert seen["file_parent"] == "g_b"
    assert fleet_file_lock(fleet._path)._depth == 0
    assert result.notices == ()
    assert result.status == "applied"
    store.close()


def test_raising_emit_does_not_roll_back_the_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet, store = _world(tmp_path)
    pending = propose(
        fleet,
        store,
        parse_change("reparent", {"scope_id": "g_a1", "new_parent_id": "g_b"}),
        proposer=Actor("g_a1"),
    )

    def emit(act_id: str) -> None:
        raise RuntimeError(f"emission failed for {act_id}")

    monkeypatch.setattr(change_events, "emit_structure_change", emit, raising=False)
    applied = approve(fleet, store, pending.proposal_id, approver=Actor("g_root"))
    assert applied.status == "applied"
    assert applied.notices == ()
    assert fleet.inter_stratum_parent("g_a1").id == "g_b"
    assert FleetConfig.load(fleet._path).inter_stratum_parent("g_a1").id == "g_b"
    act = store.get_fleet_structure_act(applied.act_id)
    assert act.after_topology["g_a1"]["parent"] == "g_b"
    assert store.get_fleet_change_proposal(pending.proposal_id).status == "applied"
    store.close()


def test_reemit_calls_emit_and_prints_what_was_emitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet, store = _world(tmp_path)
    result = apply_as_operator(
        fleet,
        store,
        parse_change("reparent", {"scope_id": "g_a1", "new_parent_id": "g_b"}),
    )
    store.close()
    calls: list[str] = []

    def emit(act_id: str) -> dict:
        calls.append(act_id)
        return {"kind": "chain_changed", "act_id": act_id}

    monkeypatch.setattr(change_events, "emit_structure_change", emit, raising=False)
    code = main(
        [
            "fleet",
            "--db",
            str(tmp_path / "strata.db"),
            "--fleet",
            str(fleet._path),
            "reemit",
            result.act_id,
        ]
    )
    assert code == 0
    assert calls == [result.act_id]
    assert json.dumps({"act_id": result.act_id, "kind": "chain_changed"}) in capsys.readouterr().out


def test_reemit_prints_already_emitted_and_does_not_call_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet, store = _world(tmp_path)
    result = apply_as_operator(
        fleet,
        store,
        parse_change("remove_edge", {"from": "g_a", "to": "g_b"}),
    )
    _notice(store, result.act_id)
    store.close()
    calls: list[str] = []
    monkeypatch.setattr(
        change_events,
        "emit_structure_change",
        lambda act_id: calls.append(act_id),
        raising=False,
    )
    code = main(
        [
            "fleet",
            "--db",
            str(tmp_path / "strata.db"),
            "--fleet",
            str(fleet._path),
            "reemit",
            result.act_id,
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.strip() == "already emitted"
    assert calls == []


def test_reemit_without_core_prints_the_stated_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert not hasattr(change_events, "emit_structure_change")
    fleet, store = _world(tmp_path)
    result = apply_as_operator(
        fleet,
        store,
        parse_change("reparent", {"scope_id": "g_a1", "new_parent_id": "g_b"}),
    )
    store.close()
    code = main(
        [
            "fleet",
            "--db",
            str(tmp_path / "strata.db"),
            "--fleet",
            str(fleet._path),
            "reemit",
            result.act_id,
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "g_a1 now inherits from g_root → g_b" in out
    assert "not re-checked against the new inherited rules automatically (not built yet)" in out
    assert "already emitted" not in out


def test_reemit_unknown_act_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fleet, store = _world(tmp_path)
    store.close()
    code = main(
        [
            "fleet",
            "--db",
            str(tmp_path / "strata.db"),
            "--fleet",
            str(fleet._path),
            "reemit",
            "fsa_missing",
        ]
    )
    assert code == 1
    assert "No fleet structure act fsa_missing." in capsys.readouterr().err


def test_doctor_lists_remove_edge_and_reparent_acts_with_no_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet, store = _world(tmp_path)
    reparented = apply_as_operator(
        fleet,
        store,
        parse_change("reparent", {"scope_id": "g_a1", "new_parent_id": "g_b"}),
    )
    removed = apply_as_operator(
        fleet,
        store,
        parse_change("remove_edge", {"from": "g_a", "to": "g_b"}),
    )
    described = apply_as_operator(
        fleet,
        store,
        parse_change("describe", {"scope_id": "g_a", "description": "purpose"}),
    )
    store.close()
    (tmp_path / ".strata").mkdir()
    (tmp_path / "summaries").mkdir()
    (tmp_path / ".strata" / "config.toml").write_text(
        "\n".join(
            [
                f'db = "{tmp_path / "strata.db"}"',
                f'fleet_yaml = "{fleet._path}"',
                f'summaries_dir = "{tmp_path / "summaries"}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()
    line = _doctor_line(capsys)
    assert reparented.act_id in line
    assert removed.act_id in line
    assert "reparent" in line
    assert "remove_edge" in line
    assert described.act_id not in line
    assert line.lstrip()[0] in {"⚠", "!"}


def test_doctor_omits_an_act_once_an_event_carries_its_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet, store = _world(tmp_path)
    reparented = apply_as_operator(
        fleet,
        store,
        parse_change("reparent", {"scope_id": "g_a1", "new_parent_id": "g_b"}),
    )
    removed = apply_as_operator(
        fleet,
        store,
        parse_change("remove_edge", {"from": "g_a", "to": "g_b"}),
    )
    _notice(store, reparented.act_id)
    # A future act_id column counts even when the payload text does not.
    contribution, event = store.append_change_notice(
        scope_id="g_a",
        content="column only",
        contributor=ContributorRef(
            scope_id="g_a",
            skill=None,
            session_id="s",
            ts="2026-10-11T00:00:00+00:00",
        ),
        change_id="chg_column",
        source_scope_id="g_root",
        item_id="c_column",
        kind="amended",
        after="no act id in this payload",
    )
    store._conn.execute("ALTER TABLE change_events ADD COLUMN act_id TEXT")
    store._conn.execute(
        "UPDATE change_events SET act_id = ? WHERE id = ?",
        (removed.act_id, event.id),
    )
    store._conn.commit()
    assert contribution.id
    store.close()
    (tmp_path / ".strata").mkdir()
    (tmp_path / "summaries").mkdir()
    (tmp_path / ".strata" / "config.toml").write_text(
        "\n".join(
            [
                f'db = "{tmp_path / "strata.db"}"',
                f'fleet_yaml = "{fleet._path}"',
                f'summaries_dir = "{tmp_path / "summaries"}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()
    line = _doctor_line(capsys)
    assert reparented.act_id not in line
    assert removed.act_id not in line
    assert "no remove_edge or reparent act is missing its change events" in line


def test_act_view_lists_emitted_events_and_conflict_lines(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    fleet, store = _world(tmp_path)
    result = apply_as_operator(
        fleet,
        store,
        parse_change("reparent", {"scope_id": "g_a1", "new_parent_id": "g_b"}),
    )
    _notice(store, result.act_id)
    store.close()
    settings = Settings(
        db_path=str(tmp_path / "strata.db"),
        summaries_dir=str(tmp_path / "summaries"),
        fleet_yaml_path=str(fleet._path),
        anthropic_api_key="test-key",
    )
    with TestClient(create_app(settings=settings)) as client:
        body = client.get("/fleet/acts")
        assert body.status_code == 200
        act = next(row for row in body.json()["acts"] if row["id"] == result.act_id)
        assert act["change_type"] == "reparent"
        assert len(act["events"]) == 1
        assert act["events"][0]["kind"] == "published"
        assert act["events"][0]["scope_id"] == "g_a"
        assert act["conflict_lines"] == [_CONFLICT]


def test_perspective_self_layer_annotates_a_conflicting_own_directive(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    fleet, store = _world(tmp_path)
    result = apply_as_operator(
        fleet,
        store,
        parse_change("reparent", {"scope_id": "g_a1", "new_parent_id": "g_b"}),
    )
    _notice(store, result.act_id)
    store.close()
    summaries = tmp_path / "summaries"
    SummaryStore(summaries).write(
        "g_a",
        ScopeSummary(
            scope_id="g_a",
            directives=[
                Directive(
                    id="c_own",
                    content="Ship on Fridays.",
                    subject="release",
                    source_scope_id="g_a",
                    created_at="2026-10-11T00:00:00+00:00",
                )
            ],
            context="",
            updated_at="2026-10-11T00:00:00+00:00",
        ),
    )
    settings = Settings(
        db_path=str(tmp_path / "strata.db"),
        summaries_dir=str(summaries),
        fleet_yaml_path=str(fleet._path),
        anthropic_api_key="test-key",
    )
    with TestClient(create_app(settings=settings)) as client:
        body = client.get("/scopes/g_a/perspective")
        assert body.status_code == 200
        layers = body.json()["layers"]
        self_layer = next(layer for layer in layers if layer["relation"] == "self")
        assert self_layer["own_directive_conflicts"]["c_own"] == [_CONFLICT]
        others = [layer for layer in layers if layer["relation"] != "self"]
        assert all("own_directive_conflicts" not in layer for layer in others)


def test_console_act_and_perspective_views_are_read_only() -> None:
    ui = Path(change_events.__file__).parent / "_ui"
    acts = (ui / "fleet-changes.jsx").read_text(encoding="utf-8")
    perspective = (ui / "view-as.jsx").read_text(encoding="utf-8")
    store = (ui / "store.js").read_text(encoding="utf-8")
    assert "conflict_lines" in acts
    assert "fetchFleetActs" in acts
    assert "Emitted events" in acts
    assert "emit(" not in acts
    assert "own_directive_conflicts" in perspective
    assert 'layer.relation === "self"' in perspective
    assert "fetchFleetActs" in store
    assert "emit(" not in store
