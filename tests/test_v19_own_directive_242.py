"""v1.19 (#242 for the scope's OWN directives).

A context note from a session bound to the scope gets the same check the inherited rules get:
a general or habitual exception in force is declined, a specific dated departure is admitted
as context, deliberation about a rule is admitted. A context note never changes the scope's
own rule, whatever the judge returned: supersede/retire ops on own directives are dropped.
The fleet is one scope with one own directive and nothing inherited.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from strata.judge_usage import call_kind_from_request, kind_is_gated
from strata.scope_manager import (
    CLASSIFY_INHERITED_RELATION_TOOL,
    Directive,
    ScopeManager,
    ScopeSummary,
)
from tests.test_scope_manager import (
    NEW_CONTRIBUTION,
    SCOPE,
    SECOND_CONTRIBUTION,
    STRATUM,
    _batch_input,
    _fake_response,
)

REVIEW_RULE = "All PRs must be reviewed by two engineers before merge."
REVIEW = Directive(
    id="c_review01",
    content=REVIEW_RULE,
    subject="review",
    source_scope_id=SCOPE.id,
    source_skill="architect",
    created_at="2026-04-01T09:00:00+00:00",
)
ALERT_RULE = "Alerts unacknowledged for 10 minutes must auto-escalate to the secondary on-call."
SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[REVIEW],
    context="Existing context.",
    updated_at="2026-04-01T09:00:00+00:00",
)
OWN_ORIGIN = f"{SCOPE.id} (this scope)"

IN_FORCE = (
    "Given we're short-staffed this sprint, let's treat single-engineer review as sufficient "
    "for now -- just noting it here rather than filing a formal change."
)
DECIDED = (
    "Team consensus in retro was that the review step feels too heavy, and everyone agreed we "
    "should just informally skip the second engineer on our own since a formal change would "
    "take too long."
)
DEPARTURE = (
    "On 2026-10-12 one PR was merged after a single review because the second engineer was out."
)
DELIBERATIONS = [
    "We are considering retiring the two-engineer review rule because it slows the hotfixes.",
    "Should we change the two-engineer review rule for small PRs?",
    "Proposing that we relax the two-engineer review requirement, raise it with the architect.",
]
NOTE_DECLINE_OWN = (
    "[Declined: c_review01 is this scope's own directive. To change it, supersede or retire it.]"
)
NOTE_DEPARTURE_OWN = (
    "[Recorded as a departure from c_review01 (this scope): not a licence; to change the "
    "rule, supersede or retire it.]"
)

FIRST_CONTEXT = {
    "decision": "accept_as_context",
    "reasoning": "A note.",
    "directive_ops": [],
    "new_context": "Existing context plus the note.",
}


def _answer(kind: str, **extra: str) -> dict:
    return {"kind": kind, "inherited_id": "c_review01", "reasoning": "r", **extra}


def _judge(
    content,
    answer,
    *,
    first=None,
    proposed="context",
    supersedes=None,
    summary=SUMMARY,
    ancestors=None,
):
    client = MagicMock()
    responses = [_fake_response(first or FIRST_CONTEXT)]
    if answer is not None:
        responses.append(_fake_response(answer))
    client.messages.create.side_effect = responses
    contribution = replace(
        NEW_CONTRIBUTION, content=content, proposed_classification=proposed, supersedes=supersedes
    )
    judgment = ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=summary,
        recent_contributions=[],
        new_contribution=contribution,
        ancestor_directives=ancestors,
    )
    return judgment, client


@pytest.mark.parametrize("note", [IN_FORCE, DECIDED])
def test_an_exception_in_force_on_an_own_directive_is_declined_with_the_own_reason(note):
    judgment, client = _judge(note, _answer("exception", instead_span=note))
    assert client.messages.create.call_count == 2
    assert judgment.decision == "decline"
    assert judgment.new_summary is None
    assert judgment.reasoning == f"A note. {NOTE_DECLINE_OWN}"
    assert judgment.inherited_relation["origin"] == OWN_ORIGIN
    assert judgment.inherited_relation["inherited_id"] == "c_review01"


@pytest.mark.parametrize("kind", ["unrelated", "banana"])
def test_the_markers_decline_an_own_directive_exception_whatever_the_answer(kind):
    judgment, _ = _judge(DECIDED, _answer(kind))
    assert judgment.decision == "decline"


def test_the_decline_reason_carries_no_end_condition_parenthetical_in_this_build():
    judgment, _ = _judge(IN_FORCE, _answer("exception", instead_span=IN_FORCE))
    assert "(a temporary change" not in judgment.reasoning
    assert "end condition" not in judgment.reasoning
    assert judgment.reasoning.endswith("supersede or retire it.]")


def test_a_dated_own_directive_departure_is_admitted_with_the_departure_note():
    judgment, _ = _judge(DEPARTURE, _answer("departure_report", occurrence_span=DEPARTURE[:-1]))
    assert judgment.decision == "accept_as_context"
    assert judgment.reasoning == f"A note. {NOTE_DEPARTURE_OWN}"
    assert judgment.inherited_relation["verdict"] == "departure_report"
    assert judgment.inherited_relation["origin"] == OWN_ORIGIN
    assert judgment.new_summary.context == "Existing context plus the note."  # no prefix written


@pytest.mark.parametrize("note", DELIBERATIONS)
@pytest.mark.parametrize("kind", ["exception", "unrelated", "banana"])
def test_deliberation_about_an_own_directive_is_admitted_whatever_the_answer(note, kind):
    judgment, _ = _judge(note, _answer(kind, instead_span=note))
    assert judgment.decision == "accept_as_context"
    assert judgment.inherited_relation["reason"] == "deliberation about c_review01"
    assert judgment.inherited_relation["fallback"] is False


def test_a_deliberation_that_also_states_a_practice_in_force_still_declines():
    note = "We are considering retiring the rule, and until then we just skip the second review."
    judgment, _ = _judge(note, _answer("exception", instead_span=note))
    assert judgment.decision == "decline"


def test_the_reask_for_an_own_directive_uses_neutral_wording_and_its_own_tool():
    _judgment, client = _judge(IN_FORCE, _answer("exception", instead_span=IN_FORCE))
    kwargs = client.messages.create.call_args_list[1].kwargs
    assert kwargs["tool_choice"]["name"] == "classify_own_directive_relation"
    assert "inherited" not in kwargs["system"][0]["text"].lower()
    assert kwargs["messages"][0]["content"].startswith("DIRECTIVES THAT BIND THIS SCOPE:")
    assert "inherited" not in kwargs["messages"][0]["content"].lower()
    assert "inherited" not in str(kwargs["tools"][0]).lower().replace("inherited_id", "")
    # the inherited wording is untouched
    assert "inherited" in CLASSIFY_INHERITED_RELATION_TOOL["description"]


# -- the judge never turns a context note into a change of the rule ---------

SUPERSEDE_OPS = {
    "decision": "accept_as_directive",
    "reasoning": "A real change.",
    "directive_ops": [{"op": "supersede", "id": "c_review01"}, {"op": "append"}],
    "new_context": None,
}


def test_a_context_note_whose_judge_output_supersedes_an_own_directive_is_dropped():
    judgment, _ = _judge(IN_FORCE, _answer("exception", instead_span=IN_FORCE), first=SUPERSEDE_OPS)
    assert judgment.directive_ops == []
    assert "[Engine: a context note can't change c_review01;" in judgment.reasoning
    assert "Ops dropped:" in judgment.reasoning
    # then judged as context, through the check: in force, so declined
    assert judgment.decision == "decline"
    assert judgment.inherited_relation["origin"] == OWN_ORIGIN


def test_the_dropped_supersede_leaves_the_rule_standing_when_the_note_is_a_deliberation():
    note = DELIBERATIONS[0]
    judgment, _ = _judge(note, _answer("unrelated"), first=SUPERSEDE_OPS)
    assert judgment.decision == "accept_as_context"
    assert judgment.directive_ops == []
    assert [d.id for d in judgment.new_summary.directives] == ["c_review01"]
    assert "[Engine: a context note can't change c_review01;" in judgment.reasoning


def test_a_retire_of_an_own_directive_from_a_context_note_is_dropped_too():
    first = {**SUPERSEDE_OPS, "directive_ops": [{"op": "retire", "id": "c_review01"}]}
    judgment, _ = _judge(DELIBERATIONS[1], _answer("unrelated"), first=first)
    assert judgment.directive_ops == []
    assert [d.id for d in judgment.new_summary.directives] == ["c_review01"]


def test_a_context_proposal_the_judge_upgrades_with_an_append_only_is_unchanged():
    first = {**SUPERSEDE_OPS, "directive_ops": [{"op": "append"}]}
    judgment, client = _judge("A brand new standing rule.", None, first=first)
    assert client.messages.create.call_count == 1
    assert [op.op for op in judgment.directive_ops] == ["append"]
    assert judgment.decision == "accept_as_directive"


def test_a_directive_proposal_with_an_explicit_supersedes_is_unchanged():
    first = {
        **SUPERSEDE_OPS,
        "directive_ops": [{"op": "supersede", "id": "c_review01"}, {"op": "append"}],
    }
    judgment, client = _judge(
        "All PRs need one reviewer.",
        None,
        first=first,
        proposed="directive",
        supersedes="c_review01",
    )
    assert client.messages.create.call_count == 1
    assert [op.op for op in judgment.directive_ops] == ["supersede", "append"]


def test_a_context_proposal_with_an_explicit_supersedes_is_unchanged():
    judgment, _ = _judge(
        "All PRs need one reviewer.",
        _answer("unrelated"),
        first=SUPERSEDE_OPS,
        supersedes="c_review01",
    )
    assert [op.op for op in judgment.directive_ops] == ["supersede", "append"]


def test_the_batch_path_drops_the_same_ops_and_checks_the_note():
    client = MagicMock()
    batch = _batch_input(
        verdicts=[
            {
                "contribution_id": NEW_CONTRIBUTION.id,
                "decision": "accept_as_directive",
                "reasoning": "a real change",
            },
            {
                "contribution_id": SECOND_CONTRIBUTION.id,
                "decision": "accept_as_directive",
                "reasoning": "a new rule",
            },
        ],
        directive_ops=[
            {"op": "supersede", "id": "c_review01", "contribution_id": NEW_CONTRIBUTION.id},
            {"op": "append", "contribution_id": NEW_CONTRIBUTION.id},
            {"op": "append", "contribution_id": SECOND_CONTRIBUTION.id},
        ],
    )
    client.messages.create.side_effect = [
        _fake_response(batch),
        _fake_response(_answer("exception", instead_span=IN_FORCE)),
    ]
    first = replace(NEW_CONTRIBUTION, content=IN_FORCE, proposed_classification="context")
    second = replace(SECOND_CONTRIBUTION, proposed_classification="directive")
    judgment = ScopeManager(client=client).judge_batch(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=SUMMARY,
        recent_contributions=[],
        new_contributions=[first, second],
    )
    decisions = {v.contribution_id: v.decision for v in judgment.verdicts}
    assert decisions[first.id] == "decline"  # dropped, judged as context, in force
    assert decisions[second.id] == "accept_as_directive"
    assert [(op.op, op.contribution_id) for op in judgment.directive_ops] == [("append", second.id)]
    reasoning = next(v.reasoning for v in judgment.verdicts if v.contribution_id == first.id)
    assert "[Engine: a context note can't change c_review01;" in reasoning
    assert NOTE_DECLINE_OWN in reasoning


# -- the extra call is visible and bounded ---------------------------------

INHERITED = Directive(
    id="c_pump01",
    content="Fuel dock pumps must be switched off at or before 20:00 every evening.",
    subject="pumps",
    source_scope_id="g_arch",
    source_skill="architect",
    created_at="2026-03-01T09:00:00+00:00",
)
ANCESTORS = [("g_arch", [INHERITED])]
EMPTY = ScopeSummary(
    scope_id=SCOPE.id, directives=[], context="Existing context.", updated_at="2026-04-01T09:00:00Z"
)
PLAIN_NOTE = "Integration tests run on every pull request."


def _calls_for(shape: str) -> tuple[int, list[str]]:
    """Judge calls (and their meter kinds) for one bound context note, per scope shape."""
    summary = SUMMARY if shape in ("own only", "both") else EMPTY
    ancestors = ANCESTORS if shape in ("inherits only", "both") else None
    answer = (
        _answer("unrelated")
        if shape != "inherits only"
        else {
            **_answer("unrelated"),
            "inherited_id": "c_pump01",
        }
    )
    _judgment, client = _judge(PLAIN_NOTE, answer, summary=summary, ancestors=ancestors)
    kinds = [call_kind_from_request(c.kwargs) for c in client.messages.create.call_args_list]
    return client.messages.create.call_count, kinds


@pytest.mark.parametrize(
    ("shape", "calls", "reask_kind"),
    [
        ("inherits only", 2, "inherited_relation_recheck"),  # before: 2
        ("own only", 2, "own_directive_relation"),  # before: 1
        ("both", 2, "inherited_relation_recheck"),  # before: 2 - one combined call, no extra
        ("neither", 1, None),  # before: 1
    ],
)
def test_judge_calls_per_bound_context_note_by_scope_shape(shape, calls, reask_kind):
    count, kinds = _calls_for(shape)
    assert count == calls
    assert kinds[0] == "judgment"
    if reask_kind is not None:
        assert kinds[1:] == [reask_kind]


def test_the_own_directive_kind_is_not_gated_by_the_daily_cap():
    assert kind_is_gated("own_directive_relation") is False


def test_a_scope_with_both_sends_one_call_listing_both_candidates():
    _judgment, client = _judge(PLAIN_NOTE, {**_answer("unrelated")}, ancestors=ANCESTORS)
    reask = client.messages.create.call_args_list[1].kwargs
    content = reask["messages"][0]["content"]
    assert client.messages.create.call_count == 2
    assert "c_pump01" in content and "c_review01" in content
    assert content.startswith("DIRECTIVES THAT BIND THIS SCOPE:")
    assert reask["tool_choice"]["name"] == "classify_inherited_relation"


# -- a practice in force wrapped in a proposal is not deliberation ----------

HOTFIX_RULE = "Every hotfix must be reviewed by a second engineer before merge."
HOTFIX = Directive(
    id="c_review01",
    content=HOTFIX_RULE,
    subject="review",
    source_scope_id=SCOPE.id,
    source_skill="architect",
    created_at="2026-04-01T09:00:00+00:00",
)
HOTFIX_SUMMARY = SUMMARY.model_copy(update={"directives": [HOTFIX]})
WRAPPED = (
    "Proposing that we retire the second-review rule; hotfixes currently go out on one review."
)
WRAPPED_SPAN = "hotfixes currently go out on one review"


def test_a_practice_in_force_wrapped_in_a_proposal_declines_on_an_unreadable_answer():
    judgment, _ = _judge(WRAPPED, _answer("banana"), summary=HOTFIX_SUMMARY)
    assert judgment.decision == "decline"


def test_a_judge_named_exception_wins_over_the_deliberation_wording():
    judgment, _ = _judge(
        WRAPPED, _answer("exception", instead_span=WRAPPED_SPAN), summary=HOTFIX_SUMMARY
    )
    assert judgment.decision == "decline"
    assert judgment.reasoning.endswith(NOTE_DECLINE_OWN)


PURE = "We are thinking about whether to change the hotfix review rule next quarter."


@pytest.mark.parametrize("kind", ["exception", "unrelated"])
def test_a_pure_deliberation_stays_admitted_when_the_span_is_the_deliberation(kind):
    judgment, _ = _judge(PURE, _answer(kind, instead_span=PURE), summary=HOTFIX_SUMMARY)
    assert judgment.decision == "accept_as_context"
    assert judgment.inherited_relation["reason"] == "deliberation about c_review01"


def test_a_question_followed_by_a_practice_in_force_declines():
    note = "Should we keep this rule? Hotfixes can go out after one review anyway."
    judgment, _ = _judge(note, _answer("banana"), summary=HOTFIX_SUMMARY)
    assert judgment.decision == "decline"


@pytest.mark.parametrize(
    "cue",
    [
        "currently",
        "at the moment",
        "right now",
        "in practice",
        "as things stand",
        "meanwhile",
        "in the meantime",
        "already",
        "anyway",
        "for now",
        "these days",
        "at present",
    ],
)
def test_each_practice_cue_breaks_the_deliberation(cue):
    note = f"We are considering retiring the review rule, and hotfixes go out on one review {cue}."
    judgment, _ = _judge(note, _answer("banana"), summary=HOTFIX_SUMMARY)
    assert judgment.inherited_relation["reason"] != "deliberation about c_review01"


P3 = "Should we keep requiring a second reviewer for hotfixes once the team doubles?"


@pytest.mark.parametrize("kind", ["exception", "unrelated", "banana"])
def test_a_pure_question_about_keeping_the_rule_is_admitted_under_every_answer(kind):
    judgment, _ = _judge(P3, _answer(kind, instead_span=P3), summary=HOTFIX_SUMMARY)
    assert judgment.decision == "accept_as_context"
    assert judgment.inherited_relation["reason"] == "deliberation about c_review01"


@pytest.mark.parametrize(
    "note",
    [
        "Should we keep skipping it? We skip it on hotfixes.",
        "Should we keep this rule? Hotfixes can go out after one review anyway.",
        "Should we keep this? We already skip it.",
    ],
)
def test_a_question_followed_by_a_statement_in_force_still_declines(note):
    judgment, _ = _judge(note, _answer("banana"), summary=HOTFIX_SUMMARY)
    assert judgment.decision == "decline"


# -- the complement of a deliberation verb is being discussed, not done ----------------------

FROST_RULE = "The frost check is required before every irrigation cycle."
FROST = Directive(
    id="c-j3od-025",
    content=FROST_RULE,
    subject="frost",
    source_scope_id=SCOPE.id,
    source_skill="architect",
    created_at="2026-04-01T09:00:00+00:00",
)
FROST_SUMMARY = SUMMARY.model_copy(update={"directives": [FROST]})
REVIEW_PROPOSAL = (
    "Proposing that we review whether c-j3od-025 still fits the new irrigation schedule."
)


def _frost_answer(kind: str, note: str) -> dict:
    return {"kind": kind, "inherited_id": "c-j3od-025", "reasoning": "r", "instead_span": note}


@pytest.mark.parametrize(
    "note",
    [
        REVIEW_PROPOSAL,
        REVIEW_PROPOSAL + " The frost check remains required.",
        "We are considering whether to review the frost check against the new schedule.",
    ],
)
@pytest.mark.parametrize("kind", ["exception", "unrelated", "banana"])
def test_a_review_inside_a_proposal_is_deliberation_under_every_answer(note, kind):
    judgment, _ = _judge(note, _frost_answer(kind, note), summary=FROST_SUMMARY)
    assert judgment.decision == "accept_as_context"
    assert judgment.inherited_relation["reason"] == "deliberation about c-j3od-025"


def test_a_proposal_followed_by_a_practice_in_force_still_declines():
    note = "Proposing that we review the frost check; meanwhile we skip it on warm nights."
    judgment, _ = _judge(
        note, _frost_answer("exception", "we skip it on warm nights"), summary=FROST_SUMMARY
    )
    assert judgment.decision == "decline"
