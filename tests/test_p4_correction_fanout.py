"""v1.15 P4, ADR 0017 — the correction's fan-out, end to end, cross-scope.

Three scopes: g_root holds and publishes the item; g_reporter (a chain child of
g_root) reports the outcome that corrects it — a cross-scope `acted_on` (P1's
chain-only rule); g_reader (another chain child) reads g_root's publication and is
who the correction must reach once g_root's OWN refresh judge withdraws it.

This is the Strata-repo counterpart of the eval family's item 8 (two readers, no
double notice, bounded), with the cross-scope leg the plan's P4 section and CEO
ruling 2 call for: the correction did not originate in the holding scope itself.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from strata.migrator import run_migrations
from strata.publication import PublishedItem, _write_publication
from strata.record_store import ContributorRef, RecordStore, standing_evidence
from strata.scope_manager import ScopeManagerJudgment
from strata.summary_store import ScopeSummary, SummaryStore

_FLEET_YAML = """
strata:
  - id: L0
    name: Root
    ordinal: 0
  - id: L1
    name: Child
    ordinal: 1
scopes:
  - id: g_root
    name: Root
    stratum_id: L0
  - id: g_reporter
    name: Reporter
    stratum_id: L1
  - id: g_reader
    name: Reader
    stratum_id: L1
edges:
  - from: g_reporter
    to: g_root
  - from: g_reader
    to: g_root
