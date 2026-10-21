"""One change event per changed directive (issue #196, item 1).

An amendment that moves several directives tells each descendant once per
directive, under one change id. A descendant refreshes once for that id.
The sweep withdraws every retired id. A re-read does not duplicate the lines.

A one-for-one replacement stays the single ``directive_superseded`` event
the existing suite already requires. These tests cover the other shape:
two directives retired and one added.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from strata.app import drain_scope, run_contribution
from strata.change_events import _prior_notices
from strata.change_events import emit as emit_change_event
from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.perspective import compose_perspective
from strata.publication import propose_publish, read_publication
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import (
    BatchVerdict,
    PublicationJudgment,
    ScopeManager,
    ScopeManagerBatchJudgment,
    ScopeManagerJudgment,
)
from strata.summary_store import Directive, ScopeSummary, SummaryStore

OLD_A = "c_old_a"
OLD_B = "c_old_b"
NEW = "c_new"


@pytest.fixture
def fleet(tmp_path: Path) -> FleetConfig:
    raw = {
        "strata": [
            {"id": "L0", "name": "executive", "ordinal": 0},
            {"id": "L1", "name": "team", "ordinal": 1},
        ],
        "scopes": [
            {"id": "g_196_parent", "name": "Parent", "stratum_id": "L0"},
            {"id": "g_196_child", "name": "Child", "stratum_id": "L1"},
        ],
        "edges": [{"from": "g_196_child", "to": "g_196_parent"}],
    }
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.dump(raw, default_flow_style=False), encoding="utf-8")
    return FleetConfig.load(path)


@pytest.fixture
def record_store(tmp_path: Path):  # noqa: ANN201 — RecordStore context manager
    db_path = str(tmp_path / "record.db")
    run_migrations(db_path)
    with RecordStore(db_path) as store:
        yield store


@pytest.fixture
def summary_store(tmp_path: Path) -> SummaryStore:
    return SummaryStore(str(tmp_path / "summaries"))


def _contributor() -> ContributorRef:
    return ContributorRef(
        scope_id="g_196_parent",
        skill="strata-developer",
        session_id="sess_196",
        ts="2026-10-21T00:00:00+00:00",
    )


def _directive(directive_id: str, content: str) -> Directive:
    return Directive(
        id=directive_id,
        content=content,
        subject="ops",
        source_scope_id="g_196_parent",
        source_skill="strata-developer",
        created_at="2026-10-21T00:00:00+00:00",
    )


def _summary(*directives: Directive) -> ScopeSummary:
    return ScopeSummary(
        scope_id="g_196_parent",
        directives=list(directives),
        context="",
        updated_at="2026-10-21T00:00:00+00:00",
    )


class _ScriptedManager:
    def __init__(self, judgment: ScopeManagerJudgment) -> None:
        self.judgment = judgment

    def judge(self, *, new_contribution, **_kwargs):  # noqa: ANN001, ANN003, ANN201
        return self.judgment


def _amend(fleet, record_store, summary_store) -> None:  # noqa: ANN001
    """Retire two directives and add one. The parent's summary starts with both."""
    summary_store.write(
        "g_196_parent",
        _summary(
            _directive(OLD_A, "Pumps off at 20:00."),
            _directive(OLD_B, "A runbook ships with every service."),
        ),
    )
    run_contribution(
        scope=fleet.get_scope("g_196_parent"),
        stratum=next(s for s in fleet.strata if s.id == "L0"),
        content="Replace both rules with one.",
        proposed_classification="directive",
        subject="ops",
        supersedes=None,
        contributor=_contributor(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=_ScriptedManager(
            ScopeManagerJudgment(
                decision="accept_as_directive",
                reasoning="One rule replaces the two.",
                new_summary=_summary(_directive(NEW, "Dock pumps follow the night order.")),
            )
        ),
        summary_max_words=500,
    )


def _child_events(record_store):  # noqa: ANN001, ANN202
    return record_store.list_change_events(scope_id="g_196_child")


def test_retiring_two_and_adding_one_emits_three_events(fleet, record_store, summary_store) -> None:
    """Two retired ids and one added id are three events, one change id."""
    _amend(fleet, record_store, summary_store)

    events = _child_events(record_store)
    assert len(events) == 3
    assert len({event.change_id for event in events}) == 1
    assert {event.item_id for event in events} == {OLD_A, OLD_B, NEW}
    assert len({event.item_id for event in events}) == 3
    for event in events:
        assert OLD_A in (event.before or "")
        assert OLD_B in (event.before or "")
        assert NEW in (event.after or "")

    def reader(scope_id: str) -> list:
        return record_store.list_change_events(scope_id=scope_id)

    perspective = compose_perspective(
        "g_196_child",
        fleet=fleet,
        summary_store=summary_store,
        change_event_reader=reader,
    )
    # The descendant's perspective lists one input_changes line per event.
    assert len(perspective["input_changes"]) == 3
    assert {line["item_id"] for line in perspective["input_changes"]} == {OLD_A, OLD_B, NEW}


class _CountingManager:
    """Counts judge entry points. One refresh is one call, whichever shape."""

    def __init__(self) -> None:
        self.calls = 0

    def judge(self, **_kwargs):  # noqa: ANN003, ANN201
        self.calls += 1
        return ScopeManagerJudgment(decision="decline", reasoning="Noted.", new_summary=None)

    def judge_batch(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls += 1
        return ScopeManagerBatchJudgment(
            verdicts=[
                BatchVerdict(contribution_id=c.id, decision="decline", reasoning="Noted.")
                for c in kwargs["new_contributions"]
            ],
            new_summary=None,
            change_ids=list(kwargs.get("change_ids") or []),
            hop=kwargs.get("hop", 0),
        )


def test_a_descendant_refreshes_once_per_change_id(fleet, record_store, summary_store) -> None:
    """Three notices, one change id, one stub-judge call."""
    _amend(fleet, record_store, summary_store)
    events = _child_events(record_store)
    assert len(events) == 3
    assert len({event.change_id for event in events}) == 1

    manager = _CountingManager()
    outcome = drain_scope(
        "g_196_child",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
        summary_max_words=500,
    )

    assert outcome.judged is True
    assert manager.calls == 1


def _publish(fleet, record_store, summary_store, content: str, anchors: list[str]):  # noqa: ANN001, ANN202
    manager = MagicMock()
    manager.judge_publication.return_value = PublicationJudgment(
        decision="accept", reasoning="Rests on the inherited rule."
    )
    manager.check_publication_exception.side_effect = ScopeManager(
        client=MagicMock()
    ).check_publication_exception
    return propose_publish(
        "g_196_child",
        content,
        "context",
        "ops",
        anchors,
        ContributorRef(
            scope_id="g_196_child",
            skill="strata-developer",
            session_id="sess_196",
            ts="2026-10-21T00:00:00+00:00",
        ),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )


def test_the_sweep_removes_both_retired_ids(fleet, record_store, summary_store) -> None:
    """Both retired ids are item ids the sweep can see, and both items go."""
    summaries_dir = str(summary_store.summaries_dir)
    summary_store.write(
        "g_196_parent",
        _summary(
            _directive(OLD_A, "Pumps off at 20:00."),
            _directive(OLD_B, "A runbook ships with every service."),
        ),
    )
    summary_store.write(
        "g_196_child",
        ScopeSummary(
            scope_id="g_196_child",
            directives=[],
            context="",
            updated_at="2026-10-21T00:00:00+00:00",
        ),
    )
    _publish(fleet, record_store, summary_store, "The night pump order.", [f"directive:{OLD_A}"])
    _publish(fleet, record_store, summary_store, "The runbook order.", [f"directive:{OLD_B}"])
    assert len(read_publication("g_196_child", summaries_dir=summaries_dir)) == 2
    _amend(fleet, record_store, summary_store)

    removal_ids = {
        event.item_id
        for event in _child_events(record_store)
        if event.kind in ("directive_retired", "directive_superseded")
    }
    assert removal_ids == {OLD_A, OLD_B}

    drain_scope(
        "g_196_child",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=None,
        summary_max_words=500,
    )

    assert read_publication("g_196_child", summaries_dir=summaries_dir) == []


def test_a_reread_shows_no_duplicate_notice_lines(fleet, record_store, summary_store) -> None:
    """_prior_notices keeps a second announce of the same items from adding lines."""
    _amend(fleet, record_store, summary_store)
    events = _child_events(record_store)
    assert len(events) == 3
    change_id = events[0].change_id

    contents = []
    for event in events:
        announced, refreshed = _prior_notices(
            record_store,
            scope_id="g_196_child",
            change_id=change_id,
            item=event.item_id,
        )
        assert announced is True
        assert refreshed is False
        notice = record_store.get_contribution(event.contribution_id)
        assert notice is not None
        contents.append(notice.content)
    assert len(contents) == len(set(contents))

    for event in events:
        emit_change_event(
            fleet=fleet,
            record_store=record_store,
            item=event.item_id,
            kind=event.kind,
            source_scope_id="g_196_parent",
            before=event.before,
            after=event.after,
            wave_ids=[change_id],
            hop=event.hop,
        )

    again = _child_events(record_store)
    assert [event.id for event in again] == [event.id for event in events]

    def reader(scope_id: str) -> list:
        return record_store.list_change_events(scope_id=scope_id)

    first = compose_perspective(
        "g_196_child",
        fleet=fleet,
        summary_store=summary_store,
        change_event_reader=reader,
    )
    second = compose_perspective(
        "g_196_child",
        fleet=fleet,
        summary_store=summary_store,
        change_event_reader=reader,
    )
    assert [line["item_id"] for line in first["input_changes"]] == [
        line["item_id"] for line in second["input_changes"]
    ]
    assert len(first["input_changes"]) == len({line["item_id"] for line in first["input_changes"]})
