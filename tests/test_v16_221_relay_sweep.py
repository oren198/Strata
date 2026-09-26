"""v1.16 #221 — the engine's claim-correction sweep reaches a depth-2 relay.

g_root holds and publishes the claim; g_child gets a verbatim copy of it — either a
TRACKED relay (ADR 0013 D4b's mechanical relay-cascade applies: fixed here to thread
the claim_corrected kind and correcting content through every cascade hop, not only
the first, so a depth-2 reader is not silently downgraded to a bare "withdrawn") or
an INDEPENDENT republish with no relay pointer at all (only the drain-time engine
sweep — the CEO-approved reshape, a single post-judgment step in `drain_scope` — ever
finds this one). g_grandchild (a chain child of g_child) reads g_child's face either
way, and must get exactly one claim_corrected under the SAME change id, whether
g_child's own refresh amends, accepts with no change, declines, or fails outright.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from strata.migrator import run_migrations
from strata.publication import PublishedItem, _write_publication, propose_publish, read_publication
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import BootstrapJudgment, PublicationJudgment, ScopeManagerJudgment
from strata.summary_store import ScopeSummary, SummaryStore

_FLEET_YAML = """
strata:
  - id: L0
    name: Root
    ordinal: 0
  - id: L1
    name: Child
    ordinal: 1
  - id: L2
    name: Grandchild
    ordinal: 2
scopes:
  - id: g_root
    name: Root
    stratum_id: L0
  - id: g_child
    name: Child
    stratum_id: L1
  - id: g_grandchild
    name: Grandchild
    stratum_id: L2
edges:
  - from: g_child
    to: g_root
  - from: g_grandchild
    to: g_child