"""


def _stratum_for(fleet, scope_id: str):
    scope = fleet.get_scope(scope_id)
    return next(s for s in fleet.strata if s.id == scope.stratum_id)


def _seed(tmp_path: Path) -> tuple[str, Path, Path]:
    db_path = str(tmp_path / "test.db")
    fleet_yaml = tmp_path / "fleet.yaml"
    fleet_yaml.write_text(textwrap.dedent(_FLEET_YAML), encoding="utf-8")
    summaries_dir = tmp_path / "summaries"
    run_migrations(db_path)
    return db_path, fleet_yaml, summaries_dir


def test_cross_scope_correction_fans_out_to_a_publication_reader_under_one_change_id(
    tmp_path: Path,
) -> None:
    from strata import app
    from strata.fleet_config import FleetConfig

    db_path, fleet_yaml, summaries_dir = _seed(tmp_path)
    fleet = FleetConfig.load(fleet_yaml)
    summary_store = SummaryStore(str(summaries_dir))

    proposer = ContributorRef(
        scope_id="g_root", skill="scope-manager", session_id="setup", ts="2026-01-01T00:00:00Z"
    )
    with RecordStore(db_path) as store:
        target = store.append_contribution(
            scope_id="g_root",
            content="The service listens on port 8443.",
            proposed_classification="context",
            subject="service-port",
            supersedes=None,
            contributor=proposer,
        )
        store.record_judgment(
            contribution_id=target.id, decision="accept_as_context", judged_by="scope-manager"
        )
        # g_root PUBLISHES the item (mechanical seed — the publish act itself is not
        # what this test is about) — g_reader is a reader of this face.
        act = store.append_publication_act(
            scope_id="g_root",
            act="publish",
            kind="context",
            content=target.content,
            subject="service-port",
            anchors=[],
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
                    content=target.content,
                    subject="service-port",
                    anchors=[],
                    published_at="2026-01-01T00:00:00Z",
                )
            ],
            summaries_dir=str(summaries_dir),
        )
        summary_store.write(
            "g_root",
            ScopeSummary(
                scope_id="g_root",
                directives=[],
                context=target.content,
                updated_at="2026-01-01T00:00:00Z",
            ),
        )

    # --- step 1: g_reporter (cross-scope, a chain child of g_root) reports the
    # failed_corrected outcome. --------------------------------------------------
    corrected_content = "Used port 8443, the service refused; the right port is unknown."
    outcome_judgment = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="Confirmed by observation.",
        new_summary=ScopeSummary(
            scope_id="g_reporter",
            directives=[],
            context=corrected_content,
            updated_at="2026-01-01T00:01:00Z",
        ),
        outcome_disposition="failed_corrected",
    )
    mock_manager = MagicMock()
    mock_manager.judge.return_value = outcome_judgment
    with RecordStore(db_path) as store:
        outcome = store.append_contribution(
            scope_id="g_reporter",
            content=corrected_content,
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=ContributorRef(
                scope_id="g_reporter",
                skill="engineer",
                session_id="s1",
                ts="2026-01-01T00:01:00Z",
            ),
            acted_on=target.id,
        )
        fleet_ = FleetConfig.load(fleet_yaml)
        summary_store_ = SummaryStore(str(summaries_dir))
        app._judge_and_record(
            contribution=outcome,
            scope=fleet_.get_scope("g_reporter"),
            stratum=_stratum_for(fleet_, "g_reporter"),
            fleet=fleet_,
            record_store=store,
            summary_store=summary_store_,
            scope_manager=mock_manager,
            summary_max_words=500,
        )

    # g_root did nothing itself — it gets ONE ordinary unprocessed claim_corrected
    # notice, self_notice=0. g_reader gets NOTHING yet — nobody but g_root has
    # necessarily seen this claim, so a reader is told only through g_root's OWN
    # subsequent withdrawal (step 2), never directly from the cross-scope emit
    # (fixed after strata-evals' unpublished-claim control caught the first
    # version over-notifying every chain child and reference-edge scope).
    with RecordStore(db_path) as store:
        holder_events = store.list_change_events(scope_id="g_root", unprocessed_only=True)
        assert len(holder_events) == 1
        assert holder_events[0].kind == "claim_corrected"
        assert holder_events[0].self_notice == 0
        change_id = holder_events[0].change_id

        assert store.list_change_events(scope_id="g_reader") == []

        # P2: not yet replaced — g_root's own summary still carries the exact claim.
        evidence = standing_evidence(store, fleet, target.id, summary_store=summary_store)
        assert len(evidence) == 1
        assert evidence[0].replaced_kind is None
        assert evidence[0].correction_pending_kind == "claim_corrected"

    # --- step 2: g_root's OWN refresh judge reacts by withdrawing the item it
    # published. -------------------------------------------------------------------
    def fake_judge(**kwargs: Any) -> ScopeManagerJudgment:
        assert kwargs["scope"].id == "g_root"
        # The mock stands in for ScopeManager.judge() itself, so — like the real
        # judge() does via _parse_judgment — it must carry the SAME change_id the
        # caller threads in (ADR 0014 D4/D8: the id is a parameter, never a lookup).
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="The service's port claim was corrected; withdrawing the stale face.",
            new_summary=ScopeSummary(
                scope_id="g_root",
                directives=[],
                context="The service's port is under investigation.",
                updated_at="2026-01-01T00:02:00Z",
            ),
            withdraw_published=[act.id],
            change_id=kwargs.get("change_id"),
        )

    mock_manager2 = MagicMock()
    mock_manager2.judge.side_effect = fake_judge
    with RecordStore(db_path) as store:
        fleet_ = FleetConfig.load(fleet_yaml)
        summary_store_ = SummaryStore(str(summaries_dir))
        # drain_scope is THE one refresh mechanism (ADR 0014 D6, implementation pin
        # 6) — the same path both `strata launch`/`strata refresh` and the MCP
        # server's drain-on-read go through, so this exercises the real thing
        # rather than a hand-rolled call.
        app.drain_scope(
            "g_root",
            fleet=fleet_,
            record_store=store,
            summary_store=summary_store_,
            scope_manager=mock_manager2,
            summary_max_words=500,
        )

    with RecordStore(db_path) as store:
        # g_root's own pending row is now processed (drained).
        assert store.list_change_events(scope_id="g_root", unprocessed_only=True) == []

        # g_reader is now told for the FIRST time — exactly once — through g_root's
        # own withdrawal, under the SAME change id the cross-scope notice minted.
        reader_events = store.list_change_events(scope_id="g_reader")
        assert len(reader_events) == 1
        assert reader_events[0].change_id == change_id
        assert reader_events[0].kind == "claim_corrected"
        assert "port is unknown" in reader_events[0].after

        # P2: NOW replaced — g_root's own summary no longer carries the claim.
        evidence = standing_evidence(store, fleet, target.id, summary_store=summary_store)
        assert len(evidence) == 1
        assert evidence[0].replaced_kind == "claim_corrected"
        assert evidence[0].correction_pending_kind is None
