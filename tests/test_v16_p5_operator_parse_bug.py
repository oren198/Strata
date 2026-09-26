"""v1.16 P5 — regression: the live gate found `_parse_judgment` never routed an
operator-directive target's decision through `_resolve_acted_on_decision` at all
(the gate checked only `acted_on`, never `acted_on_operator_item`), and would
also have wrongly forced a RAISED contribution's own genuine accept through the
same narrowed resolver (the gate never excluded `raised_from` either).

Both regressions are reproduced with the REAL ``ScopeManager`` and REAL
``_parse_judgment`` — only the underlying Anthropic client is mocked, returning
the exact raw tool_use shape captured from a live qwen3-via-Novita run against
p5-02 (`traces/joutcome_loop-live-20260926T020018Z` in strata-evals-p5,
2026-09-26). No ``ScopeManagerJudgment`` is ever handed to the app directly —
that would bypass the very parse code that was broken.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager
from strata.migrator import run_migrations
from strata.operator import operator_publish
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import ScopeManager
from strata.settings import Settings

_FLEET_YAML = """
strata:
  - id: L0
    name: Executive
    ordinal: 0
  - id: L1
    name: Function
    ordinal: 1

scopes:
  - id: g_parent
    name: Parent
    stratum_id: L0
    status: active
  - id: g_child
    name: Child
    stratum_id: L1
    status: active

edges:
  - from: g_child
    to: g_parent
