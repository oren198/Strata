"""v1.15 P4, ADR 0017 — a reader reached by TWO paths still gets ONE claim_corrected.

`affected_scopes`'s claim_corrected branch unions chain_children(source) with
referenced_by(source) (see change_events.py) — a scope that is BOTH a chain child
of the holding scope AND separately holds an explicit reference edge to it appears
in both sets, but a Python set union de-duplicates before the row is ever written.
This pins that de-duplication end to end, through the ENGINE's own withdrawal of a
verbatim published carrier (the same live-finding shape P4's other tests exercise),
not just at the pure-function level `test_change_events.py` already covers.

The default fixture fleets elsewhere (test_change_events.py, test_p4_correction_
fanout.py) have no scope that is both a chain child and a reference reader of the
same source, so this file builds its own small one.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

from strata.migrator import run_migrations
from strata.publication import PublishedItem, _write_publication, read_publication
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import ScopeManagerJudgment
from strata.summary_store import ScopeSummary, SummaryStore

# g_reader is BOTH a chain child of g_root (the untyped edge resolves to chain,
# adjacent strata) AND holds a SEPARATE, explicitly-typed reference edge to it —
# two distinct edges, same pair, so it appears once via chain_children and once
# via referenced_by before affected_scopes' set union collapses it to one.
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
  - id: g_reader
    name: TwoPathReader
    stratum_id: L1
edges:
  - from: g_reader
    to: g_root
  - from: g_reader
    to: g_root
    kind: reference
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


def test_claim_corrected_reaches_a_two_path_reader_once(tmp_path: Path) -> None:
    from strata import app
    from strata.fleet_config import FleetConfig

    db_path, fleet_yaml, summaries_dir = _seed(tmp_path)
    fleet = FleetConfig.load(fleet_yaml)

    # Precondition: g_reader really is reachable both ways.
    assert "g_reader" in [s.id for s in fleet.chain_children("g_root")]
    assert "g_reader" in [s.id for s in fleet.referenced_by("g_root")]

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

    # Same-scope failed_corrected outcome, judge OMITS withdraw_published — the
    # engine must find and withdraw the verbatim-carrying published item itself.
    corrected_content = "Used port 8443, the service refused; the right port is unknown."
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
        # Deliberately omitted: withdraw_published=[act.id]
    )
    mock_manager = MagicMock()
    mock_manager.judge.return_value = outcome_judgment
    with RecordStore(db_path) as store:
        outcome = store.append_contribution(
            scope_id="g_root",
            content=corrected_content,
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
        assert read_publication("g_root", summaries_dir=str(summaries_dir)) == []

        reader_events = store.list_change_events(scope_id="g_reader")
        assert len(reader_events) == 1
        assert reader_events[0].kind == "claim_corrected"
        assert "port is unknown" in reader_events[0].after
        change_id = reader_events[0].change_id

        # One refresh after draining: the drain marks it processed, and no second
        # row for this change id ever appears — the once-per-(change id, scope)
        # rule holds regardless of how many paths named the scope.
        fleet_ = FleetConfig.load(fleet_yaml)
        summary_store_ = SummaryStore(str(summaries_dir))
        summary_store_.write(
            "g_reader",
            ScopeSummary(
                scope_id="g_reader",
                directives=[],
                context="",
                updated_at="2026-01-01T00:00:00Z",
            ),
        )
        refresh_judgment = ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="Noted the correction.",
            new_summary=ScopeSummary(
                scope_id="g_reader",
                directives=[],
                context="The service's port is under investigation.",
                updated_at="2026-01-01T00:02:00Z",
            ),
        )
        mock_reader_manager = MagicMock()
        mock_reader_manager.judge.return_value = refresh_judgment
        outcome_result = app.drain_scope(
            "g_reader",
            fleet=fleet_,
            record_store=store,
            summary_store=summary_store_,
            scope_manager=mock_reader_manager,
            summary_max_words=500,
        )
        assert outcome_result.judged is True
        assert mock_reader_manager.judge.call_count == 1

        assert store.list_change_events(scope_id="g_reader", unprocessed_only=True) == []
        reader_events_after = store.list_change_events(scope_id="g_reader")
        assert len(reader_events_after) == 1
        assert reader_events_after[0].change_id == change_id
