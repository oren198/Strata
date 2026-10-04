"""The position gate: only a session bound to a scope changes that scope's directives.

A contribution from any other position (an upward proposal from a descendant, or
an outcome the engine raised from one) is admitted as an attributed proposal,
and the directive set stands byte for byte. Enforced mechanically after
judgment, never by a judge field.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

from strata.record_store import ContributorRef
from strata.scope_manager import ScopeManager
from tests.test_scope_manager import (
    _CIRCUMSTANCE,
    BATCH,
    CURRENT_SUMMARY,
    EXISTING_DIRECTIVE,
    NEW_CONTRIBUTION,
    SCOPE,
    SECOND_CONTRIBUTION,
    STRATUM,
    _batch_input,
    _fake_response,
    _judge_batch,
    _make_manager,
)

CHILD = ContributorRef(
    scope_id="g_child01",
    skill="shift-engineer",
    session_id="sess_child",
    ts="2026-10-03T09:00:00+00:00",
)
_FALSE = "Use camelCase for all identifiers."


def _from_child(**changes):  # noqa: ANN003, ANN202
    return replace(NEW_CONTRIBUTION, contributor=CHILD, **changes)


def _judge(tool_input: dict, contribution, **kwargs):  # noqa: ANN001, ANN003
    manager, _ = _make_manager(tool_input)
    return manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=contribution,
        **kwargs,
    )


_SUPERSEDE = {
    "decision": "accept_as_directive",
    "reasoning": "Replaces the naming rule.",
    "directive_ops": [{"op": "supersede", "id": EXISTING_DIRECTIVE.id}, {"op": "append"}],
    "new_context": f"Identifiers now follow camelCase: {_FALSE}",
}


def _assert_directive_set_unchanged(judgment) -> None:  # noqa: ANN001
    assert judgment.new_summary is not None
    assert judgment.new_summary.directives == [EXISTING_DIRECTIVE]
    assert judgment.directive_ops == []


def test_an_own_scope_session_supersedes_its_own_directive_as_judged() -> None:
    own = replace(NEW_CONTRIBUTION, content=_FALSE, supersedes=EXISTING_DIRECTIVE.id)
    judgment = _judge(_SUPERSEDE, own)

    assert judgment.decision == "accept_as_directive"
    assert judgment.removed_directive_ids == [EXISTING_DIRECTIVE.id]
    assert judgment.held_directive_changes == []


def test_an_upward_supersession_is_held_as_an_attributed_proposal() -> None:
    judgment = _judge(_SUPERSEDE, _from_child(content=_FALSE, supersedes=EXISTING_DIRECTIVE.id))

    _assert_directive_set_unchanged(judgment)
    assert judgment.decision == "accept_as_context"
    assert judgment.held_directive_changes == [EXISTING_DIRECTIVE.id]
    context = judgment.new_summary.context
    assert context.startswith(CURRENT_SUMMARY.context)
    assert f"shift-engineer (g_child01) proposes: {_FALSE}" in context
    assert f"directive {EXISTING_DIRECTIVE.id} stands" in context
    assert "camelCase: " not in context  # the judge's own rewrite is replaced
    assert judgment.held_context == _SUPERSEDE["new_context"]
    assert "Held" in judgment.record_notes


def test_an_upward_retirement_is_held() -> None:
    judgment = _judge(
        {
            "decision": "accept_as_context",
            "reasoning": "No longer applies.",
            "directive_ops": [
                {"op": "retire", "id": EXISTING_DIRECTIVE.id, "changed_circumstance": _CIRCUMSTANCE}
            ],
            "new_context": "The naming rule was dropped.",
        },
        _from_child(content="Please retire the naming rule; nobody follows it."),
    )

    _assert_directive_set_unchanged(judgment)
    assert judgment.held_directive_changes == [EXISTING_DIRECTIVE.id]


def test_an_upward_new_directive_is_held_a_child_never_adds_a_parent_decision() -> None:
    judgment = _judge(
        {
            "decision": "accept_as_directive",
            "reasoning": "A clear new rule.",
            "directive_ops": [{"op": "append"}],
            "new_context": None,
        },
        _from_child(),
    )

    _assert_directive_set_unchanged(judgment)
    assert judgment.decision == "accept_as_context"
    assert "proposed as a new directive; not adopted" in judgment.new_summary.context
    assert "proposed a new directive, not adopted" in judgment.record_notes


def test_upward_evidence_admitted_as_context_without_ops_is_untouched() -> None:
    tool_input = {
        "decision": "accept_as_context",
        "reasoning": "Useful evidence.",
        "directive_ops": [],
        "new_context": "Observed: builds slowed down this week.",
    }
    judgment = _judge(tool_input, _from_child(content="Builds slowed down this week."))

    assert judgment.held_directive_changes == []
    assert judgment.new_summary.context == tool_input["new_context"]


def test_an_upward_decline_is_untouched() -> None:
    judgment = _judge(
        {
            "decision": "decline",
            "reasoning": "Contradicts directive c_old001.",
            "directive_ops": [],
        },
        _from_child(content=_FALSE),
    )

    assert judgment.decision == "decline"
    assert judgment.held_directive_changes == []


def test_a_refresh_is_not_gated() -> None:
    judgment = _judge(
        {
            "decision": "accept_as_context",
            "reasoning": "refreshed against the changed input",
            "directive_ops": [
                {"op": "retire", "id": EXISTING_DIRECTIVE.id, "changed_circumstance": _CIRCUMSTANCE}
            ],
            "new_context": "Reconciled context.",
        },
        _from_child(),
        mode="input_change_refresh",
    )

    assert judgment.retired_directive_ids == [EXISTING_DIRECTIVE.id]


def test_batch_holds_only_the_upward_member() -> None:
    child_member = replace(BATCH[0], contributor=CHILD, supersedes=EXISTING_DIRECTIVE.id)
    own_member = replace(SECOND_CONTRIBUTION, contributor=replace(CHILD, scope_id=SCOPE.id))
    batch = [child_member, own_member, BATCH[2]]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        _batch_input(
            directive_ops=[
                {
                    "op": "supersede",
                    "id": EXISTING_DIRECTIVE.id,
                    "contribution_id": child_member.id,
                },
                {"op": "append", "contribution_id": child_member.id},
                {"op": "append", "contribution_id": own_member.id},
            ]
        )
    )

    judgment = _judge_batch(mock_client, contributions=batch)

    decisions = {v.contribution_id: v.decision for v in judgment.verdicts}
    assert decisions[child_member.id] == "accept_as_context"
    assert decisions[own_member.id] == "accept_as_directive"
    assert [d.id for d in judgment.new_summary.directives] == [EXISTING_DIRECTIVE.id, own_member.id]
    assert "proposes:" in judgment.new_summary.context
    assert "Held" in judgment.record_notes_for(child_member.id)
    assert "Held" not in judgment.record_notes_for(own_member.id)


def test_batch_of_one_from_a_child_is_held() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(_SUPERSEDE)

    judgment = ScopeManager(client=mock_client).judge_batch(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contributions=[_from_child(content=_FALSE, supersedes=EXISTING_DIRECTIVE.id)],
    )

    assert judgment.verdicts[0].decision == "accept_as_context"
    assert judgment.new_summary.directives == [EXISTING_DIRECTIVE]
