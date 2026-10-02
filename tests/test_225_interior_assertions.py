"""#225 — interior assertions about another scope (ADR 0016).

Offline: a REAL `ScopeManager`, only the underlying Anthropic client mocked —
the FIRST response is an ordinary accept naming another fleet scope this
scope is not entitled to; the SECOND (when reached) is the one targeted
re-ask's own response.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from strata.fleet_config import EntitlementView, Scope, Stratum
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import ScopeManager
from strata.summary_store import Directive, ScopeSummary

from .test_scope_manager import _fake_response

STRATUM = Stratum(id="L1", name="function", ordinal=1)
PARENT = Scope(id="g_225parent", name="225-parent", stratum_id="L0")
SCOPE = Scope(id="g_225self", name="225-self", stratum_id="L1")
OTHER = Scope(id="g_225other", name="225-other-scope", stratum_id="L1")
PEER = Scope(id="g_225peer", name="225-peer", stratum_id="L1")

CONTRIBUTOR = ContributorRef(
    scope_id="g_225reporter",
    skill="on-call-engineer",
    session_id="sess_225",
    ts="2026-10-02T10:00:00+00:00",
)

EXISTING_DIRECTIVE = Directive(
    id="c_225old",
    content="Use the shared logging format.",
    subject="logging",
    source_scope_id=SCOPE.id,
    source_skill="architect",
    created_at="2026-09-01T09:00:00+00:00",
)

CURRENT_SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[EXISTING_DIRECTIVE],
    context="The team favours minimal abstractions.",
    updated_at="2026-09-01T09:00:00+00:00",
)

ENTITLEMENT = EntitlementView(
    chain=[PARENT, SCOPE], descendants=[], referenced_peers=[PEER], others=[OTHER]
)


def _contribution(content: str, *, cid: str = "c_225new") -> Contribution:
    return Contribution(
        id=cid,
        scope_id=SCOPE.id,
        content=content,
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=CONTRIBUTOR,
        created_at="2026-10-02T09:00:00+00:00",
    )


def _ordinary_accept(content: str, **extra) -> MagicMock:
    payload = {
        "decision": "accept_as_context",
        "reasoning": "Accepting this observation.",
        "directive_ops": [],
        "new_context": f"{CURRENT_SUMMARY.context} {content}",
    }
    payload.update(extra)
    return _fake_response(payload)


def _judge(mock_client: MagicMock, content: str, *, entitlement=ENTITLEMENT) -> tuple:
    manager = ScopeManager(client=mock_client)
    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=_contribution(content),
        entitlement=entitlement,
    )
    return judgment, manager


def _reask_response(**payload) -> MagicMock:
    payload.setdefault("reasoning", "Classified.")
    return _fake_response(payload)


# ---------------------------------------------------------------------------
# Non-triggers: self, ancestor, entitled peer, alias text
# ---------------------------------------------------------------------------


def test_mentioning_this_scope_itself_does_not_trigger() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _ordinary_accept(
        "225-self decided to keep the current approach."
    )
    judgment, _ = _judge(mock_client, "225-self decided to keep the current approach.")
    assert mock_client.messages.create.call_count == 1
    assert not any("interior assertion" in n for n in judgment.protocol_notes)


def test_mentioning_an_ancestor_does_not_trigger() -> None:
    mock_client = MagicMock()
    content = "225-parent only approves changes under budget."
    mock_client.messages.create.return_value = _ordinary_accept(content)
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 1
    assert not any("interior assertion" in n for n in judgment.protocol_notes)


def test_mentioning_an_entitled_referenced_peer_does_not_trigger() -> None:
    mock_client = MagicMock()
    content = "225-peer only approves changes under budget."
    mock_client.messages.create.return_value = _ordinary_accept(content)
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 1
    assert not any("interior assertion" in n for n in judgment.protocol_notes)


def test_alias_text_does_not_trigger() -> None:
    """No fuzzy or alias matching (CEO condition 3) — only a literal
    occurrence of the id or name itself triggers."""
    mock_client = MagicMock()
    content = "the other team only approves changes under budget."
    mock_client.messages.create.return_value = _ordinary_accept(content)
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 1
    assert not any("interior assertion" in n for n in judgment.protocol_notes)


def test_a_decline_never_triggers() -> None:
    """Only an ACCEPT triggers — a decline has nothing to verify."""
    mock_client = MagicMock()
    content = "225-other-scope only approves changes under budget."
    mock_client.messages.create.return_value = _fake_response(
        {
            "decision": "decline",
            "reasoning": "Manufactured attribution.",
            "directive_ops": [],
            "new_context": None,
        }
    )
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 1
    assert judgment.decision == "decline"


# ---------------------------------------------------------------------------
# Triggers: each classification path
# ---------------------------------------------------------------------------


def test_conduct_is_admitted_as_judged_unchanged() -> None:
    content = "225-other-scope's agent rejected our request twice this week."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="conduct"),
    ]
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_context"
    assert judgment.new_context == f"{CURRENT_SUMMARY.context} {content}"
    assert any("interior assertion" in n and "conduct" in n for n in judgment.protocol_notes)


def test_none_classification_is_declined_as_manufactured_attribution() -> None:
    content = "225-other-scope has decided to freeze the budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="none"),
    ]
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "decline"
    assert judgment.new_summary is None
    assert "Manufactured attribution" in judgment.reasoning
    assert any("none" in n for n in judgment.protocol_notes)


def test_publication_with_a_visible_ref_id_is_admitted() -> None:
    content = "225-other-scope published a note about its new policy."
    mock_client = MagicMock()
    published = [_published_item("pub_225")]
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="publication", ref_id="pub_225"),
    ]
    manager = ScopeManager(client=mock_client)
    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=_contribution(content),
        entitlement=ENTITLEMENT,
        current_publication=published,
    )
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_context"
    assert any("publication" in n for n in judgment.protocol_notes)


def test_publication_with_an_invisible_ref_id_is_declined() -> None:
    content = "225-other-scope published a note about its new policy."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="publication", ref_id="pub_nonexistent"),
    ]
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "decline"
    assert "not visible" in judgment.reasoning
    assert any("ref not visible" in n for n in judgment.protocol_notes)


def test_directive_with_a_visible_ancestor_directive_id_is_admitted() -> None:
    content = "225-parent's policy is that 225-other-scope owns the budget decision."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="directive", ref_id="c_225ancestor_dir"),
    ]
    manager = ScopeManager(client=mock_client)
    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=_contribution(content),
        entitlement=ENTITLEMENT,
        ancestor_directives=[
            (
                PARENT.id,
                [
                    Directive(
                        id="c_225ancestor_dir",
                        content="225-other-scope owns the budget decision.",
                        subject="budget",
                        source_scope_id=PARENT.id,
                        source_skill="architect",
                        created_at="2026-01-01T00:00:00+00:00",
                    )
                ],
            )
        ],
    )
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_context"
    assert any("directive" in n for n in judgment.protocol_notes)


def test_invented_informant_span_is_declined() -> None:
    content = "225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="informant", informant_span="Nobody Real"),
    ]
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "decline"
    assert "invented informant" in judgment.reasoning
    assert any("invented informant" in n for n in judgment.protocol_notes)


def test_verified_informant_replaces_context_and_strips_directive_ops() -> None:
    content = "Jordan Lee from 225-other-scope told me they only approve changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(
            {
                "decision": "accept_as_directive",
                "reasoning": "Treating this as binding.",
                "directive_ops": [{"op": "append"}],
                "new_context": None,
            }
        ),
        _reask_response(
            classification="informant", informant_span="Jordan Lee from 225-other-scope"
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 2
    # Forced to context — an informant's word is never a directive.
    assert judgment.decision == "accept_as_context"
    assert judgment.directive_ops == []
    assert judgment.replaced_context is None  # first judgment's new_context was None
    assert "on-call-engineer (g_225reporter) reports that" in judgment.new_context
    assert "Jordan Lee from 225-other-scope" in judgment.new_context
    assert content in judgment.new_context
    assert judgment.new_summary is not None
    assert judgment.new_summary.context == judgment.new_context
    # The existing directive survives untouched — stripping only drops the
    # NEW append op, never carries-across directives.
    assert judgment.new_summary.directives == [EXISTING_DIRECTIVE]
    assert any("informant" in n and "context replaced" in n for n in judgment.protocol_notes)


def test_unreadable_reask_response_fails_closed_as_judge_failure() -> None:
    content = "225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="not-a-real-value"),
    ]
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "decline"
    assert judgment.judge_failure is True
    assert "judge failure" in judgment.reasoning
    assert any("judge failure" in n for n in judgment.protocol_notes)


def test_informant_with_no_span_at_all_fails_closed_as_judge_failure() -> None:
    content = "225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="informant"),  # no informant_span
    ]
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "decline"
    assert judgment.judge_failure is True


def _published_item(item_id: str):
    item = MagicMock()
    item.id = item_id
    return item