"""

_CONTRIBUTOR_BODY = {
    "scope_id": "g_child",
    "skill": "on-call-engineer",
    "session_id": "sess-p5-child",
    "ts": "2026-09-26T09:05:00Z",
}


def _tool_use_response(**payload) -> MagicMock:
    """The exact shape `ScopeManager.judge` reads off an Anthropic response:
    one tool_use content block carrying the raw fields the model returned."""
    block = MagicMock()
    block.type = "tool_use"
    block.input = payload
    resp = MagicMock()
    resp.content = [block]
    return resp


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    summaries_dir = str(tmp_path / "summaries")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")
    run_migrations(db_path)
    Path(fleet_yaml_path).write_text(_FLEET_YAML, encoding="utf-8")
    settings = Settings(
        db_path=db_path,
        summaries_dir=summaries_dir,
        fleet_yaml_path=fleet_yaml_path,
        manager_model="claude-haiku-4-5",
        anthropic_api_key="test-key",
    )
    application = create_app(settings=settings)

    # A REAL ScopeManager — only its underlying Anthropic client is mocked, so
    # `_parse_judgment` and the tool-schema selection both run for real.
    mock_anthropic = MagicMock()
    real_manager = ScopeManager(client=mock_anthropic, model="claude-haiku-4-5")
    application.dependency_overrides[get_scope_manager] = lambda: real_manager

    with TestClient(application) as tc:
        tc.db_path = db_path  # type: ignore[attr-defined]
        tc.summaries_dir = summaries_dir  # type: ignore[attr-defined]
        tc.mock_anthropic = mock_anthropic  # type: ignore[attr-defined]
        yield tc


def test_a_live_shaped_failed_verdict_on_an_operator_directive_writes_evidence(
    client,
) -> None:
    """Regression for the missing operator_evidence row (live gate, p5-02,
    8/10 reps): the model's raw tool_use `decision` was the literal string
    "failed" -- exactly what the narrowed {held, failed, decline} tool offers
    -- and the OLD `_parse_judgment` gate (checking only `.acted_on`, never
    `.acted_on_operator_item`) silently took the ORDINARY parse branch,
    passing "failed" through as `decision` unresolved and losing
    `outcome_disposition` entirely, so the raise never fired."""
    fleet = __import__("strata.fleet_config", fromlist=["FleetConfig"]).FleetConfig.load(
        Path(client.db_path).parent / "fleet.yaml"
    )
    store = RecordStore(client.db_path)
    item = operator_publish(
        "g_child",
        "Every deploy must page the sev-1 rotation through the primary pager before rolling back.",
        "sev1-paging-rotation-operator",
        record_store=store,
        summaries_dir=client.summaries_dir,
        fleet=fleet,
    )

    # The exact raw shape captured live: `decision` is the literal narrowed
    # value the operator-directive tool offers, `new_context` a plain string,
    # `directive_ops` empty -- an ordinary, well-formed tool_use call.
    client.mock_anthropic.messages.create.return_value = _tool_use_response(
        decision="failed",
        reasoning=(
            "The report constitutes a failure of the directive's execution: the "
            "page was sent but never reached anyone. This is an observed outcome "
            "that qualifies as 'failed' under the outcome reporting rule."
        ),
        directive_ops=[],
        new_context=(
            "The on-call rotation attempted to page the primary pager per the "
            "directive, but the page never reached anyone."
        ),
    )

    resp = client.post(
        "/contribute",
        json={
            "scope_id": "g_child",
            "content": (
                "I paged the sev-1 through the primary rotation before rolling "
                "back and it went wrong: the page never reached anyone."
            ),
            "proposed_classification": "context",
            "subject": "sev1-paging-rotation-operator",
            "contributor": _CONTRIBUTOR_BODY,
            "acted_on": item.id,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["judgment"]["decision"] == "accept_as_context"
    outcome_id = resp.json()["contribution_id"]

    evidence = store.list_operator_evidence(operator_item_id=item.id)
    assert len(evidence) == 1
    assert evidence[0].raised_from == outcome_id
    assert "went wrong" in evidence[0].content


def test_contribute_never_500s_on_an_operator_acted_on_live_verdict(client) -> None:
    """Regression for the live 2/10 500s: whatever the model's raw `decision`
    value legitimately is (here "held", the narrowed tool's other non-decline
    value), `/contribute` must return 200 -- never surface an unhandled parse
    exception as a 500."""
    fleet = __import__("strata.fleet_config", fromlist=["FleetConfig"]).FleetConfig.load(
        Path(client.db_path).parent / "fleet.yaml"
    )
    store = RecordStore(client.db_path)
    item = operator_publish(
        "g_child",
        "Every deploy must page the sev-1 rotation through the primary pager before rolling back.",
        "sev1-paging-rotation-operator",
        record_store=store,
        summaries_dir=client.summaries_dir,
        fleet=fleet,
    )

    client.mock_anthropic.messages.create.return_value = _tool_use_response(
        decision="held",
        reasoning=(
            'Confirmed: "paged the primary rotation, on-call responded within 2 '
            'minutes" -- an action that could have failed, and did not.'
        ),
        directive_ops=[],
        new_context="Paging the sev-1 rotation through the primary pager works as directed.",
    )

    resp = client.post(
        "/contribute",
        json={
            "scope_id": "g_child",
            "content": "Paged the sev-1 through the primary rotation; on-call responded promptly.",
            "proposed_classification": "context",
            "subject": "sev1-paging-rotation-operator",
            "contributor": _CONTRIBUTOR_BODY,
            "acted_on": item.id,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["judgment"]["decision"] == "accept_as_context"
    assert store.list_operator_evidence(operator_item_id=item.id) == []


def test_a_raised_contributions_own_genuine_accept_is_not_forced_through_declined(
    client,
) -> None:
    """Second-order regression: the same missing `raised_from` exclusion meant a
    RAISED contribution's own genuine accept_as_context verdict at the issuing
    scope -- an ORDINARY decision value the ordinary tool offers, never one of
    the narrowed disposition strings -- was wrongly routed through
    `_resolve_acted_on_decision` too (since `.acted_on` is set on a raised
    contribution as a record fact), rejected as malformed, and forced to
    `decline` after the one retry. Reproduced directly via `record_judgment_and_raise`
    + a synchronous real judge call, mirroring app.py's own §B sync-judge path."""
    from strata.record_store import RaisedContributionInput
    from strata.summary_store import SummaryStore

    fleet = __import__("strata.fleet_config", fromlist=["FleetConfig"]).FleetConfig.load(
        Path(client.db_path).parent / "fleet.yaml"
    )
    store = RecordStore(client.db_path)
    directive_id = store.append_contribution(
        scope_id="g_parent",
        content="Page the sev-1 rotation through the primary pager.",
        proposed_classification="directive",
        subject="sev1-paging-rotation",
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_parent", skill="eng-lead", session_id="s1", ts="2026-01-01T00:00:00Z"
        ),
    ).id
    store.record_judgment(
        contribution_id=directive_id, decision="accept_as_directive", judged_by="scope-manager"
    )
    outcome_id = store.append_contribution(
        scope_id="g_child",
        content="Paged the secondary rotation directly; it went wrong.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_child", skill="on-call-engineer", session_id="s2", ts="2026-01-01T00:00:00Z"
        ),
    ).id
    _judgment, raised = store.record_judgment_and_raise(
        contribution_id=outcome_id,
        decision="accept_as_context",
        judged_by="scope-manager",
        raise_contribution=RaisedContributionInput(
            scope_id="g_parent",
            content=(
                f"evidence from g_child: following {directive_id} went wrong: "
                "paged the wrong rotation."
            ),
            subject=None,
            contributor=ContributorRef(
                scope_id="g_child",
                skill="on-call-engineer",
                session_id="s2",
                ts="2026-01-01T00:00:00Z",
            ),
            acted_on=directive_id,
            raised_from=outcome_id,
        ),
    )

    client.mock_anthropic.messages.create.return_value = _tool_use_response(
        decision="accept_as_context",
        reasoning="A genuine report of a real deviation from the directive; kept as context.",
        directive_ops=[],
        new_context="A report noted that paging went to the wrong rotation once.",
    )

    fleet_ = fleet
    parent_scope = fleet_.get_scope("g_parent")
    parent_stratum = next(s for s in fleet_.strata if s.id == parent_scope.stratum_id)
    manager = client.app.dependency_overrides[get_scope_manager]()
    result = manager.judge(
        scope=parent_scope,
        stratum=parent_stratum,
        current_summary=SummaryStore(client.summaries_dir).read("g_parent"),
        recent_contributions=[],
        new_contribution=raised,
    )
    assert result.decision == "accept_as_context"
