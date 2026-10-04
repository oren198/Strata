"""#234, read signaling — the watermark primitive.

Covers design note §1/§3/§6: each component moves on its own trigger, an
unrelated scope's change never moves this scope's watermark, and the
comparison (`perspective_changed_since`) tolerates an absent baseline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from strata.fleet_config import FleetConfig
from strata.operator import OperatorItem
from strata.perspective import perspective_changed_since, perspective_watermark
from strata.record_store import ChangeEvent
from strata.summary_store import ScopeSummary, SummaryStore

_FLEET_YAML = """
strata:
  - id: L0
    name: Executive
    ordinal: 0
  - id: L1
    name: Team
    ordinal: 1

scopes:
  - id: g_parent
    name: Parent
    stratum_id: L0
  - id: g_child
    name: Child
    stratum_id: L1
  - id: g_other
    name: Other
    stratum_id: L1

edges:
  - from: g_child
    to: g_parent
"""


@pytest.fixture()
def fleet(tmp_path: Path) -> FleetConfig:
    path = tmp_path / "fleet.yaml"
    path.write_text(_FLEET_YAML, encoding="utf-8")
    return FleetConfig.load(path)


@pytest.fixture()
def summary_store(tmp_path: Path) -> SummaryStore:
    return SummaryStore(str(tmp_path / "summaries"))


def _summary(scope_id: str, *, version: int, context: str = "") -> ScopeSummary:
    return ScopeSummary(
        scope_id=scope_id,
        directives=[],
        context=context,
        updated_at="2026-10-02T00:00:00+00:00",
        version=version,
    )


def _write_version(store: SummaryStore, scope_id: str, version: int) -> None:
    # SummaryStore.write() always bumps version by one itself — seed the
    # EXACT target version by writing that many times (simplest faithful
    # way to reach a given version with the real store, not a bypass of it).
    for _ in range(version):
        store.write(scope_id, _summary(scope_id, version=1))


def _operator_item(item_id: str) -> OperatorItem:
    return OperatorItem(
        id=item_id, kind="directive", content="x", subject=None, created_at="2026-10-02T00:00:00Z"
    )


def _change_event(event_id: str, scope_id: str) -> ChangeEvent:
    return ChangeEvent(
        id=event_id,
        change_id=f"chg_{event_id}",
        contribution_id="c_x",
        scope_id=scope_id,
        source_scope_id=None,
        item_id="c_x",
        kind="published",
        before=None,
        after=None,
        hop=0,
        processed_at=None,
        created_at="2026-10-02T00:00:00Z",
        self_notice=0,
        shown_at=None,
    )


def test_own_summary_version_moves_the_watermark(fleet, summary_store) -> None:
    before = perspective_watermark("g_child", fleet=fleet, summary_store=summary_store)
    _write_version(summary_store, "g_child", 1)
    after = perspective_watermark("g_child", fleet=fleet, summary_store=summary_store)
    assert before != after


def test_ancestor_directive_version_moves_the_watermark(fleet, summary_store) -> None:
    before = perspective_watermark("g_child", fleet=fleet, summary_store=summary_store)
    _write_version(summary_store, "g_parent", 1)
    after = perspective_watermark("g_child", fleet=fleet, summary_store=summary_store)
    assert before != after


def test_an_unrelated_scopes_change_does_not_move_the_watermark(fleet, summary_store) -> None:
    before = perspective_watermark("g_child", fleet=fleet, summary_store=summary_store)
    _write_version(summary_store, "g_other", 1)
    after = perspective_watermark("g_child", fleet=fleet, summary_store=summary_store)
    assert before == after


def test_an_operator_act_moves_the_watermark(fleet, summary_store) -> None:
    items: list[OperatorItem] = []

    def reader(scope_id: str) -> list[OperatorItem]:
        return items if scope_id == "g_child" else []

    before = perspective_watermark(
        "g_child", fleet=fleet, summary_store=summary_store, operator_reader=reader
    )
    items.append(_operator_item("op_1"))
    after = perspective_watermark(
        "g_child", fleet=fleet, summary_store=summary_store, operator_reader=reader
    )
    assert before != after


def test_an_operator_act_at_an_ancestor_moves_the_watermark(fleet, summary_store) -> None:
    items: list[OperatorItem] = []

    def reader(scope_id: str) -> list[OperatorItem]:
        return items if scope_id == "g_parent" else []

    before = perspective_watermark(
        "g_child", fleet=fleet, summary_store=summary_store, operator_reader=reader
    )
    items.append(_operator_item("op_1"))
    after = perspective_watermark(
        "g_child", fleet=fleet, summary_store=summary_store, operator_reader=reader
    )
    assert before != after


def test_operator_retire_also_moves_the_watermark(fleet, summary_store) -> None:
    items = [_operator_item("op_1")]

    def reader(scope_id: str) -> list[OperatorItem]:
        return items if scope_id == "g_child" else []

    before = perspective_watermark(
        "g_child", fleet=fleet, summary_store=summary_store, operator_reader=reader
    )
    items.clear()  # a retire act empties the current operator layer
    after = perspective_watermark(
        "g_child", fleet=fleet, summary_store=summary_store, operator_reader=reader
    )
    assert before != after


def test_a_new_change_event_moves_the_watermark(fleet, summary_store) -> None:
    events: list[ChangeEvent] = []

    def reader(scope_id: str) -> list[ChangeEvent]:
        return events

    before = perspective_watermark(
        "g_child", fleet=fleet, summary_store=summary_store, change_event_reader=reader
    )
    events.append(_change_event("ce_1", "g_child"))
    after = perspective_watermark(
        "g_child", fleet=fleet, summary_store=summary_store, change_event_reader=reader
    )
    assert before != after


def test_marking_an_event_processed_alone_does_not_move_the_watermark(fleet, summary_store) -> None:
    """The reader returns events processed or not (ADR 0014) — but the
    watermark's count+newest-id marker intentionally never reads
    `processed_at`, so a drain marking an EXISTING event processed (no new
    event, no new summary version) is not itself a watermark move; the
    summary rewrite that drain performs is what moves it, via the summary
    version component instead."""
    import dataclasses

    events = [_change_event("ce_1", "g_child")]
    processed = [dataclasses.replace(events[0], processed_at="2026-10-02T00:00:01Z")]

    def reader_unprocessed(scope_id: str) -> list[ChangeEvent]:
        return events

    def reader_processed(scope_id: str) -> list[ChangeEvent]:
        return processed

    before = perspective_watermark(
        "g_child",
        fleet=fleet,
        summary_store=summary_store,
        change_event_reader=reader_unprocessed,
    )
    after = perspective_watermark(
        "g_child", fleet=fleet, summary_store=summary_store, change_event_reader=reader_processed
    )
    assert before == after


def test_an_unrelated_scopes_change_event_does_not_move_the_watermark(fleet, summary_store) -> None:
    events: list[ChangeEvent] = []

    def reader(scope_id: str) -> list[ChangeEvent]:
        return events if scope_id == "g_child" else [_change_event("ce_other", "g_other")]

    before = perspective_watermark(
        "g_child", fleet=fleet, summary_store=summary_store, change_event_reader=reader
    )
    after = perspective_watermark(
        "g_child", fleet=fleet, summary_store=summary_store, change_event_reader=reader
    )
    assert before == after


def test_changed_since_with_no_baseline_is_never_stale(fleet, summary_store) -> None:
    assert (
        perspective_changed_since(None, "g_child", fleet=fleet, summary_store=summary_store)
        is False
    )


def test_changed_since_detects_a_real_move(fleet, summary_store) -> None:
    watermark = perspective_watermark("g_child", fleet=fleet, summary_store=summary_store)
    assert (
        perspective_changed_since(watermark, "g_child", fleet=fleet, summary_store=summary_store)
        is False
    )
    _write_version(summary_store, "g_child", 1)
    assert (
        perspective_changed_since(watermark, "g_child", fleet=fleet, summary_store=summary_store)
        is True
    )


def test_watermark_raises_for_an_unknown_scope(fleet, summary_store) -> None:
    with pytest.raises(ValueError, match="Scope not found"):
        perspective_watermark("g_nonexistent", fleet=fleet, summary_store=summary_store)
