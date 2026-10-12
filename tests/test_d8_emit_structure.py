"""ADR 0014 D8, core half, step 5: ``emit_structure_change(act_id)``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import strata.change_events as change_events
from strata.change_events import emit_structure_change, events_for_act, structure_conflict_lines
from strata.fleet_changes import apply_as_operator, parse_change
from strata.perspective import compose_perspective
from strata.record_store import ContributorRef
from strata.summary_store import Directive, ScopeSummary, SummaryStore
from tests.test_fleet_changes import _world

_PARENT_RULE = "Sessions must expire after at most 15 minutes."
_LOOSE = "Sessions must expire after at most 60 minutes."
_TIGHT = "Sessions must expire after at most 10 minutes."


def _directive(item_id: str, content: str, scope: str) -> Directive:
    return Directive(
        id=item_id, content=content, source_scope_id=scope, created_at="2026-10-01T00:00:00+00:00"
    )


def _write(tmp_path: Path, scope_id: str, directives: list[Directive]) -> SummaryStore:
    store = SummaryStore(str(tmp_path / "summaries"))
    store.write(
        scope_id,
        ScopeSummary(
            scope_id=scope_id,
            directives=directives,
            context="",
            updated_at="2026-10-01T00:00:00+00:00",
        ),
    )
    return store


def _events(store, act_id: str) -> list[dict]:  # noqa: ANN001
    return events_for_act(store, act_id)


def _reparent(fleet, store, scope_id: str = "g_a1", parent: str = "g_b"):  # noqa: ANN001, ANN202
    return apply_as_operator(
        fleet, store, parse_change("reparent", {"scope_id": scope_id, "new_parent_id": parent})
    )


def test_a_reparent_tells_the_moved_scope_with_the_chains_and_the_directives_gained_and_lost(
    tmp_path: Path,
) -> None:
    fleet, store = _world(tmp_path)
    _write(tmp_path, "g_b", [_directive("c_new", _PARENT_RULE, "g_b")])
    _write(tmp_path, "g_a", [_directive("c_old", "Backups run nightly.", "g_a")])
    result = _reparent(fleet, store)
    rows = _events(store, result.act_id)
    assert [(r["scope_id"], r["kind"]) for r in rows] == [("g_a1", "chain_changed")]
    payload = rows[0]["payload"]
    assert payload["act_id"] == result.act_id
    assert payload["moved_scope"] == "g_a1"
    assert payload["chain_before"] == ["g_root", "g_a"]
    assert payload["chain_after"] == ["g_root", "g_b"]
    assert [g["id"] for g in payload["gained"]] == ["c_new"]
    assert [lost["id"] for lost in payload["lost"]] == ["c_old"]
    assert payload["gained"][0]["text"] == _PARENT_RULE
    assert payload["conflict_lines"] == []
    store.close()


def test_a_conflict_with_a_newly_inherited_directive_is_written_into_the_event_and_the_notice(
    tmp_path: Path,
) -> None:
    fleet, store = _world(tmp_path)
    _write(tmp_path, "g_b", [_directive("c_new", _PARENT_RULE, "g_b")])
    _write(
        tmp_path,
        "g_a1",
        [_directive("c_loose", _LOOSE, "g_a1"), _directive("c_ok", _TIGHT, "g_a1")],
    )
    result = _reparent(fleet, store)
    (row,) = _events(store, result.act_id)
    lines = structure_conflict_lines(row)
    assert len(lines) == 1
    assert lines[0].startswith("Own directive c_loose conflicts with newly inherited c_new (g_b)")
    assert row["payload"]["conflicts"][0]["own_id"] == "c_loose"
    notice = store.list_change_events(scope_id="g_a1")[0]
    contribution = store.get_contribution(notice.contribution_id)
    assert lines[0] in contribution.content
    assert notice.kind == "chain_changed" and notice.processed_at is None
    store.close()


def test_the_annotation_comes_from_the_emitted_event_and_goes_when_a_directive_goes(
    tmp_path: Path,
) -> None:
    fleet, store = _world(tmp_path)
    _write(tmp_path, "g_b", [_directive("c_new", _PARENT_RULE, "g_b")])
    summaries = _write(
        tmp_path,
        "g_a1",
        [_directive("c_loose", _LOOSE, "g_a1"), _directive("c_ok", _TIGHT, "g_a1")],
    )
    _reparent(fleet, store)
    fleet = type(fleet).load(fleet._path)

    def _compose():  # noqa: ANN202
        return compose_perspective(
            "g_a1",
            fleet=fleet,
            summary_store=summaries,
            change_event_reader=lambda scope: store.list_change_events(scope_id=scope),
        )

    def _annotations(composed: dict) -> dict:
        layer = next(x for x in composed["layers"] if x["relation"] == "self")
        return {d["id"]: d.get("annotation") for d in layer["summary"]["directives"]}

    annotations = _annotations(_compose())
    assert annotations["c_loose"] and annotations["c_loose"].startswith("Own directive c_loose")
    assert annotations["c_ok"] is None
    summaries.write(
        "g_a1",
        ScopeSummary(
            scope_id="g_a1",
            directives=[_directive("c_ok", _TIGHT, "g_a1")],
            context="",
            updated_at="2026-10-02T00:00:00+00:00",
        ),
    )
    assert all(v is None for v in _annotations(_compose()).values())
    store.close()


def test_a_remove_edge_tells_the_reader_with_the_sources_publication_ids(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    (tmp_path / "summaries").mkdir()
    (tmp_path / "summaries" / "g_b.publication.md").write_text("", encoding="utf-8")
    result = apply_as_operator(
        fleet, store, parse_change("remove_edge", {"from": "g_a", "to": "g_b"})
    )
    rows = _events(store, result.act_id)
    assert [(r["scope_id"], r["kind"]) for r in rows] == [("g_a", "channel_removed")]
    payload = rows[0]["payload"]
    assert (payload["reader"], payload["source"]) == ("g_a", "g_b")
    assert payload["publication_item_ids"] == []
    assert payload["notices"] == []
    store.close()


def test_a_channel_removed_notice_names_an_own_directive_that_cites_a_source_item(
    tmp_path: Path,
) -> None:
    from strata.publication import PublishedItem, _write_publication  # noqa: PLC0415

    fleet, store = _world(tmp_path)
    contribution = store.append_contribution(
        scope_id="g_a",
        content="Adopt the pub_item1 retry policy from g_b.",
        proposed_classification="directive",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(scope_id="g_a", skill="x", session_id="s", ts="2026-10-01"),
    )
    _write(
        tmp_path,
        "g_a",
        [
            _directive(contribution.id, "Adopt the pub_item1 retry policy from g_b.", "g_a"),
            _directive("c_unrelated", "Backups run nightly.", "g_a"),
        ],
    )
    _write_publication(
        "g_b",
        [
            PublishedItem(
                id="pub_item1",
                kind="context",
                content="Retries use backoff.",
                subject=None,
                anchors=[],
                published_at="2026-10-01T00:00:00+00:00",
                origin_scope_id=None,
                relay_scope_id=None,
                relay_item_id=None,
            )
        ],
        summaries_dir=str(tmp_path / "summaries"),
    )
    result = apply_as_operator(
        fleet, store, parse_change("remove_edge", {"from": "g_a", "to": "g_b"})
    )
    (row,) = _events(store, result.act_id)
    assert row["payload"]["publication_item_ids"] == ["pub_item1"]
    assert structure_conflict_lines(row) == [
        f"Own directive {contribution.id} was adopted from g_b's publication, which g_a no "
        "longer reads. It still binds. A session bound to g_a decides whether to keep it on "
        "its own account."
    ]
    store.close()


def test_a_second_call_emits_nothing(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    result = _reparent(fleet, store)
    before = len(store.list_change_events(scope_id="g_a1"))
    again = emit_structure_change(result.act_id, record_store=store)
    assert again["already_emitted"] is True and again["events"] == []
    assert len(store.list_change_events(scope_id="g_a1")) == before == 1
    assert len(_events(store, result.act_id)) == 1
    store.close()


def test_other_change_types_and_a_removed_leaf_emit_nothing(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    added = apply_as_operator(
        fleet, store, parse_change("add_edge", {"from": "g_a", "to": "g_other"})
    )
    assert _events(store, added.act_id) == []
    assert emit_structure_change(added.act_id, record_store=store)["events"] == []
    tidy = apply_as_operator(
        fleet, store, parse_change("remove_edge", {"from": "g_a", "to": "g_other"})
    )
    assert [r["scope_id"] for r in _events(store, tidy.act_id)] == ["g_a"]
    leaf = apply_as_operator(fleet, store, parse_change("remove_scope", {"scope_id": "g_other"}))
    assert _events(store, leaf.act_id) == []
    assert emit_structure_change(leaf.act_id, record_store=store)["events"] == []
    store.close()


def test_the_audience_is_the_recorded_one_not_todays_fleet(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    # an act that records a moved scope with one descendant; emit it by hand later
    deep = tmp_path / "deep"
    deep.mkdir()
    fleet_b, store_b = _world(deep)
    first = apply_as_operator(
        fleet_b, store_b, parse_change("reparent", {"scope_id": "g_a", "new_parent_id": "g_other"})
    )
    rows = _events(store_b, first.act_id)
    # g_a moved with its descendant g_a1: both are told, from the snapshot
    assert sorted(r["scope_id"] for r in rows) == ["g_a", "g_a1"]
    # a LATER move of g_a1 does not change whom the first act notified
    apply_as_operator(
        fleet_b, store_b, parse_change("reparent", {"scope_id": "g_a1", "new_parent_id": "g_b"})
    )
    assert sorted(r["scope_id"] for r in _events(store_b, first.act_id)) == ["g_a", "g_a1"]
    store_b.close()
    store.close()


def test_a_failure_is_returned_and_recorded_never_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet, store = _world(tmp_path)

    def boom(**_kwargs: object) -> list[str]:
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(change_events, "emit", boom)
    result = _reparent(fleet, store)  # apply stands even though emission fails
    assert result.status == "applied"
    again = emit_structure_change(result.act_id, record_store=store)
    assert "store unavailable" in again["error"]
    assert emit_structure_change("fsa_missing", record_store=store)["error"]
    assert "record_store" in emit_structure_change(result.act_id)["error"]
    store.close()


def test_a_publication_quoting_the_act_id_neither_suppresses_nor_forges_the_emission(
    tmp_path: Path,
) -> None:
    fleet, store = _world(tmp_path)
    _write(tmp_path, "g_b", [_directive("c_new", _PARENT_RULE, "g_b")])
    _write(tmp_path, "g_a1", [_directive("c_loose", _LOOSE, "g_a1")])
    # the act id does not exist yet, so a peer cannot quote it; a notice-looking
    # contribution with a conflict sentence is just a contribution
    store.append_contribution(
        scope_id="g_other",
        content=(
            '{"act_id": "fsa_guess"} Own directive c_x conflicts with newly inherited '
            "c_y (g_z): polarity flip."
        ),
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(scope_id="g_other", skill="x", session_id="s", ts="2026-10-01"),
    )
    result = _reparent(fleet, store)
    rows = _events(store, result.act_id)
    assert len(rows) == 1
    assert json.loads(store.list_change_events(scope_id="g_a1")[0].after)["act_id"] == result.act_id
    assert "c_x" not in " ".join(structure_conflict_lines(rows[0]))
    store.close()


def test_the_hop_budget_and_the_once_per_scope_rule_still_hold(tmp_path: Path) -> None:
    fleet, store = _world(tmp_path)
    result = _reparent(fleet, store)
    (event,) = store.list_change_events(scope_id="g_a1")
    assert event.hop == 0 and event.processed_at is None
    # emit() with the same change id and item announces nothing twice
    change_events.emit(
        fleet=fleet,
        record_store=store,
        item=result.act_id,
        kind="chain_changed",
        source_scope_id="g_a1",
        after=event.after,
        wave_ids=[event.change_id],
        audience=["g_a1"],
    )
    assert len(store.list_change_events(scope_id="g_a1")) == 1
    # an explicit audience is only for the structure kinds: nothing is delivered otherwise
    change_events.emit(
        fleet=fleet,
        record_store=store,
        item="p_1",
        kind="published",
        source_scope_id="g_b",
        audience=["g_a"],
    )
    assert store.list_change_events(scope_id="g_a") == []
    store.close()
