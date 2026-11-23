"""#212 backstop 1 (ADR 0016 D2, Philis's ruling (a)): a note that names a REFERENCED PEER
which has given this scope no basis (no publication reaching it, no directive inherited
from it, no operator item about it) gets one keep-first re-ask with three answers: decline;
restate as hearsay from a named informant; or keep WITH a stated reason that the note does
not assert the peer's position. A keep with no usable reason is unresolved and declined.

The check sits behind ``_MANUFACTURED_BASIS_CHECK`` (an internal switch, tested in both
positions); the #225 path for non-entitled scopes is untouched either way.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strata import scope_manager as sm
from strata.fleet_config import EntitlementView, Scope, Stratum
from strata.operator import OperatorItem
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import ScopeManager
from strata.summary_store import Directive, ScopeSummary

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


# --- the trigger: a referenced peer with no basis -----------------------------------


def test_a_named_peer_with_no_basis_gets_the_re_ask() -> None:
    judgment, client = _judge(
        [_accept(CLAIM), _answer(classification="none")],
        CLAIM,
    )
    assert client.messages.create.call_count == 2
    assert _tool_names(client)[1] == "classify_interior_assertion"
    assert judgment.decision == "decline"
    assert any("interior assertion" in n and "212-peer" in n for n in judgment.protocol_notes)


def test_the_re_ask_offers_the_third_answer() -> None:
    _, client = _judge([_accept(CLAIM), _answer(classification="none")], CLAIM)
    assert "not_an_assertion" in _reask_enum(client)


def test_a_peer_that_published_to_this_scope_is_a_basis() -> None:
    judgment, client = _judge([_accept(CLAIM)], CLAIM, peer_publications=[_published(PEER.id)])
    assert client.messages.create.call_count == 1
    assert not any("interior assertion" in n for n in judgment.protocol_notes)


def test_a_directive_inherited_from_the_peer_is_a_basis() -> None:
    inherited = Directive(
        id="c_212inh",
        content="Refunds need a second approver.",
        subject="refunds",
        source_scope_id=PEER.id,
        source_skill="architect",
        created_at="2026-10-01T00:00:00+00:00",
    )
    _, client = _judge([_accept(CLAIM)], CLAIM, ancestor_directives=[(PARENT.id, [inherited])])
    assert client.messages.create.call_count == 1


def test_an_operator_item_naming_the_peer_is_a_basis() -> None:
    item = OperatorItem(
        id="o_212",
        kind="context",
        content="212-peer owns refund approvals.",
        subject=None,
        created_at="2026-10-01T00:00:00+00:00",
    )
    _, client = _judge([_accept(CLAIM)], CLAIM, operator_memory=[(PARENT.id, [item])])
    assert client.messages.create.call_count == 1


def test_a_note_naming_no_peer_does_not_trigger() -> None:
    _, client = _judge([_accept("The retry budget is three.")], "The retry budget is three.")
    assert client.messages.create.call_count == 1


def test_a_peer_with_a_basis_beside_one_without_triggers_on_the_unbased_one() -> None:
    text = "212-peer and 212-peer-two both approve refunds under fifty euros."
    judgment, client = _judge(
        [_accept(text), _answer(classification="none")],
        text,
        peer_publications=[_published(PEER.id)],
    )
    note = next(n for n in judgment.protocol_notes if "interior assertion" in n)
    assert "212-peer-two" in note
    assert "g_212peer (" not in note


# --- the three answers --------------------------------------------------------------


def test_answer_decline_declines() -> None:
    judgment, _ = _judge([_accept(CLAIM), _answer(classification="none")], CLAIM)
    assert judgment.decision == "decline"
    assert "Manufactured attribution" in judgment.reasoning


def test_answer_hearsay_admits_context_standing_on_the_informant() -> None:
    text = "Priya from the finance desk told me that 212-peer only approves refunds under fifty."
    judgment, client = _judge(
        [
            _accept(text),
            _answer(
                classification="informant",
                informant_span="Priya from the finance desk",
                telling_span="told me",
            ),
        ],
        text,
    )
    assert client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_context"
    assert judgment.directive_ops == []
    assert "reports that Priya from the finance desk" in judgment.new_context


def test_answer_keep_with_a_verbatim_reason_admits_as_judged() -> None:
    judgment, _ = _judge(
        [
            _accept(OWN_ACT),
            _answer(classification="not_an_assertion", reason_span="sent back our refund request"),
        ],
        "Their agent sent back our refund request twice last week. 212-peer did this.",
    )
    assert judgment.decision == "accept_as_context"
    assert any(
        "not_an_assertion" in n and "admitted as judged" in n for n in judgment.protocol_notes
    )


def test_answer_keep_with_no_reason_is_unresolved_and_declined() -> None:
    judgment, _ = _judge([_accept(CLAIM), _answer(classification="not_an_assertion")], CLAIM)
    assert judgment.decision == "decline"


def test_answer_keep_with_a_reason_not_in_the_text_is_declined() -> None:
    judgment, _ = _judge(
        [_accept(CLAIM), _answer(classification="not_an_assertion", reason_span="made up words")],
        CLAIM,
    )
    assert judgment.decision == "decline"


def test_an_unreadable_answer_fails_closed() -> None:
    judgment, _ = _judge([_accept(CLAIM), _answer(classification="gibberish")], CLAIM)
    assert judgment.decision == "decline"


# --- the #225 path is unchanged -----------------------------------------------------


def test_a_non_entitled_scope_re_ask_keeps_its_original_tool() -> None:
    text = "212-other-scope only approves refunds under fifty euros."
    _, client = _judge([_accept(text), _answer(classification="none")], text)
    assert "not_an_assertion" not in _reask_enum(client)


# --- the switch ---------------------------------------------------------------------


def test_the_switch_off_restores_todays_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "_MANUFACTURED_BASIS_CHECK", False)
    judgment, client = _judge([_accept(CLAIM)], CLAIM)
    assert client.messages.create.call_count == 1
    assert judgment.decision == "accept_as_context"


def test_the_switch_off_still_re_asks_for_a_non_entitled_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sm, "_MANUFACTURED_BASIS_CHECK", False)
    text = "212-other-scope only approves refunds under fifty euros."
    judgment, client = _judge([_accept(text), _answer(classification="none")], text)
    assert client.messages.create.call_count == 2
    assert judgment.decision == "decline"


def test_the_switch_is_on_by_default() -> None:
    assert sm._MANUFACTURED_BASIS_CHECK is True
