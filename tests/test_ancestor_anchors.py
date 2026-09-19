"""Ancestor directive ids are valid anchors, and are swept mechanically (ADR 0015 D3).

An inherited directive binds the scope. Before ADR 0015 the splice copied the
ancestor's row into the descendant's summary, so anchoring to it happened to
work — the id was, by then, in the descendant's own summary. With the copy
gone (D1) the id is in the ancestor walk instead, and two things follow:

- anchor validation must accept an id from the walk as readily as one from the
  scope's own summary, or a scope loses the ability to publish anything that
  rests on what binds it from above;
- when the ancestor retires that directive, the descendant's published items
  anchored to it must be swept with the same MECHANICAL rule a local removal
  gets (ADR 0007 D3). A rule that is mechanical for one's own retirements and
  judged for an ancestor's would be a second rule.

The sweep runs at the top of the descendant's drain, before its judge, and
needs no judge at all — a keyless server sweeps too.

Vocabulary follows CONTEXT.md verbatim: scope, scope summary, directive,
publication, withdrawal, change event, refresh, record.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from strata.app import drain_scope
from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.publication import (
    propose_publish,
    read_publication,
)
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import PublicationJudgment, ScopeManagerJudgment
from strata.summary_store import Directive, ScopeSummary, SummaryStore

ROOT_DIRECTIVE_ID = "c_root_rule"


def _fleet(tmp_path: Path) -> FleetConfig:
    path = tmp_path / "fleet.yaml"
    path.write_text(
        yaml.dump(
            {
                "strata": [
                    {"id": "L0", "name": "executive", "ordinal": 0},
                    {"id": "L1", "name": "team", "ordinal": 1},
                ],
                "scopes": [
                    {"id": "g_root", "name": "Root", "stratum_id": "L0"},
                    {"id": "g_child", "name": "Child", "stratum_id": "L1"},
                ],
                "edges": [{"from": "g_child", "to": "g_root"}],
            }
        ),
        encoding="utf-8",
    )
    return FleetConfig.load(path)


@pytest.fixture()
def env(tmp_path: Path):
    """A root holding one directive, a child holding one of its own."""
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    record_store = RecordStore(db_path)
    summary_store = SummaryStore(str(tmp_path / "summaries"))

    summary_store.write(
        "g_root",
        ScopeSummary(
            scope_id="g_root",
            directives=[
                Directive(
                    id=ROOT_DIRECTIVE_ID,
                    content="Every service ships with a runbook.",
                    subject="ops",
                    source_scope_id="g_root",
                    source_skill="scope-manager",
                    created_at="2026-09-01T09:00:00+00:00",
                )
            ],
            context="Root working note.",
            updated_at="2026-09-01T09:00:00+00:00",
        ),
    )
    summary_store.write(
        "g_child",
        ScopeSummary(
            scope_id="g_child",
            directives=[
                Directive(
                    id="c_child_rule",
                    content="Runbooks live in the repo.",
                    subject="ops",
                    source_scope_id="g_child",
                    source_skill="scope-manager",
                    created_at="2026-09-01T10:00:00+00:00",
                )
            ],
            context="Child working note.",
            updated_at="2026-09-01T10:00:00+00:00",
        ),
    )

    yield _fleet(tmp_path), record_store, summary_store
    record_store.close()


def _proposer() -> ContributorRef:
    return ContributorRef(
        scope_id="g_child",
        skill="strata-developer",
        session_id="sess_test",
        ts="2026-09-01T10:00:00+00:00",
    )


def _accepting_manager() -> MagicMock:
    manager = MagicMock()
    manager.judge_publication.return_value = PublicationJudgment(
        decision="accept", reasoning="Rests on what binds this scope."
    )
    return manager


def _publish(fleet, record_store, summary_store, anchors: list[str]):
    return propose_publish(
        "g_child",
        "Our runbook template is checked in.",
        "context",
        "ops",
        anchors,
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=_accepting_manager(),
    )


def _root_retires(record_store, summary_store) -> None:
    """The root retires its directive and the child is told (ADR 0014 D1)."""
    root = summary_store.read("g_root")
    summary_store.write("g_root", root.model_copy(update={"directives": []}))
    record_store.append_change_notice(
        scope_id="g_child",
        content=f"[Input change chg_r: directive {ROOT_DIRECTIVE_ID} was retired at g_root.]",
        contributor=ContributorRef(
            scope_id="g_child",
            skill="scope-manager",
            session_id="refresh",
            ts="2026-09-01T11:00:00+00:00",
        ),
        change_id="chg_r",
        source_scope_id="g_root",
        item_id=ROOT_DIRECTIVE_ID,
        kind="directive_retired",
        before=ROOT_DIRECTIVE_ID,
        after=None,
    )


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------


def test_an_ancestor_directive_id_is_a_valid_anchor(env) -> None:
    """It binds the scope, so a published item may rest on it (ADR 0015 D3)."""
    fleet, record_store, summary_store = env

    outcome = _publish(fleet, record_store, summary_store, [f"directive:{ROOT_DIRECTIVE_ID}"])

    assert outcome.decision == "accept"
    published = read_publication("g_child", summaries_dir=str(summary_store.summaries_dir))
    assert published[0].anchors == [f"directive:{ROOT_DIRECTIVE_ID}"]


def test_a_bare_ancestor_directive_id_is_tagged_as_a_directive_anchor(env) -> None:
    """Auto-classification reads the walk too — otherwise the id would silently
    become a free-form ``subject:`` anchor, and nothing would ever sweep it."""
    fleet, record_store, summary_store = env

    _publish(fleet, record_store, summary_store, [ROOT_DIRECTIVE_ID])

    published = read_publication("g_child", summaries_dir=str(summary_store.summaries_dir))
    assert published[0].anchors == [f"directive:{ROOT_DIRECTIVE_ID}"]


def test_an_unknown_directive_anchor_is_still_refused(env) -> None:
    """Widening the id space is not abandoning the check."""
    fleet, record_store, summary_store = env

    with pytest.raises(ValueError, match="c_nobody"):
        _publish(fleet, record_store, summary_store, ["directive:c_nobody"])


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def test_an_ancestors_retirement_withdraws_the_descendants_item(env) -> None:
    """Mechanical, because the local case is (ADR 0007 D3, ADR 0015 D3)."""
    fleet, record_store, summary_store = env
    _publish(fleet, record_store, summary_store, [f"directive:{ROOT_DIRECTIVE_ID}"])
    _root_retires(record_store, summary_store)

    drain_scope(
        "g_child",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        # No judge at all: the sweep owes the judge nothing, so a keyless
        # server sweeps exactly as a keyed one does.
        scope_manager=None,
        summary_max_words=500,
    )

    assert read_publication("g_child", summaries_dir=str(summary_store.summaries_dir)) == []
    withdrawals = [
        act
        for act in record_store.list_publication_acts(scope_id="g_child")
        if act.act == "withdraw"
    ]
    assert len(withdrawals) == 1
    # A mechanical consequence of an already-judged event carries its trigger
    # and no judgment row of its own.
    assert withdrawals[0].trigger is not None
    assert record_store.get_publication_judgment(withdrawals[0].id) is None


def test_an_item_with_a_surviving_subject_anchor_is_not_swept(env) -> None:
    """An item lives while ANY anchor lives — the ancestor case is no exception."""
    fleet, record_store, summary_store = env
    _publish(
        fleet,
        record_store,
        summary_store,
        [f"directive:{ROOT_DIRECTIVE_ID}", "subject:runbooks"],
    )
    _root_retires(record_store, summary_store)

    drain_scope(
        "g_child",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=None,
        summary_max_words=500,
    )

    assert len(read_publication("g_child", summaries_dir=str(summary_store.summaries_dir))) == 1


def test_the_sweep_runs_before_the_judge(env) -> None:
    """The judge is shown the swept publication, never asked to do the sweeping."""
    fleet, record_store, summary_store = env
    _publish(fleet, record_store, summary_store, [f"directive:{ROOT_DIRECTIVE_ID}"])
    _root_retires(record_store, summary_store)

    seen: list[list] = []

    def fake_judge(**kwargs):
        seen.append(list(kwargs["current_publication"]))
        return ScopeManagerJudgment(
            decision="decline",
            reasoning="Nothing here still rests on the retired directive.",
            new_summary=None,
        )

    manager = MagicMock()
    manager.judge.side_effect = fake_judge

    drain_scope(
        "g_child",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
        summary_max_words=500,
    )

    assert seen and seen[0] == []


def test_a_second_drain_sweeps_nothing_more(env) -> None:
    """Idempotent: the item is already gone, so there is nothing left to withdraw."""
    fleet, record_store, summary_store = env
    _publish(fleet, record_store, summary_store, [f"directive:{ROOT_DIRECTIVE_ID}"])
    _root_retires(record_store, summary_store)

    for _ in range(2):
        drain_scope(
            "g_child",
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=None,
            summary_max_words=500,
        )

    withdrawals = [
        act
        for act in record_store.list_publication_acts(scope_id="g_child")
        if act.act == "withdraw"
    ]
    assert len(withdrawals) == 1


def test_an_unaffected_item_survives_an_ancestor_retirement(env) -> None:
    """Only the items anchored to the removed id are swept."""
    fleet, record_store, summary_store = env
    _publish(fleet, record_store, summary_store, ["directive:c_child_rule"])
    _root_retires(record_store, summary_store)

    drain_scope(
        "g_child",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=None,
        summary_max_words=500,
    )

    assert len(read_publication("g_child", summaries_dir=str(summary_store.summaries_dir))) == 1


def test_a_publication_written_by_hand_is_untouched_without_a_retirement(env) -> None:
    """No pending removal event, nothing swept — the drain is not a garbage collector."""
    fleet, record_store, summary_store = env
    _publish(fleet, record_store, summary_store, [f"directive:{ROOT_DIRECTIVE_ID}"])

    drain_scope(
        "g_child",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=None,
        summary_max_words=500,
    )

    assert len(read_publication("g_child", summaries_dir=str(summary_store.summaries_dir))) == 1
