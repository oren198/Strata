"""#212 (removals): backstop 1, the basis-check re-ask, no longer exists. A note that names a
REFERENCED PEER gets no interior-assertion re-ask whether or not the record gives this scope a
basis to speak of it; the #225 re-ask for a scope that is NOT entitled to the named scope is
unchanged (the original tool, no third answer).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from strata import scope_manager as sm
from strata.fleet_config import EntitlementView, Scope, Stratum
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import ScopeManager
from strata.summary_store import ScopeSummary

from .test_scope_manager import _fake_response

STRATUM = Stratum(id="L1", name="function", ordinal=1)
PARENT = Scope(id="g_212parent", name="212-parent", stratum_id="L0")
SCOPE = Scope(id="g_212self", name="212-self", stratum_id="L1")
PEER = Scope(id="g_212peer", name="212-peer", stratum_id="L1")
PEER2 = Scope(id="g_212peer2", name="212-peer-two", stratum_id="L1")
OTHER = Scope(id="g_212other", name="212-other-scope", stratum_id="L1")
CONTRIBUTOR = ContributorRef(
    scope_id="g_212reporter", skill="engineer", session_id="s212", ts="2026-11-23T10:00:00+00:00"
)
SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[],
    context="The team favours minimal abstractions.",
    updated_at="2026-09-01T09:00:00+00:00",
)
ENT = EntitlementView(
    chain=[PARENT, SCOPE], descendants=[], referenced_peers=[PEER, PEER2], others=[OTHER]
)
CLAIM = "212-peer only approves refunds under fifty euros."
OWN_ACT = "Their agent sent back our refund request twice last week."


def _contribution(content: str) -> Contribution:
    return Contribution(
        id="c_212new",
        scope_id=SCOPE.id,
        content=content,
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=CONTRIBUTOR,
        created_at="2026-11-23T09:00:00+00:00",
    )


def _accept(content: str) -> MagicMock:
    return _fake_response(
        {
            "decision": "accept_as_context",
            "reasoning": "Accepting this observation.",
            "directive_ops": [],
            "new_context": f"{SUMMARY.context} {content}",
        }
    )


def _answer(**payload) -> MagicMock:
    payload.setdefault("reasoning", "Classified.")
    return _fake_response(payload)


def _published(peer_id: str) -> tuple[str, list]:
    item = SimpleNamespace(
        id="p_212item",
        kind="context",
        content="Refunds follow the standard schedule.",
        subject=None,
        anchors=[],
        published_at="2026-11-01T00:00:00+00:00",
        origin_scope_id=None,
        relay_scope_id=None,
        relay_item_id=None,
    )
    return (peer_id, [item])


def _judge(responses: list, content: str, **extra):
    client = MagicMock()
    client.messages.create.side_effect = responses
    judgment = ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=SUMMARY,
        recent_contributions=[],
        new_contribution=_contribution(content),
        entitlement=ENT,
        **extra,
    )
    return judgment, client


def _tool_names(client) -> list[str]:
    return [c.kwargs["tools"][0]["name"] for c in client.messages.create.call_args_list]


def _reask_enum(client) -> list[str]:
    tool = client.messages.create.call_args_list[1].kwargs["tools"][0]
    return tool["input_schema"]["properties"]["classification"]["enum"]


# --- shipped behaviour -------------------------------------------------------------


def test_a_named_referenced_peer_gets_no_re_ask_with_or_without_a_basis() -> None:
    judgment, client = _judge([_accept(CLAIM)], CLAIM)  # no basis in the record
    assert client.messages.create.call_count == 1
    assert judgment.decision == "accept_as_context"
    assert not any("interior assertion" in n for n in judgment.protocol_notes)
    judgment, client = _judge([_accept(CLAIM)], CLAIM, peer_publications=[_published(PEER.id)])
    assert client.messages.create.call_count == 1  # a basis changes nothing either
    assert not any("interior assertion" in n for n in judgment.protocol_notes)


def test_the_basis_check_has_no_switch_and_no_third_answer_tool() -> None:
    assert not hasattr(sm, "_MANUFACTURED_BASIS_CHECK")
    assert not hasattr(sm, "_interior_assertion_basis_tool")
    assert "not_an_assertion" not in str(sm.INTERIOR_ASSERTION_TOOL)


def test_a_note_naming_no_peer_does_not_trigger() -> None:
    _, client = _judge([_accept("The retry budget is three.")], "The retry budget is three.")
    assert client.messages.create.call_count == 1


def test_a_non_entitled_scope_re_ask_keeps_its_original_tool() -> None:
    text = "212-other-scope only approves refunds under fifty euros."
    _, client = _judge([_accept(text), _answer(classification="none")], text)
    assert "not_an_assertion" not in _reask_enum(client)
