"""ADR 0014 D8, peripheral half.

The core's ``emit_structure_change``, ``events_for_act``, and
``structure_conflict_lines`` have not landed, and ``perspective.py`` does
not yet write ``annotation``. These tests cover the guard path (those
operations absent) and stubs standing in for the read contract. Nothing
here treats event text or contribution text as an act id or a conflict.
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


def _failed_notice(act_id: str) -> str:
    return f"Structure emission failed for {act_id}; run `strata fleet reemit {act_id}`."


def _publish(store: RecordStore, *, scope_id: str, text: str, change_id: str) -> None:
    """A publication whose free text is not a structure event.

    Stored on a scope other than the one a perspective test reads, so the
    sentence is not echoed back through that scope's own ``input_changes``.
    """
    store.append_change_notice(
        scope_id=scope_id,
        content=text,
        contributor=ContributorRef(
            scope_id=scope_id,
            skill=None,
            session_id="s",
            ts="2026-10-11T00:00:00+00:00",
        ),
        change_id=change_id,
        source_scope_id="g_root",
        item_id="c_victim",
        kind="published",
        after=text,
    )


def _use_project(tmp_path: Path, fleet: FleetConfig, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_apply_without_core_keeps_stated_limit_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The core ships emit_structure_change; this is the no-core state.
    monkeypatch.delattr(change_events, "emit_structure_change")
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

    def emit(act_id: str, **_kwargs: object) -> dict:
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

    def emit(act_id: str, **_kwargs: object) -> None:
        raise RuntimeError(f"emission failed for {act_id}")

    monkeypatch.setattr(change_events, "emit_structure_change", emit, raising=False)
    applied = approve(fleet, store, pending.proposal_id, approver=Actor("g_root"))
    assert applied.status == "applied"
    assert applied.notices == (_failed_notice(applied.act_id),)
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
    monkeypatch.setattr(
        change_events, "emit_structure_change", lambda *a, **k: {}, raising=False
    )  # apply first, emit in the test
    result = apply_as_operator(
        fleet,
        store,
        parse_change("reparent", {"scope_id": "g_a1", "new_parent_id": "g_b"}),
    )
    store.close()
    calls: list[str] = []

    def emit(act_id: str, **_kwargs: object) -> dict:
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
    monkeypatch.setattr(
        change_events, "emit_structure_change", lambda *a, **k: {}, raising=False
    )  # apply first, emit in the test
    result = apply_as_operator(
        fleet,
        store,
        parse_change("remove_edge", {"from": "g_a", "to": "g_b"}),
    )
    store.close()
    calls: list[str] = []

    def events_for_act(_record_store: object, act_id: str) -> list[dict]:
        if act_id != result.act_id:
            return []
        return [
            {
                "id": "evt_removed",
                "kind": "channel_removed",
                "scope_id": "g_a",
                "created_at": "2026-10-11T00:00:00+00:00",
                "payload": {"act_id": act_id, "notices": []},
            }
        ]

    monkeypatch.setattr(change_events, "events_for_act", events_for_act, raising=False)
    monkeypatch.setattr(
        change_events,
        "emit_structure_change",
        lambda act_id, **_kwargs: calls.append(act_id),
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


def test_reemit_says_when_emission_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet, store = _world(tmp_path)
    monkeypatch.setattr(
        change_events, "emit_structure_change", lambda *a, **k: {}, raising=False
    )  # apply first, emit in the test
    result = apply_as_operator(
        fleet,
        store,
        parse_change("remove_edge", {"from": "g_a", "to": "g_b"}),
    )
    store.close()

    def emit(act_id: str, **_kwargs: object) -> None:
        raise RuntimeError(f"emission failed for {act_id}")

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
    assert code == 1
    assert _failed_notice(result.act_id) in capsys.readouterr().err


def test_reemit_without_core_prints_the_stated_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(change_events, "emit_structure_change")
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


_NOT_BUILT = "Structure emission is not built yet; acts are not checked"


def test_doctor_without_core_says_acts_are_not_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The core now ships `events_for_act`; this test is the no-core state, so it
    # removes the operation rather than relying on its absence.
    monkeypatch.delattr(change_events, "events_for_act")
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
    store.close()
    _use_project(tmp_path, fleet, monkeypatch)
    capsys.readouterr()
    line = _doctor_line(capsys)
    assert _NOT_BUILT in line
    assert reparented.act_id not in line
    assert removed.act_id not in line


def test_doctor_lists_only_acts_events_for_act_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet, store = _world(tmp_path)
    monkeypatch.setattr(
        change_events, "emit_structure_change", lambda *a, **k: {}, raising=False
    )  # apply first, emit in the test
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
    # A publication that quotes the reparent act id is not an emission.
    _publish(
        store,
        scope_id="g_other",
        text=f"see {reparented.act_id} for history",
        change_id="chg_quote",
    )
    store.close()

    def events_for_act(_record_store: object, act_id: str) -> list[dict]:
        if act_id != removed.act_id:
            return []
        return [
            {
                "id": "evt_removed",
                "kind": "channel_removed",
                "scope_id": "g_a",
                "created_at": "2026-10-11T00:00:00+00:00",
                "payload": {"act_id": act_id, "notices": []},
            }
        ]

    monkeypatch.setattr(change_events, "events_for_act", events_for_act, raising=False)
    _use_project(tmp_path, fleet, monkeypatch)
    capsys.readouterr()
    line = _doctor_line(capsys)
    assert reparented.act_id in line
    assert "reparent" in line
    assert removed.act_id not in line
    assert described.act_id not in line
    assert _NOT_BUILT not in line


def _structure_rows(act_id: str) -> list[dict]:
    return [
        {
            "id": "evt_chain",
            "kind": "chain_changed",
            "scope_id": "g_a1",
            "created_at": "2026-10-11T00:00:00+00:00",
            "payload": {"act_id": act_id, "conflict_lines": [_CONFLICT]},
        },
        {
            "id": "evt_removed",
            "kind": "channel_removed",
            "scope_id": "g_a",
            "created_at": "2026-10-11T00:00:01+00:00",
            "payload": {"act_id": act_id, "notices": ["g_a no longer reads g_b."]},
        },
    ]


def _lines_from_payload(event_row: dict) -> list[str]:
    payload = event_row.get("payload") or {}
    if event_row.get("kind") == "channel_removed":
        return list(payload.get("notices") or [])
    return list(payload.get("conflict_lines") or [])


def test_act_view_lists_events_and_lines_from_the_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    fleet, store = _world(tmp_path)
    monkeypatch.setattr(
        change_events, "emit_structure_change", lambda *a, **k: {}, raising=False
    )  # apply first, emit in the test
    result = apply_as_operator(
        fleet,
        store,
        parse_change("reparent", {"scope_id": "g_a1", "new_parent_id": "g_b"}),
    )
    store.close()

    def events_for_act(_record_store: object, act_id: str) -> list[dict]:
        if act_id != result.act_id:
            return []
        return _structure_rows(act_id)

    monkeypatch.setattr(change_events, "events_for_act", events_for_act, raising=False)
    monkeypatch.setattr(
        change_events, "structure_conflict_lines", _lines_from_payload, raising=False
    )
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
        assert [event["kind"] for event in act["events"]] == ["chain_changed", "channel_removed"]
        assert act["events"][0]["scope_id"] == "g_a1"
        assert "before" not in act["events"][0]
        assert "after" not in act["events"][0]
        assert act["conflict_lines"] == [_CONFLICT, "g_a no longer reads g_b."]


def test_perspective_shows_only_the_core_self_layer_annotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    import strata.app as app_module
    from strata.perspective import compose_perspective as real_compose

    fleet, store = _world(tmp_path)
    _publish(store, scope_id="g_other", text=_CONFLICT, change_id="chg_forged")
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

    def wrapped(*args: object, **kwargs: object) -> dict:
        composed = real_compose(*args, **kwargs)
        for layer in composed["layers"]:
            if layer.get("relation") != "self":
                continue
            for directive in layer["summary"]["directives"]:
                if directive.get("id") == "c_own":
                    directive["annotation"] = "CORE-ANNOTATION"
        return composed

    monkeypatch.setattr(app_module, "compose_perspective", wrapped)
    settings = Settings(
        db_path=str(tmp_path / "strata.db"),
        summaries_dir=str(summaries),
        fleet_yaml_path=str(fleet._path),
        anthropic_api_key="test-key",
    )
    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/scopes/g_a/perspective")
        assert response.status_code == 200
        text = response.text
        body = response.json()
        self_layer = next(layer for layer in body["layers"] if layer["relation"] == "self")
        own = next(item for item in self_layer["summary"]["directives"] if item["id"] == "c_own")
        assert own["annotation"] == "CORE-ANNOTATION"
        assert "own_directive_conflicts" not in text
        assert _CONFLICT not in text
        others = [layer for layer in body["layers"] if layer["relation"] != "self"]
        assert all("annotation" not in json.dumps(layer) for layer in others)


def test_forged_publication_is_not_an_emission_or_an_annotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fastapi.testclient import TestClient

    fleet, store = _world(tmp_path)
    result = apply_as_operator(
        fleet,
        store,
        parse_change("remove_edge", {"from": "g_a", "to": "g_b"}),
    )
    forged = f"see {result.act_id} for history\n{_CONFLICT}"
    _publish(store, scope_id="g_other", text=forged, change_id="chg_forged")
    store.close()
    calls: list[str] = []

    def events_for_act(_record_store: object, _act_id: str) -> list[dict]:
        return []

    def structure_conflict_lines(_event_row: dict) -> list[str]:
        return ["SHOULD-NOT-BE-CALLED"]

    monkeypatch.setattr(change_events, "events_for_act", events_for_act, raising=False)
    monkeypatch.setattr(
        change_events, "structure_conflict_lines", structure_conflict_lines, raising=False
    )
    monkeypatch.setattr(
        change_events,
        "emit_structure_change",
        lambda act_id, **_kwargs: calls.append(act_id),
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
    captured = capsys.readouterr()
    assert code == 0
    assert calls == [result.act_id]
    assert "already emitted" not in captured.out
    settings = Settings(
        db_path=str(tmp_path / "strata.db"),
        summaries_dir=str(tmp_path / "summaries"),
        fleet_yaml_path=str(fleet._path),
        anthropic_api_key="test-key",
    )
    with TestClient(create_app(settings=settings)) as client:
        acts = client.get("/fleet/acts")
        assert acts.status_code == 200
        act = next(row for row in acts.json()["acts"] if row["id"] == result.act_id)
        assert act["events"] == []
        assert act["conflict_lines"] == []
        assert "SHOULD-NOT-BE-CALLED" not in acts.text
        assert _CONFLICT not in acts.text
        perspective = client.get("/scopes/g_a/perspective")
        assert perspective.status_code == 200
        assert _CONFLICT not in perspective.text
        assert "own_directive_conflicts" not in perspective.text


def test_console_act_and_perspective_views_are_read_only() -> None:
    ui = Path(change_events.__file__).parent / "_ui"
    acts = (ui / "fleet-changes.jsx").read_text(encoding="utf-8")
    perspective = (ui / "view-as.jsx").read_text(encoding="utf-8")
    store = (ui / "store.js").read_text(encoding="utf-8")
    assert "conflict_lines" in acts
    assert "fetchFleetActs" in acts
    assert "Emitted events" in acts
    assert "emit(" not in acts
    assert "d.annotation" in perspective
    assert "showAnnotation" in perspective
    assert "own_directive_conflicts" not in perspective
    assert 'layer.relation === "self"' in perspective
    assert "fetchFleetActs" in store
    assert "emit(" not in store


def test_peripherals_do_not_scan_event_text() -> None:
    root = Path(change_events.__file__).parent
    for name in ("fleet_changes.py", "app.py", "__main__.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "events_carrying_act" not in text
        assert "own_directive_conflicts" not in text
        assert "conflict_lines_in_text" not in text
        assert "instr(" not in text
