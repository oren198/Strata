"""v1.20 forgetting core, stage 2: operator items (end condition, date lapse, retire with a
notice, candidates). The operator owns its items, so only the operator confirms or retires."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from strata import forgetting
from strata.forgetting import PositionGateRefusal, validate_fulfils
from strata.operator import (
    OperatorItem,
    operator_publish,
    operator_retire_item,
    read_operator_layer,
    sweep_operator_date_lapses,
)
from strata.perspective import compose_perspective
from strata.record_store import ContributorRef
from strata.summary_store import EndCondition
from tests import test_v18_243_publication_exception as t243

world = t243.world

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
OWNER_SESSION = ContributorRef(scope_id="g_org", skill=None, session_id="s_org", ts="t")
NOTICE = "The freeze is over; deploys are open again."


def _kw(world):  # noqa: ANN001, ANN202
    fleet, store, summaries = world
    return {"fleet": fleet, "record_store": store, "summaries_dir": str(summaries.summaries_dir)}


def _layer(world) -> list[OperatorItem]:  # noqa: ANN001
    return read_operator_layer("g_org", summaries_dir=str(world[2].summaries_dir))


def _perspective(world, scope_id: str) -> dict:  # noqa: ANN001
    fleet, store, summaries = world
    return compose_perspective(
        scope_id,
        fleet=fleet,
        summary_store=summaries,
        operator_reader=lambda s: read_operator_layer(
            s, summaries_dir=str(summaries.summaries_dir)
        ),
        change_event_reader=lambda s: store.list_change_events(scope_id=s),
    )


def _operator_ids(perspective: dict) -> set[str]:
    return {
        d["id"]
        for layer in perspective["layers"]
        if layer.get("relation") == "operator"
        for d in layer["operator_memory"]["directives"]
    }


def test_an_operator_item_carries_its_end_condition_through_the_layer_file(world) -> None:  # noqa: ANN001
    item = operator_publish("g_org", "Freeze deploys.", ends={"until": "2026-12-01"}, **_kw(world))
    assert item.end_condition == EndCondition(kind="date", until="2026-12-01")
    assert item.set_by == item.id
    (back,) = _layer(world)
    assert back.end_condition == item.end_condition and back.set_by == item.id
    shown = [
        d
        for layer in _perspective(world, "g_web")["layers"]
        if layer.get("relation") == "operator"
        for d in layer["operator_memory"]["directives"]
    ]
    assert shown[0]["ends"] == {"kind": "date", "until": "2026-12-01"}


def test_an_item_without_one_is_exactly_as_before(world) -> None:  # noqa: ANN001
    operator_publish("g_org", "Tag releases.", **_kw(world))
    shown = [
        d
        for layer in _perspective(world, "g_web")["layers"]
        if layer.get("relation") == "operator"
        for d in layer["operator_memory"]["directives"]
    ]
    assert set(shown[0]) == {"id", "content", "subject", "created_at"}
    text = (world[2].summaries_dir / "operator" / "g_org.md").read_text(encoding="utf-8")
    assert "ends:" not in text


def test_a_bad_end_condition_writes_nothing(world) -> None:  # noqa: ANN001
    with pytest.raises(ValueError, match="past"):
        operator_publish("g_org", "Freeze.", ends={"until": "2020-01-01"}, **_kw(world))
    assert _layer(world) == [] and world[1].list_operator_acts(target_scope_id="g_org") == []


def test_a_date_lapse_retires_an_operator_item_and_tells_its_readers(world) -> None:  # noqa: ANN001
    item = operator_publish("g_org", "Freeze deploys.", ends={"until": "2026-11-09"}, **_kw(world))
    assert (
        sweep_operator_date_lapses("g_org", now=datetime(2026, 11, 9, 23, tzinfo=UTC), **_kw(world))
        == []
    )
    (act,) = sweep_operator_date_lapses("g_org", now=NOW, **_kw(world))
    assert act.retires == item.id and _layer(world) == []
    detail = world[1].get_operator_act_forgetting(act.id)
    assert detail["cause"] == "date_lapse" and detail["kind"] == "currency"
    assert detail["actor_session_id"] == "engine" and "bound until 2026-11-09" in detail["notice"]
    assert sweep_operator_date_lapses("g_org", now=NOW, **_kw(world)) == []
    for reader in ("g_org", "g_web", "g_api"):
        perspective = _perspective(world, reader)
        assert item.id not in _operator_ids(perspective)
        (change,) = [
            c
            for c in perspective["input_changes"]
            if c["item_id"] == item.id and c["after"] is None
        ]
        assert change["retirement"]["label"] == "retired"
        assert change["retirement"]["cause"] == "date_lapse"


def test_a_retire_with_kind_and_notice_reaches_every_reader_with_its_label(world) -> None:  # noqa: ANN001
    a = operator_publish("g_org", "Freeze deploys.", **_kw(world))
    b = operator_publish("g_org", "Use the old gateway.", **_kw(world))
    operator_retire_item("g_org", a.id, kind="currency", notice=NOTICE, **_kw(world))
    operator_retire_item(
        "g_org", b.id, kind="correction", notice="It was never right.", **_kw(world)
    )
    for reader in ("g_web", "g_api"):
        perspective = _perspective(world, reader)
        assert not (_operator_ids(perspective) & {a.id, b.id})
        gone_a = [
            c for c in perspective["input_changes"] if c["item_id"] == a.id and c["after"] is None
        ]
        gone_b = [
            c for c in perspective["input_changes"] if c["item_id"] == b.id and c["after"] is None
        ]
        assert gone_a[0]["retirement"]["label"] == "retired"
        assert gone_a[0]["retirement"]["notice"] == NOTICE
        assert gone_b[0]["retirement"]["label"] == "retracted"
        # the event is the ordinary one, and the item's earlier publish notice is not relabelled
        assert gone_a[0]["kind"] == gone_b[0]["kind"] == "operator_directive_changed"
        published = [c for c in perspective["input_changes"] if c["item_id"] == a.id and c["after"]]
        assert published and "retirement" not in published[0]


def test_the_plain_retire_keeps_its_event_shape(world) -> None:  # noqa: ANN001
    item = operator_publish("g_org", "Freeze deploys.", **_kw(world))
    operator_retire_item("g_org", item.id, **_kw(world))
    change = [
        c
        for c in _perspective(world, "g_web")["input_changes"]
        if c["item_id"] == item.id and c["after"] is None
    ][0]
    assert "retirement" not in change


def test_kind_and_notice_go_together_and_nothing_is_written_otherwise(world) -> None:  # noqa: ANN001
    item = operator_publish("g_org", "Freeze deploys.", **_kw(world))
    for bad in (
        {"kind": "currency"},
        {"notice": NOTICE},
        {"kind": "forgotten", "notice": NOTICE},
        {"kind": "currency", "notice": "  "},
    ):
        with pytest.raises(ValueError):
            operator_retire_item("g_org", item.id, **bad, **_kw(world))
    assert [i.id for i in _layer(world)] == [item.id]


def test_a_read_through_a_descendant_lapses_an_ancestors_operator_item(world) -> None:  # noqa: ANN001
    item = operator_publish("g_org", "Freeze deploys.", ends={"until": "2026-11-09"}, **_kw(world))
    forgetting.sweep_for_read(
        "g_web", fleet=world[0], record_store=world[1], summary_store=world[2], now=NOW
    )
    assert item.id not in _operator_ids(_perspective(world, "g_web"))


def test_operator_candidates_belong_to_the_operator_not_to_scope_sessions(world) -> None:  # noqa: ANN001
    item = operator_publish(
        "g_org", "Freeze deploys.", ends={"when": "the audit closes"}, **_kw(world)
    )
    store = world[1]
    evidence = store.append_contribution(
        scope_id="g_web",
        content="The audit closed.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(scope_id="g_web", skill=None, session_id="s_web", ts="t"),
        fulfils=item.id,
    )
    store.record_judgment(
        contribution_id=evidence.id,
        decision="accept_as_context",
        judged_by="scope-manager",
        notes=None,
    )
    forgetting.compute_retirement_candidates(
        "g_org", record_store=store, summary_store=world[2], now=NOW, force=True
    )
    (candidate,) = forgetting.operator_candidates(store, scope_id="g_org")
    assert candidate.ground == "condition_evidence" and evidence.id in candidate.evidence_ids
    assert forgetting.visible_candidates("g_org", OWNER_SESSION, record_store=store) == []
    with pytest.raises(PositionGateRefusal):
        forgetting.dismiss_candidate(
            candidate.id, reason="keep", days=5, actor=OWNER_SESSION, record_store=store
        )
    forgetting.dismiss_operator_candidate(
        candidate.id, reason="not yet", days=5, record_store=store
    )
    assert store.get_retirement_candidate(candidate.id).state == "dismissed"
    assert [i.id for i in _layer(world)] == [item.id]  # nothing was retired


def test_fulfils_can_name_an_operator_item_in_the_readers_surface(world) -> None:  # noqa: ANN001
    item = operator_publish(
        "g_org", "Freeze deploys.", ends={"when": "the audit closes"}, **_kw(world)
    )
    web = ContributorRef(scope_id="g_web", skill=None, session_id="s_web", ts="t")
    owner, directive = validate_fulfils(item.id, web, fleet=world[0], summary_store=world[2])
    assert owner == "g_org" and directive.end_condition.kind == "judged"
    plain = operator_publish("g_org", "Tag releases.", **_kw(world))
    with pytest.raises(ValueError, match="no judged end condition"):
        validate_fulfils(plain.id, web, fleet=world[0], summary_store=world[2])