"""

_CLAIM = "The service listens on port 8443."
_CORRECTION = "Used port 8443, the service refused; the right port is unknown."


class _FakePublicationManager:
    """A scope-manager fake whose judge_publication always accepts (relay seeding)."""

    def judge_publication(self, **kwargs: Any) -> PublicationJudgment:  # noqa: ARG002
        return PublicationJudgment(decision="accept", reasoning="Worth relaying.")

    def judge_bootstrap_publication(self, **kwargs: Any) -> BootstrapJudgment:  # noqa: ARG002
        raise NotImplementedError


def _stratum_for(fleet, scope_id: str):
    scope = fleet.get_scope(scope_id)
    return next(s for s in fleet.strata if s.id == scope.stratum_id)


def _setup(tmp_path: Path, *, use_relay: bool = True):
    """Seed g_root's claim, publish it, give g_child a verbatim copy (a tracked relay
    or an independent republish), then correct it at g_root. Returns (db_path,
    fleet_yaml, summaries_dir, change_id, relay_item_id)."""
    from strata import app
    from strata.fleet_config import FleetConfig

    db_path = str(tmp_path / "test.db")
    fleet_yaml = tmp_path / "fleet.yaml"
    fleet_yaml.write_text(textwrap.dedent(_FLEET_YAML), encoding="utf-8")
    summaries_dir = tmp_path / "summaries"
    run_migrations(db_path)
    fleet = FleetConfig.load(fleet_yaml)
    summary_store = SummaryStore(str(summaries_dir))

    proposer = ContributorRef(
        scope_id="g_root", skill="scope-manager", session_id="setup", ts="2026-01-01T00:00:00Z"
    )
    with RecordStore(db_path) as store:
        target = store.append_contribution(
            scope_id="g_root",
            content=_CLAIM,
            proposed_classification="context",
            subject="service-port",
            supersedes=None,
            contributor=proposer,
        )
        store.record_judgment(
            contribution_id=target.id, decision="accept_as_context", judged_by="scope-manager"
        )
        act = store.append_publication_act(
            scope_id="g_root",
            act="publish",
            kind="context",
            content=_CLAIM,
            subject="service-port",
            anchors=["subject:service-port"],
            withdraws=None,
            trigger=None,
            proposer=proposer,
        )
        store.record_publication_judgment(
            act_id=act.id, decision="accept", judged_by="scope-manager"
        )
        _write_publication(
            "g_root",
            [
                PublishedItem(
                    id=act.id,
                    kind="context",
                    content=_CLAIM,
                    subject="service-port",
                    anchors=["subject:service-port"],
                    published_at="2026-01-01T00:00:00Z",
                )
            ],
            summaries_dir=str(summaries_dir),
        )
        summary_store.write(
            "g_root",
            ScopeSummary(
                scope_id="g_root", directives=[], context=_CLAIM, updated_at="2026-01-01T00:00:00Z"
            ),
        )

        # g_child gets its own verbatim copy of the claim — either a TRACKED relay
        # (relay_scope_id/relay_item_id pointers, ADR 0013 D4b's mechanical cascade
        # applies) or an INDEPENDENT republish with no relay pointer at all (only
        # the drain-time engine sweep, #221's own mechanism, ever finds this one).
        if use_relay:
            relay_outcome = propose_publish(
                "g_child",
                _CLAIM,
                "context",
                "service-port",
                ["subject:service-port"],
                ContributorRef(
                    scope_id="g_child", skill="engineer", session_id="s1", ts="2026-01-01T00:00:00Z"
                ),
                fleet=fleet,
                record_store=store,
                summary_store=summary_store,
                scope_manager=_FakePublicationManager(),
                relay_source_scope_id="g_root",
                relay_source_item_id=act.id,
            )
            assert relay_outcome.decision == "accept"
            relay_item_id = relay_outcome.act_id
        else:
            child_proposer = ContributorRef(
                scope_id="g_child", skill="engineer", session_id="s1", ts="2026-01-01T00:00:00Z"
            )
            child_act = store.append_publication_act(
                scope_id="g_child",
                act="publish",
                kind="context",
                content=_CLAIM,
                subject="service-port",
                anchors=["subject:service-port"],
                withdraws=None,
                trigger=None,
                proposer=child_proposer,
            )
            store.record_publication_judgment(
                act_id=child_act.id, decision="accept", judged_by="scope-manager"
            )
            _write_publication(
                "g_child",
                [
                    PublishedItem(
                        id=child_act.id,
                        kind="context",
                        content=_CLAIM,
                        subject="service-port",
                        anchors=["subject:service-port"],
                        published_at="2026-01-01T00:00:00Z",
                    )
                ],
                summaries_dir=str(summaries_dir),
            )
            relay_item_id = child_act.id
        assert read_publication("g_child", summaries_dir=str(summaries_dir)) != []

    # g_root's own outcome corrects the claim (same-scope), judge omits
    # withdraw_published — the engine withdraws g_root's own item, fanning out to
    # g_child under one change id.
    outcome_judgment = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="Confirmed by observation.",
        new_summary=ScopeSummary(
            scope_id="g_root",
            directives=[],
            context="The service's port is under investigation.",
            updated_at="2026-01-01T00:01:00Z",
        ),
        outcome_disposition="failed_corrected",
    )
    mock_manager = MagicMock()
    mock_manager.judge.return_value = outcome_judgment
    with RecordStore(db_path) as store:
        outcome = store.append_contribution(
            scope_id="g_root",
            content=_CORRECTION,
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=ContributorRef(
                scope_id="g_root", skill="engineer", session_id="s1", ts="2026-01-01T00:01:00Z"
            ),
            acted_on=target.id,
        )
        fleet_ = FleetConfig.load(fleet_yaml)
        summary_store_ = SummaryStore(str(summaries_dir))
        app._judge_and_record(
            contribution=outcome,
            scope=fleet_.get_scope("g_root"),
            stratum=_stratum_for(fleet_, "g_root"),
            fleet=fleet_,
            record_store=store,
            summary_store=summary_store_,
            scope_manager=mock_manager,
            summary_max_words=500,
        )

    with RecordStore(db_path) as store:
        pending = store.list_change_events(scope_id="g_child", unprocessed_only=True)
        assert len(pending) == 1
        assert pending[0].kind == "claim_corrected"
        change_id = pending[0].change_id

        grandchild_claim_corrected = [
            e
            for e in store.list_change_events(scope_id="g_grandchild")
            if e.kind == "claim_corrected"
        ]
        if use_relay:
            # TRACKED relay: ADR 0013 D4b's mechanical cascade, not g_child's own
            # judge, already withdrew it as part of g_root's own correction — and
            # (v1.16 #221 threads the correction kind through every cascade hop, not
            # only the first) that reached g_grandchild too, tagged claim_corrected,
            # under this SAME change id, before g_child's own drain ever runs.
            assert relay_item_id not in {
                i.id for i in read_publication("g_child", summaries_dir=str(summaries_dir))
            }
            assert len(grandchild_claim_corrected) == 1
            assert grandchild_claim_corrected[0].change_id == change_id
        else:
            # INDEPENDENT republish: no relay pointer, so the mechanical cascade
            # never finds it — it is still published, and g_grandchild has heard
            # nothing yet. Only g_child's OWN drain-time engine sweep (#221) will
            # ever catch this one.
            assert relay_item_id in {
                i.id for i in read_publication("g_child", summaries_dir=str(summaries_dir))
            }
            assert grandchild_claim_corrected == []

    return db_path, fleet_yaml, summaries_dir, change_id, relay_item_id


def _drain_child(db_path, fleet_yaml, summaries_dir, *, judge):
    from strata import app
    from strata.fleet_config import FleetConfig

    with RecordStore(db_path) as store:
        fleet_ = FleetConfig.load(fleet_yaml)
        summary_store_ = SummaryStore(str(summaries_dir))
        return app.drain_scope(
            "g_child",
            fleet=fleet_,
            record_store=store,
            summary_store=summary_store_,
            scope_manager=judge,
            summary_max_words=500,
        )


def _mock_judge(fn):
    manager = MagicMock()
    manager.judge.side_effect = fn
    return manager


@pytest.mark.parametrize(
    "outcome",
    ["amend", "accept_no_change", "decline", "judge_error"],
)
@pytest.mark.parametrize("use_relay", [True, False], ids=["tracked_relay", "independent_republish"])
def test_grandchild_gets_one_notice_under_the_original_change_id(
    tmp_path: Path, outcome: str, use_relay: bool
) -> None:
    db_path, fleet_yaml, summaries_dir, change_id, relay_item_id = _setup(
        tmp_path, use_relay=use_relay
    )

    def fake_judge(**kwargs: Any) -> ScopeManagerJudgment:
        assert kwargs["scope"].id == "g_child"
        if outcome == "judge_error":
            raise RuntimeError("judge is unavailable")
        if outcome == "amend":
            return ScopeManagerJudgment(
                decision="accept_as_context",
                reasoning="Noting the correction.",
                new_summary=ScopeSummary(
                    scope_id="g_child",
                    directives=[],
                    context="The service's port is under investigation upstream.",
                    updated_at="2026-01-01T00:02:00Z",
                ),
                change_id=kwargs.get("change_id"),
            )
        if outcome == "accept_no_change":
            return ScopeManagerJudgment(
                decision="accept_as_context",
                reasoning="Nothing to change here.",
                new_summary=ScopeSummary(
                    scope_id="g_child",
                    directives=[],
                    context="",
                    updated_at="2026-01-01T00:02:00Z",
                ),
                change_id=kwargs.get("change_id"),
            )
        assert outcome == "decline"
        return ScopeManagerJudgment(
            decision="decline",
            reasoning="Nothing to admit from this notice.",
            new_summary=None,
            change_id=kwargs.get("change_id"),
        )

    manager = _mock_judge(fake_judge)

    if outcome == "judge_error":
        from strata.app import DrainFailed

        with pytest.raises(DrainFailed):
            _drain_child(db_path, fleet_yaml, summaries_dir, judge=manager)
    else:
        result = _drain_child(db_path, fleet_yaml, summaries_dir, judge=manager)
        assert result.judged is True

    with RecordStore(db_path) as store:
        # g_child's own relayed copy is gone, whatever the refresh judge did.
        assert relay_item_id not in {
            i.id for i in read_publication("g_child", summaries_dir=str(summaries_dir))
        }

        grandchild_events = [
            e
            for e in store.list_change_events(scope_id="g_grandchild")
            if e.kind == "claim_corrected"
        ]
        assert len(grandchild_events) == 1
        assert grandchild_events[0].change_id == change_id
        assert "port is unknown" in grandchild_events[0].after


def test_retry_after_judge_error_does_not_double_notify(tmp_path: Path) -> None:
    """Independent republish (no relay pointer): unlike the tracked-relay case, this
    one is untouched until g_child's OWN drain-time sweep runs — so a failed drain,
    retried, is the meaningful test of not double-notifying."""
    db_path, fleet_yaml, summaries_dir, change_id, relay_item_id = _setup(tmp_path, use_relay=False)

    def failing_judge(**kwargs: Any) -> ScopeManagerJudgment:  # noqa: ARG001
        raise RuntimeError("judge is unavailable")

    from strata.app import DrainFailed

    with pytest.raises(DrainFailed):
        _drain_child(db_path, fleet_yaml, summaries_dir, judge=_mock_judge(failing_judge))

    with RecordStore(db_path) as store:
        first_attempt_events = store.list_change_events(scope_id="g_grandchild")
        assert len(first_attempt_events) == 1

    # Retry: the judge succeeds this time. The events are still unprocessed (the
    # first attempt failed before marking), so the drain retries them — the sweep
    # must not double-notify g_grandchild.
    def succeeding_judge(**kwargs: Any) -> ScopeManagerJudgment:
        return ScopeManagerJudgment(
            decision="decline",
            reasoning="Nothing to admit.",
            new_summary=None,
            change_id=kwargs.get("change_id"),
        )

    result = _drain_child(db_path, fleet_yaml, summaries_dir, judge=_mock_judge(succeeding_judge))
    assert result.judged is True

    with RecordStore(db_path) as store:
        grandchild_events = store.list_change_events(scope_id="g_grandchild")
        assert len(grandchild_events) == 1
        assert grandchild_events[0].change_id == change_id


def test_a_refresh_of_another_kind_gets_no_new_sweep_call(tmp_path: Path, monkeypatch) -> None:
    """A withdrawn (not claim_corrected) refresh must never trigger
    propagate_claim_correction — the sweep is gated strictly on the drained
    event's own kind."""
    from strata import app
    from strata.fleet_config import FleetConfig

    db_path = str(tmp_path / "test.db")
    fleet_yaml = tmp_path / "fleet.yaml"
    fleet_yaml.write_text(textwrap.dedent(_FLEET_YAML), encoding="utf-8")
    summaries_dir = tmp_path / "summaries"
    run_migrations(db_path)

    calls: list[dict] = []
    monkeypatch.setattr(
        app,
        "propagate_claim_correction",
        lambda *a, **k: calls.append(k) or [],  # noqa: ARG005
    )

    with RecordStore(db_path) as store:
        notice = store.append_contribution(
            scope_id="g_child",
            content="[Input change chg_x: item pub_1 was withdrawn.]",
            proposed_classification="context",
            subject="manager-refresh",
            supersedes=None,
            contributor=ContributorRef(
                scope_id="g_child",
                skill="scope-manager",
                session_id="refresh",
                ts="2026-01-01T00:01:00Z",
            ),
        )
        store.append_change_event(
            change_id="chg_x",
            contribution_id=notice.id,
            scope_id="g_child",
            item_id="pub_1",
            kind="withdrawn",
            before="stale claim",
            after=None,
        )

    def fake_judge(**kwargs: Any) -> ScopeManagerJudgment:
        assert kwargs["mode"] == "input_change_refresh"
        return ScopeManagerJudgment(
            decision="decline", reasoning="Nothing to admit.", new_summary=None
        )

    with RecordStore(db_path) as store:
        fleet_ = FleetConfig.load(fleet_yaml)
        summary_store_ = SummaryStore(str(summaries_dir))
        app.drain_scope(
            "g_child",
            fleet=fleet_,
            record_store=store,
            summary_store=summary_store_,
            scope_manager=_mock_judge(fake_judge),
            summary_max_words=500,
        )

    assert calls == []
