"""v1.19 #138: the operator-attribution corrective fires on the echo, not only on the citation.

RULE 2: admitted material that echoes an operator directive carries "per operator directive <id>"
in text the judge authors. Until now the one corrective re-ask fired only when the judge CITED the
id in its reasoning. The engine now also matches the admitted text to an operator directive's
subject. The judge may decline the attribution (the same amendment, unchanged): then it is kept
and recorded, never filled in by the engine.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from strata.judge_usage import call_kind_from_request
from strata.scope_manager import OperatorItem, ScopeManager, ScopeSummary
from tests.test_scope_manager import (
    NEW_CONTRIBUTION,
    SCOPE,
    SECOND_CONTRIBUTION,
    STRATUM,
    _batch_input,
    _fake_response,
)

TAG = OperatorItem(
    id="op_tag001",
    kind="directive",
    content="Every release must be tagged before it is published.",
    subject="release-tagging",
    created_at="2026-01-01T00:00:00+00:00",
)
TAG_CONTEXT = OperatorItem(
    id="op_tagctx",
    kind="context",
    content="Every release must be tagged before it is published.",
    subject=None,
    created_at="2026-01-02T00:00:00+00:00",
)
MEMORY = [("g_exec", [TAG])]
EMPTY = ScopeSummary(
    scope_id=SCOPE.id, directives=[], context="Existing context.", updated_at="2026-04-01T09:00:00Z"
)

ECHO = "Releases are tagged before they are published."
ATTRIBUTED = f"Releases are tagged before they are published, per operator directive {TAG.id}."
UNRELATED = "The cafeteria menu changes on Mondays."


_JUDGE_TOOLS = ("submit_judgment", "submit_batch_judgment")


def _judge_calls(client: MagicMock) -> list:
    """The judge's own calls and its correctives (not the #242 context re-ask, which an operator
    directive also triggers: operator memory is inherited memory)."""
    return [
        c
        for c in client.messages.create.call_args_list
        if c.kwargs["tool_choice"]["name"] in _JUDGE_TOOLS
    ]


def _accept(context: str, reasoning: str = "A fine note.") -> dict:
    return {
        "decision": "accept_as_context",
        "reasoning": reasoning,
        "directive_ops": [],
        "new_context": context,
    }


def _judge(first: dict, second: dict | None = None, *, memory=MEMORY, content="A release note."):
    client = MagicMock()
    client.messages.create.side_effect = [
        _fake_response(first),
        *([_fake_response(second)] if second is not None else []),
    ]
    contribution = replace(NEW_CONTRIBUTION, content=content, proposed_classification="context")
    judgment = ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=EMPTY,
        recent_contributions=[],
        new_contribution=contribution,
        operator_memory=memory,
    )
    return judgment, client


def test_an_echo_with_no_citation_gets_one_corrective_and_is_stored_attributed():
    judgment, client = _judge(_accept(ECHO), _accept(ATTRIBUTED))
    assert len(_judge_calls(client)) == 2
    assert judgment.new_context == ATTRIBUTED
    assert judgment.attribution_gap_kept == []
    corrective = _judge_calls(client)[1].kwargs["messages"][-1]["content"]
    text = "\n".join(b.get("text", "") for b in corrective if isinstance(b, dict))
    assert (
        "The engine matched the admitted text to operator directive op_tag001 on its subject"
        in text
    )
    assert "Your reasoning cites" not in text
    assert call_kind_from_request(_judge_calls(client)[1].kwargs) == ("attribution_echo_recheck")


def test_a_judge_that_returns_the_amendment_unchanged_is_kept_and_recorded():
    judgment, client = _judge(_accept(ECHO), _accept(ECHO))
    assert len(_judge_calls(client)) == 2
    assert judgment.new_context == ECHO  # never filled in by the engine
    assert "per operator directive" not in judgment.new_context
    assert judgment.attribution_gap_kept == ["op_tag001"]


def test_an_unrelated_accept_with_operator_memory_gets_no_corrective():
    judgment, client = _judge(_accept(UNRELATED))
    assert len(_judge_calls(client)) == 1
    assert judgment.attribution_gap_kept == []


def test_an_echoed_operator_context_item_gets_no_corrective():
    judgment, client = _judge(_accept(ECHO), memory=[("g_exec", [TAG_CONTEXT])])
    assert len(_judge_calls(client)) == 1
    assert judgment.new_context == ECHO


def test_an_already_attributed_echo_gets_no_corrective():
    judgment, client = _judge(_accept(ATTRIBUTED))
    assert len(_judge_calls(client)) == 1
    assert judgment.attribution_gap_kept == []


def test_the_reasoning_cited_path_is_unchanged_and_keeps_its_kind():
    cited = _accept(ECHO, reasoning=f"Consistent with operator directive {TAG.id}.")
    judgment, client = _judge(cited, _accept(ATTRIBUTED))
    assert len(_judge_calls(client)) == 2
    corrective = _judge_calls(client)[1].kwargs["messages"][-1]["content"]
    text = "\n".join(b.get("text", "") for b in corrective if isinstance(b, dict))
    assert f"Your reasoning cites operator directive(s) {TAG.id}" in text
    assert "The engine matched" not in text  # the added sentence is for engine matches only
    assert call_kind_from_request(_judge_calls(client)[1].kwargs) == ("corrective_reask")
    assert judgment.new_context == ATTRIBUTED


def test_a_cited_and_engine_matched_id_uses_the_existing_kind_and_the_cited_wording():
    cited = _accept(ECHO, reasoning=f"Consistent with operator directive {TAG.id}.")
    _judgment, client = _judge(cited, _accept(ECHO))
    assert call_kind_from_request(_judge_calls(client)[1].kwargs) == ("corrective_reask")


def _batch_judge(batch: dict, second: dict | None):
    client = MagicMock()
    client.messages.create.side_effect = [
        _fake_response(batch),
        *([_fake_response(second)] if second is not None else []),
    ]
    echoing = replace(NEW_CONTRIBUTION, content=ECHO, proposed_classification="context")
    other = replace(SECOND_CONTRIBUTION, content=UNRELATED, proposed_classification="context")
    judgment = ScopeManager(client=client).judge_batch(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=EMPTY,
        recent_contributions=[],
        new_contributions=[echoing, other],
        operator_memory=MEMORY,
    )
    return judgment, client, echoing, other


def _two_context_verdicts(echoing, other, context):
    return _batch_input(
        verdicts=[
            {"contribution_id": echoing.id, "decision": "accept_as_context", "reasoning": "a"},
            {"contribution_id": other.id, "decision": "accept_as_context", "reasoning": "b"},
        ],
        directive_ops=[],
        new_context=context,
    )


def test_the_batch_names_only_the_echoing_member_in_one_corrective_call():
    echoing = replace(NEW_CONTRIBUTION, content=ECHO, proposed_classification="context")
    other = replace(SECOND_CONTRIBUTION, content=UNRELATED, proposed_classification="context")
    first = _two_context_verdicts(echoing, other, f"{ECHO} {UNRELATED}")
    second = _two_context_verdicts(echoing, other, f"{ATTRIBUTED} {UNRELATED}")
    judgment, client, echoing, other = _batch_judge(first, second)
    assert len(_judge_calls(client)) == 2
    corrective = _judge_calls(client)[1].kwargs["messages"][-1]["content"]
    text = "\n".join(b.get("text", "") for b in corrective if isinstance(b, dict))
    assert f"{echoing.id}:{TAG.id}" in text
    assert f"{other.id}:{TAG.id}" not in text
    assert judgment.new_context == f"{ATTRIBUTED} {UNRELATED}"
    assert judgment.attribution_gap_kept == []
    assert call_kind_from_request(_judge_calls(client)[1].kwargs) == ("attribution_echo_recheck")


def test_the_batch_keeps_and_records_an_unchanged_amendment():
    echoing = replace(NEW_CONTRIBUTION, content=ECHO, proposed_classification="context")
    other = replace(SECOND_CONTRIBUTION, content=UNRELATED, proposed_classification="context")
    same = _two_context_verdicts(echoing, other, f"{ECHO} {UNRELATED}")
    judgment, client, echoing, _other = _batch_judge(same, same)
    assert len(_judge_calls(client)) == 2
    assert judgment.attribution_gap_kept == [f"{echoing.id}:{TAG.id}"]
    assert "per operator directive" not in judgment.new_context


def test_a_batch_with_no_echo_makes_no_corrective_call():
    echoing = replace(NEW_CONTRIBUTION, content=UNRELATED, proposed_classification="context")
    other = replace(SECOND_CONTRIBUTION, content=UNRELATED, proposed_classification="context")
    first = _two_context_verdicts(echoing, other, UNRELATED)
    client = MagicMock()
    client.messages.create.side_effect = [_fake_response(first)]
    ScopeManager(client=client).judge_batch(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=EMPTY,
        recent_contributions=[],
        new_contributions=[echoing, other],
        operator_memory=MEMORY,
    )
    assert len(_judge_calls(client)) == 1


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Your reasoning cites operator directive(s) op_1, but no text", "corrective_reask"),
        (
            "Your amendment may echo operator directive(s) op_1, but no text ... "
            "The engine matched the admitted text to operator directive op_1 on its subject; ...",
            "attribution_echo_recheck",
        ),
    ],
)
def test_the_meter_names_the_two_kinds_by_the_corrective_that_was_sent(text, kind):
    request = {
        "tool_choice": {"type": "tool", "name": "submit_judgment"},
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": []},
            {"role": "user", "content": [{"type": "text", "text": text}]},
        ],
    }
    assert call_kind_from_request(request) == kind
