"""v1.18 (#242) — a child's CONTEXT that undercuts an inherited directive.

An ordinary context admit from a session bound to the scope, in a scope that
inherits at least one directive, gets one compact re-ask
(``classify_inherited_relation``); the engine verifies the answer. An
exception is declined; a specific past report stays admitted; an unreadable
or unrelated answer declines only when an exception marker is present.
"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from strata.record_store import ContributorRef
from strata.scope_manager import (
    CLASSIFY_INHERITED_RELATION_TOOL,
    Directive,
    OperatorItem,
    ScopeManager,
    inherited_for_context,
    verify_inherited_relation,
)
from tests.test_same_scope_provenance import _SINGLE_MESSAGES_SHA, _sha_json
from tests.test_scope_manager import (
    CURRENT_SUMMARY,
    NEW_CONTRIBUTION,
    SCOPE,
    SECOND_CONTRIBUTION,
    STRATUM,
    _batch_input,
    _fake_response,
    _judge_batch,
)

RULE = "Fuel dock pumps must be switched off at or before 20:00 every evening."
ANCESTOR = Directive(
    id="c_pump01",
    content=RULE,
    subject="pumps",
    source_scope_id="g_arch",
    source_skill="architect",
    created_at="2026-03-01T09:00:00+00:00",
)
ANCESTORS = [("g_arch", [ANCESTOR])]

EXCEPTION_NORMATIVE = (
    "FYI, on nights when the fleet returns the pumps may stay running until 22:00."
)
EXCEPTION_HABITUAL = (
    "FYI, on nights when the fishing fleet is returning we leave the pumps running until 22:00."
)
CONSEQUENCE = (
    "Switching the dock pumps off at 20:00 on 9 October left two trawlers without fuel at the quay."
)
DEPARTURE = "On 5 October the dock pumps were not switched off until 21:10 and the quay stayed lit."
GENERALISED_DEPARTURE = (
    "On 5 October the dock pumps were not switched off until 21:10, so pumps don't need to go "
    "off at 20:00."
)
PLAIN_FACT = "The fuel dock sits at the north end of the quay."

NOTE_DECLINE = (
    "[Declined: contrary to inherited directive c_pump01 (g_arch). A practice that departs from "
    "an inherited rule can't be recorded as this scope's context. Report a specific occurrence "
    "of following the rule and what happened (admitted, and raised to g_arch with acted_on), or "
    "propose the exception to g_arch.]"
)
NOTE_CONSEQUENCE = (
    "[A report of following c_pump01 (g_arch). To raise it to g_arch, resubmit with "
    "acted_on = c_pump01.]"
)
NOTE_DEPARTURE = (
    "[A departure from c_pump01 (g_arch), not a licence: only g_arch decides whether the rule is "
    "slack. To raise it to g_arch, resubmit with acted_on = c_pump01.]"
)


def _answer(kind: str, **extra: str) -> dict:
    return {"kind": kind, "inherited_id": "c_pump01", "reasoning": "r", **extra}


# -- the verifier -----------------------------------------------------------


def test_an_exception_with_a_verbatim_instead_span_is_declined() -> None:
    verdict, _ = verify_inherited_relation(
        _answer("exception", instead_span="may stay running until 22:00"),
        EXCEPTION_NORMATIVE,
        RULE,
    )
    assert verdict == "decline"


def test_a_specific_past_consequence_report_is_admitted_as_one() -> None:
    verdict, _ = verify_inherited_relation(
        _answer("consequence_report", occurrence_span=CONSEQUENCE.rstrip(".")), CONSEQUENCE, RULE
    )
    assert verdict == "consequence_report"


def test_a_dated_departure_report_passes_as_a_departure() -> None:
    verdict, _ = verify_inherited_relation(
        _answer("departure_report", occurrence_span=DEPARTURE.rstrip(".")), DEPARTURE, RULE
    )
    assert verdict == "departure_report"


def test_a_departure_mislabelled_as_a_consequence_is_still_named_a_departure() -> None:
    verdict, _ = verify_inherited_relation(
        _answer("consequence_report", occurrence_span=DEPARTURE.rstrip(".")), DEPARTURE, RULE
    )
    assert verdict == "departure_report"


def test_a_generalising_clause_declines_the_whole_report() -> None:
    for kind in ("consequence_report", "departure_report"):
        verdict, reason = verify_inherited_relation(
            _answer(kind, occurrence_span=GENERALISED_DEPARTURE.rstrip(".")),
            GENERALISED_DEPARTURE,
            RULE,
        )
        assert verdict == "decline"
        assert "generalises" in reason


@pytest.mark.parametrize("text", [EXCEPTION_NORMATIVE, EXCEPTION_HABITUAL])
def test_a_forged_report_answer_on_an_exception_still_declines(text: str) -> None:
    for span in (text, text.rstrip("."), "the pumps"):
        for kind in ("consequence_report", "departure_report"):
            verdict, _ = verify_inherited_relation(_answer(kind, occurrence_span=span), text, RULE)
            assert verdict == "decline"


def test_a_forged_exception_answer_without_a_verbatim_span_leaves_a_report_admitted() -> None:
    for answer in (
        _answer("exception", instead_span="pumps may be skipped instead"),
        _answer("exception"),
    ):
        verdict, _ = verify_inherited_relation(answer, CONSEQUENCE, RULE)
        assert verdict == "admit"


def test_the_fallback_declines_with_a_marker_and_admits_without() -> None:
    for answer in (_answer("unrelated"), {"kind": "banana"}, None, {}):
        assert verify_inherited_relation(answer, EXCEPTION_HABITUAL, RULE)[0] == "decline"
        assert verify_inherited_relation(answer, PLAIN_FACT, RULE)[0] == "admit"


def test_a_span_that_is_not_in_the_text_fails_the_check() -> None:
    verdict, reason = verify_inherited_relation(
        _answer("consequence_report", occurrence_span="something never said on 9 October"),
        CONSEQUENCE,
        RULE,
    )
    assert verdict == "admit"
    assert "verbatim" in reason


@pytest.mark.parametrize(
    "text",
    [
        "In the pump hall the outlet chlorine residual is tested every 4 hours at night.",
        "We start repeat jobs straight away and a second operator proofs only the new ones.",
    ],
)
def test_a_looser_or_narrowed_practice_with_no_modal_word_is_the_habitual_signal(
    text: str,
) -> None:
    rule = "Chlorine residual at the outlet must be tested every 2 hours."
    if "operator" in text:
        rule = "A second operator proofs every client job before the press is started."
    verdict, _ = verify_inherited_relation(_answer("unrelated"), text, rule)
    assert verdict == "decline"


def test_the_tool_requires_kind_id_and_reasoning() -> None:
    schema = CLASSIFY_INHERITED_RELATION_TOOL["input_schema"]
    assert schema["required"] == ["kind", "inherited_id", "reasoning"]
    assert schema["properties"]["kind"]["enum"] == [
        "consequence_report",
        "departure_report",
        "exception",
        "unrelated",
    ]
    assert {"occurrence_span", "instead_span"} <= set(schema["properties"])


def test_the_reask_shows_the_most_relevant_directives_within_a_budget() -> None:
    long_rule = ("Unrelated filler words about vendors. " * 40).strip()
    inherited = [("c_far", "g_x", long_rule)] * 10 + [("c_near", "g_x", RULE)]
    shown = inherited_for_context(CONSEQUENCE, inherited)
    assert shown[0][0] == "c_near"
    assert sum(min(len(t), 600) for _i, _o, t in shown) <= 2400 + 600


# -- the single path --------------------------------------------------------

FIRST_CONTEXT = {
    "decision": "accept_as_context",
    "reasoning": "A note.",
    "directive_ops": [],
    "new_context": "Existing context plus the note.",
}


def _judge(
    content: str,
    answer: dict | Exception | None,
    *,
    first: dict | None = None,
    ancestors=ANCESTORS,  # noqa: ANN001
    operator_memory=None,  # noqa: ANN001
    contributor: ContributorRef | None = None,
):  # noqa: ANN202
    client = MagicMock()
    responses = [_fake_response(first or FIRST_CONTEXT)]
    if isinstance(answer, Exception):
        responses.append(answer)
    elif answer is not None:
        responses.append(_fake_response(answer))
    client.messages.create.side_effect = responses
    contribution = replace(NEW_CONTRIBUTION, content=content, proposed_classification="context")
    if contributor is not None:
        contribution = replace(contribution, contributor=contributor)
    judgment = ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=contribution,
        ancestor_directives=ancestors,
        operator_memory=operator_memory,
    )
    return judgment, client


def test_an_exception_is_declined_with_the_engine_reason_and_the_trace() -> None:
    judgment, client = _judge(
        EXCEPTION_NORMATIVE,
        _answer("exception", instead_span="may stay running until 22:00"),
    )
    assert client.messages.create.call_count == 2
    assert judgment.decision == "decline"
    assert judgment.directive_ops == []
    assert judgment.new_context is None
    assert judgment.new_summary is None
    assert judgment.reasoning == f"A note. {NOTE_DECLINE}"
    assert judgment.inherited_relation == {
        "contribution_id": NEW_CONTRIBUTION.id,
        "kind": "exception",
        "verdict": "decline",
        "fallback": False,
        "inherited_id": "c_pump01",
        "origin": "g_arch",
        "reason": "an exception: states what happens instead of the rule",
    }


def test_a_consequence_report_stays_admitted_with_the_acted_on_suggestion() -> None:
    judgment, _client = _judge(
        CONSEQUENCE, _answer("consequence_report", occurrence_span=CONSEQUENCE.rstrip("."))
    )
    assert judgment.decision == "accept_as_context"
    assert judgment.new_summary is not None
    assert judgment.new_summary.context == "Existing context plus the note."
    assert judgment.reasoning == f"A note. {NOTE_CONSEQUENCE}"
    assert judgment.inherited_relation["verdict"] == "consequence_report"
    assert judgment.inherited_relation["fallback"] is False


def test_a_departure_report_is_named_a_departure_never_a_licence() -> None:
    judgment, _client = _judge(
        DEPARTURE, _answer("departure_report", occurrence_span=DEPARTURE.rstrip("."))
    )
    assert judgment.decision == "accept_as_context"
    assert judgment.reasoning == f"A note. {NOTE_DEPARTURE}"
    assert judgment.inherited_relation["verdict"] == "departure_report"


def test_a_generalised_departure_declines_as_a_whole() -> None:
    judgment, _client = _judge(
        GENERALISED_DEPARTURE,
        _answer("departure_report", occurrence_span=GENERALISED_DEPARTURE.rstrip(".")),
    )
    assert judgment.decision == "decline"


def test_the_fallback_is_counted_both_ways() -> None:
    admitted, _c = _judge(PLAIN_FACT, _answer("unrelated"))
    assert admitted.decision == "accept_as_context"
    assert admitted.inherited_relation["fallback"] is True
    assert admitted.reasoning == "A note."  # nothing to say about an unverified admit

    declined, _c = _judge(EXCEPTION_HABITUAL, _answer("unrelated"))
    assert declined.decision == "decline"
    assert declined.inherited_relation["fallback"] is False


def test_a_failed_reask_is_the_unreadable_case_not_an_error() -> None:
    admitted, _c = _judge(PLAIN_FACT, RuntimeError("boom"))
    assert admitted.decision == "accept_as_context"
    assert admitted.inherited_relation["fallback"] is True
    assert "re-ask failed" in admitted.inherited_relation["reason"]
    declined, _c = _judge(EXCEPTION_HABITUAL, RuntimeError("boom"))
    assert declined.decision == "decline"


def test_an_answer_naming_no_shown_directive_is_unreadable() -> None:
    answer = _answer("consequence_report", occurrence_span=CONSEQUENCE.rstrip("."))
    answer["inherited_id"] = "c_not_shown"
    judgment, _c = _judge(CONSEQUENCE, answer)
    assert judgment.inherited_relation["fallback"] is True


def test_an_operator_directive_is_an_inherited_source_too() -> None:
    operator = OperatorItem(
        id="op_pump", kind="directive", content=RULE, subject=None,
        created_at="2026-01-01T00:00:00+00:00",
    )  # fmt: skip
    answer = _answer("exception", instead_span="may stay running until 22:00")
    answer["inherited_id"] = "op_pump"
    judgment, _c = _judge(
        EXCEPTION_NORMATIVE, answer, ancestors=None, operator_memory=[("g_exec", [operator])]
    )
    assert judgment.decision == "decline"
    assert "operator, g_exec" in judgment.reasoning


def test_the_reask_message_carries_only_the_definitions_the_directives_and_the_item() -> None:
    _j, client = _judge(PLAIN_FACT, _answer("unrelated"))
    kwargs = client.messages.create.call_args_list[1].kwargs
    assert kwargs["max_tokens"] <= 300
    assert [t["name"] for t in kwargs["tools"]] == ["classify_inherited_relation"]
    assert kwargs["messages"] == [
        {
            "role": "user",
            "content": (
                f"INHERITED DIRECTIVES:\n- c_pump01: {RULE}\n\nCONTEXT ITEM:\n{PLAIN_FACT}\n\n"
                "Call `classify_inherited_relation` exactly once."
            ),
        }
    ]
    assert "SUMMARY" not in json.dumps(kwargs).upper().replace("SUMMARISE", "")


# -- no extra call ----------------------------------------------------------


def test_no_extra_call_for_a_decline_a_directive_admit_or_no_inherited_directive() -> None:
    for first, ancestors in (
        (
            {"decision": "decline", "reasoning": "no", "directive_ops": [], "new_context": None},
            ANCESTORS,
        ),
        (
            {
                "decision": "accept_as_directive",
                "reasoning": "a rule",
                "directive_ops": [{"op": "append"}],
                "new_context": None,
            },
            ANCESTORS,
        ),
        (FIRST_CONTEXT, None),
    ):
        judgment, client = _judge(EXCEPTION_HABITUAL, None, first=first, ancestors=ancestors)
        assert client.messages.create.call_count == 1
        assert judgment.inherited_relation is None


def test_no_extra_call_for_another_position() -> None:
    other = ContributorRef(
        scope_id="g_elsewhere", skill="s", session_id="x", ts="2026-05-01T10:00:00+00:00"
    )
    judgment, client = _judge(EXCEPTION_HABITUAL, None, contributor=other)
    assert client.messages.create.call_count == 1
    assert judgment.inherited_relation is None


def test_no_extra_call_in_a_non_ordinary_mode() -> None:
    client = MagicMock()
    client.messages.create.side_effect = [_fake_response(FIRST_CONTEXT)]
    ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=replace(NEW_CONTRIBUTION, content=EXCEPTION_HABITUAL),
        ancestor_directives=ANCESTORS,
        mode="input_change_refresh",
    )
    assert client.messages.create.call_count == 1


def test_the_first_call_is_byte_identical_whether_or_not_the_re_ask_will_fire() -> None:
    inputs = []
    for ancestors in (None, ANCESTORS):
        client = MagicMock()
        client.messages.create.side_effect = [
            _fake_response(
                {
                    "decision": "accept_as_context",
                    "reasoning": "r",
                    "directive_ops": [],
                    "new_context": "x",
                }
            ),
            _fake_response(_answer("unrelated")),
        ]
        ScopeManager(client=client).judge(
            scope=SCOPE,
            stratum=STRATUM,
            current_summary=CURRENT_SUMMARY,
            recent_contributions=[],
            new_contribution=NEW_CONTRIBUTION,
            ancestor_directives=ancestors,
        )
        inputs.append(client.messages.create.call_args_list[0].kwargs)
    without, with_ancestors = inputs
    assert _sha_json(without["messages"]) == _SINGLE_MESSAGES_SHA
    assert without["tools"] == with_ancestors["tools"]
    assert without["system"] == with_ancestors["system"]


# -- the batch path ---------------------------------------------------------


def _batch(first_text: str, second_text: str, answers: list[dict]):  # noqa: ANN202
    first = replace(NEW_CONTRIBUTION, content=first_text, proposed_classification="context")
    second = replace(SECOND_CONTRIBUTION, content=second_text, proposed_classification="context")
    client = MagicMock()
    client.messages.create.side_effect = [
        _fake_response(
            _batch_input(
                verdicts=[
                    {
                        "contribution_id": first.id,
                        "decision": "accept_as_context",
                        "reasoning": "a",
                    },
                    {
                        "contribution_id": second.id,
                        "decision": "accept_as_context",
                        "reasoning": "b",
                    },
                ],
                directive_ops=[],
                new_context="The judge's rewrite, with the exception in it.",
            )
        ),
        *[_fake_response(a) for a in answers],
    ]
    result = _judge_batch(client, contributions=[first, second], ancestor_directives=ANCESTORS)
    return result, first, second, client


def test_a_batch_declines_the_exception_member_and_withholds_the_context_rewrite() -> None:
    result, first, second, client = _batch(
        EXCEPTION_HABITUAL,
        CONSEQUENCE,
        [
            _answer("unrelated"),
            _answer("consequence_report", occurrence_span=CONSEQUENCE.rstrip(".")),
        ],
    )
    assert client.messages.create.call_count == 3  # the batch call + one re-ask per member
    decisions = {v.contribution_id: v.decision for v in result.verdicts}
    assert decisions == {first.id: "decline", second.id: "accept_as_context"}
    assert result.verdicts[0].reasoning == f"a {NOTE_DECLINE}"
    assert result.verdicts[1].reasoning == f"b {NOTE_CONSEQUENCE}"
    # The judge's rewrite had the exception in view: it is replaced by the previous
    # context plus the remaining member's own text.
    assert "exception in it" not in (result.new_context or "")
    assert EXCEPTION_HABITUAL not in (result.new_context or "")
    assert CONSEQUENCE in (result.new_context or "")
    assert [r["verdict"] for r in result.inherited_relations] == ["decline", "consequence_report"]


def test_a_batch_with_no_declined_member_keeps_the_judges_rewrite() -> None:
    result, _first, _second, _client = _batch(
        PLAIN_FACT,
        CONSEQUENCE,
        [
            _answer("unrelated"),
            _answer("consequence_report", occurrence_span=CONSEQUENCE.rstrip(".")),
        ],
    )
    assert result.new_context == "The judge's rewrite, with the exception in it."
    assert [r["fallback"] for r in result.inherited_relations] == [True, False]


def test_a_batch_without_inherited_directives_makes_no_extra_call() -> None:
    client = MagicMock()
    client.messages.create.return_value = _fake_response(
        _batch_input(
            verdicts=[
                {
                    "contribution_id": NEW_CONTRIBUTION.id,
                    "decision": "accept_as_context",
                    "reasoning": "a",
                },
                {
                    "contribution_id": SECOND_CONTRIBUTION.id,
                    "decision": "accept_as_context",
                    "reasoning": "b",
                },
            ],
            directive_ops=[],
        )
    )
    result = _judge_batch(
        client, contributions=[replace(NEW_CONTRIBUTION), replace(SECOND_CONTRIBUTION)]
    )
    assert client.messages.create.call_count == 1
    assert result.inherited_relations == []


def test_a_batch_of_one_carries_the_single_path_trace() -> None:
    client = MagicMock()
    client.messages.create.side_effect = [
        _fake_response(FIRST_CONTEXT),
        _fake_response(_answer("exception", instead_span="may stay running until 22:00")),
    ]
    result = ScopeManager(client=client).judge_batch(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contributions=[replace(NEW_CONTRIBUTION, content=EXCEPTION_NORMATIVE)],
        ancestor_directives=ANCESTORS,
    )
    assert result.verdicts[0].decision == "decline"
    assert result.inherited_relations[0]["verdict"] == "decline"


def test_the_month_may_is_not_a_permission_marker() -> None:
    text = "On 12 May the vendor invoices were paid at day 29 and the petty-cash float was emptied."
    verdict, _ = verify_inherited_relation(
        _answer("consequence_report", occurrence_span=text.rstrip(".")),
        text,
        "Invoices must be paid within 30 days.",
    )
    assert verdict == "consequence_report"


def test_a_so_clause_that_says_enough_generalises_the_report() -> None:
    text = "On 9 June the release shipped with one approval and nothing broke, so one is enough."
    verdict, reason = verify_inherited_relation(
        _answer("departure_report", occurrence_span=text.rstrip(".")),
        text,
        "Releases require two approvals.",
    )
    assert verdict == "decline"
    assert "generalises" in reason
