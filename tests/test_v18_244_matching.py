"""#244: inherited-rule matching on the shipping judge. Fix 1: the comparator."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.scope_manager import _tighten_value_check, inherited_conflict  # noqa: E402

_TOKENS = "Access tokens expire after at most 15 minutes."
_LOGS = "Application logs are retained for at most 14 days."
_PUMPS = "Pumps are switched off at or before 20:00."


@pytest.mark.parametrize(
    ("parent", "child"),
    [
        (_TOKENS, "Platform-issued access tokens expire after 10 minutes."),
        (_TOKENS, "Platform-issued access tokens expire after ten minutes."),
        (_TOKENS, "Platform-issued access tokens expire after at most 10 minutes."),
        (_LOGS, "The platform team retains application logs for 7 days."),
        (_LOGS, "The platform team retains application logs for seven days."),
        (_PUMPS, "Pumps in hall 2 are switched off at 19:00."),
        ("Tokens expire after at least 10 minutes.", "Tokens expire after 20 minutes."),
    ],
)
def test_a_bare_value_inside_the_bound_is_a_tightening(parent: str, child: str) -> None:
    assert inherited_conflict(child, parent) is None


@pytest.mark.parametrize(
    ("parent", "child"),
    [
        (_TOKENS, "Platform-issued access tokens expire after 20 minutes."),
        (_TOKENS, "Platform tokens expire after at least 10 minutes."),
        (_LOGS, "The platform team retains application logs for 30 days."),
        (_PUMPS, "Pumps in hall 2 are switched off at 21:00."),
        ("Tokens expire after at least 10 minutes.", "Tokens expire after 5 minutes."),
    ],
)
def test_a_bare_value_outside_the_bound_or_under_another_comparator_still_conflicts(
    parent: str, child: str
) -> None:
    assert inherited_conflict(child, parent) is not None


def test_two_different_same_unit_values_are_not_guessed() -> None:
    # Ambiguous: which one is the retention? Fails toward the rule.
    ok, _reason = _tighten_value_check(_LOGS, "Logs are kept 7 days locally and 30 days offsite.")
    assert not ok


def test_the_bare_value_must_have_the_parents_unit() -> None:
    ok, _reason = _tighten_value_check(_TOKENS, "Tokens expire after 10 hours.")
    assert not ok


# ---------------------------------------------------------------------------
# Fix 2: the engine locates the kept value; the model's span is a hint
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock  # noqa: E402

from strata.scope_manager import (  # noqa: E402
    relation_decline_scope_trigger,
    verify_relation_ground,
)

from .test_v17_relation_recheck import (  # noqa: E402
    ANCESTOR_DIRECTIVE,
    ANCESTOR_DIRECTIVES,
    _judge,
    _ordinary_decline,
    _reask_response,
)

_ALERT_PARENT = ANCESTOR_DIRECTIVE.content  # "... at or below 500 ms p99 latency."


def _tightens(kept_span: str) -> dict:
    return {
        "relation": "tightens",
        "other_grounds_clear": True,
        "parent_id": "p",
        "tighten_kind": "rule",
        "kept_span": kept_span,
    }


@pytest.mark.parametrize(
    "kept_span",
    [
        "Alerting escalates to SEV-1 at or below 500 ms p99 latency.",  # quotes the parent
        "alerts go to SEV-1 once latency is under 400 ms",  # paraphrase
    ],
)
def test_the_engine_locates_the_kept_value_when_the_models_span_is_not_verbatim(
    kept_span: str,
) -> None:
    content = "The platform team escalates to SEV-1 at 400 ms p99 latency."
    ok, result, classification, _ctx = verify_relation_ground(
        _tightens(kept_span), _ALERT_PARENT, content, "directive"
    )
    assert ok is True, result
    assert classification == "directive"
    assert "located by the engine" in result


def test_a_non_verbatim_span_over_a_looser_value_still_declines() -> None:
    content = "The platform team escalates to SEV-1 at 900 ms p99 latency."
    ok, result, _c, _x = verify_relation_ground(
        _tightens("escalates at 400 ms"), _ALERT_PARENT, content, "directive"
    )
    assert ok is False
    assert "kept_span not verbatim" in result


def test_a_non_verbatim_span_with_no_value_in_the_text_declines() -> None:
    ok, result, _c, _x = verify_relation_ground(
        _tightens("something else entirely"),
        _ALERT_PARENT,
        "The platform team escalates quickly.",
        "directive",
    )
    assert ok is False


def test_a_non_verbatim_span_does_not_bypass_the_exemption_guards() -> None:
    content = "The platform team escalates to SEV-1 at 400 ms p99 latency, except for batch jobs."
    ok, result, _c, _x = verify_relation_ground(
        _tightens("not in the text"), _ALERT_PARENT, content, "directive"
    )
    assert ok is False
    assert "exemption" in result


def test_a_verbatim_span_that_truncates_a_number_still_declines() -> None:
    content = "Escalates to SEV-1 at 400 ms p99 latency."
    ok, result, _c, _x = verify_relation_ground(
        _tightens("at 4"), _ALERT_PARENT, content, "directive"
    )
    assert ok is False
    assert "truncates" in result


def test_the_whole_flow_rescues_a_paraphrased_span() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_decline(f"Contradicts {ANCESTOR_DIRECTIVE.id}."),
        _reask_response(
            relation="tightens",
            tighten_kind="rule",
            kept_span="Alerting escalates to SEV-1 at or below 500 ms p99 latency",
            classification="directive",
        ),
    ]
    judgment, _manager = _judge(mock_client, "The platform team escalates to SEV-1 at 400 ms.")
    assert judgment.decision == "accept_as_directive"
    assert judgment.relation_recheck["relation"] == "tightens"


# ---------------------------------------------------------------------------
# Fix 3: a decline naming the ancestor SCOPE, not the directive id
# ---------------------------------------------------------------------------


def test_scope_trigger_picks_the_directive_sharing_the_subject() -> None:
    reasoning = "The ancestor scope g_relparent has already established this."
    assert (
        relation_decline_scope_trigger(
            reasoning, "Alerting escalates at 400 ms.", ANCESTOR_DIRECTIVES
        )
        == ANCESTOR_DIRECTIVE.id
    )


@pytest.mark.parametrize(
    ("reasoning", "content"),
    [
        ("No scope is named here.", "Alerting escalates at 400 ms."),
        ("The ancestor scope g_relparent says so.", "The standup is at 09:30."),
        ("The ancestor scope g_relparentX says so.", "Alerting escalates at 400 ms."),
        (None, "Alerting escalates at 400 ms."),
    ],
)
def test_scope_trigger_needs_both_the_scope_and_a_shared_word(
    reasoning: str | None, content: str
) -> None:
    assert relation_decline_scope_trigger(reasoning, content, ANCESTOR_DIRECTIVES) is None


def test_the_whole_flow_fires_the_reask_on_a_scope_naming_decline() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_decline("The ancestor scope g_relparent has already established this rule."),
        _reask_response(
            relation="tightens",
            tighten_kind="rule",
            kept_span="x",
            classification="directive",
        ),
    ]
    judgment, _manager = _judge(mock_client, "Alerting escalates to SEV-1 at 400 ms p99 latency.")
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_directive"


def test_no_reask_when_the_decline_names_neither_id_nor_scope() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [_ordinary_decline("Not relevant to this scope.")]
    judgment, _manager = _judge(mock_client, "Alerting escalates to SEV-1 at 400 ms p99 latency.")
    assert mock_client.messages.create.call_count == 1
    assert judgment.decision == "decline"


# ---------------------------------------------------------------------------
# Step 4: comparator reach (durations, clock words)
# ---------------------------------------------------------------------------

_BORROW = "Archive volumes may be borrowed for a maximum of 14 days."
_PUMP_EVENING = "Fuel dock pumps must be switched off at or before 20:00 every evening."


@pytest.mark.parametrize(
    "child",
    [
        "Archive volumes may be borrowed for a fortnight.",
        "Archive volumes may be borrowed for one week.",
        "Archive volumes may be borrowed for 2 weeks.",
        "Archive volumes may be borrowed for 7 days.",
    ],
)
def test_a_duration_in_another_unit_inside_the_bound_is_a_tightening(child: str) -> None:
    assert inherited_conflict(child, _BORROW) is None


@pytest.mark.parametrize(
    "child",
    [
        "Archive volumes may be borrowed for two months.",
        "Archive volumes may be borrowed for three weeks.",
        "Archive volumes may be borrowed for 1 year.",
    ],
)
def test_a_duration_in_another_unit_outside_the_bound_conflicts(child: str) -> None:
    assert inherited_conflict(child, _BORROW) is not None


def test_a_bare_hour_word_after_noon_is_read_as_the_evening_hour() -> None:
    late = "Close at or before 20:00."
    assert inherited_conflict("Pumps close at ten.", late) is not None
    assert inherited_conflict("Pumps close at six.", late) is None
    early = "Close at or before 09:00."
    assert inherited_conflict("Pumps close at ten.", early) is not None
    assert inherited_conflict("Pumps close at eight.", early) is None


# ---------------------------------------------------------------------------
# Step 3: the verifier
# ---------------------------------------------------------------------------

from strata.scope_manager import _relaxation_marker, verify_directive_relation  # noqa: E402

_SUITE = ("c_suite", "g_org", "Run the full test suite before publishing a release.")
_DOCK = ("c_dock", "g_ops", _PUMP_EVENING)
_TOKENS_P = ("c_tok", "g_sec", "Access tokens expire after at most 15 minutes.")
_RETAIN_P = ("c_ret", "g_org", "Application logs are retained for at most 14 days.")
_INHERITED = [_SUITE, _DOCK, _TOKENS_P, _RETAIN_P]

_EXCEPTIONS = [
    "Hotfix builds may be published without the full test suite.",
    "Checkout hotfixes skip QA; a green smoke check is enough.",
    "Releases go out on the author's sign-off alone, no second pair of eyes required.",
    "Night shift keeps the fuel dock pumps on until 22:00.",
]
_LEGIT = [
    "The platform team retains application logs for at most 7 days.",
    "Platform-issued access tokens expire after 10 minutes.",
    "The group holds its daily standup at 09:30 CET.",
    "Dry ice deliveries to the cryo room are received by two people.",
    "A release is published only after the full test suite has passed on staging.",
    "Never skip the full test suite before publishing a release.",
]


def _verify(answer: dict, content: str):  # noqa: ANN202
    return verify_directive_relation(answer, content, _INHERITED)


def test_an_exempts_answer_with_a_verbatim_span_declines_and_names_the_parent() -> None:
    content = "Hotfix builds may be published without the full test suite."
    verdict, _reason, target, fallback = _verify(
        {"relation": "exempts", "parent_id": "c_suite", "exempted_case_span": "without the full"},
        content,
    )
    assert (verdict, target, fallback) == ("decline", ("c_suite", "g_org"), False)


@pytest.mark.parametrize("content", _LEGIT)
def test_a_forged_exempts_without_a_verbatim_span_leaves_a_legitimate_row_standing(
    content: str,
) -> None:
    answer = {"relation": "exempts", "parent_id": "c_suite", "exempted_case_span": "no such text"}
    assert _verify(answer, content)[0] == "admit"


@pytest.mark.parametrize("content", _LEGIT)
@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"relation": "unrelated", "parent_id": "c_suite"},
        {"relation": "refines", "parent_id": "c_suite"},
        {"relation": "tightens", "parent_id": "c_suite"},
        {"relation": "contradicts", "parent_id": "c_suite", "exempted_case_span": "zzz"},
    ],
)
def test_no_forged_answer_declines_a_legitimate_row(content: str, answer: dict) -> None:
    assert _verify(answer, content)[0] == "admit"


@pytest.mark.parametrize("content", _EXCEPTIONS)
@pytest.mark.parametrize(
    "answer",
    [
        {"relation": "unrelated", "parent_id": "c_suite"},
        {"relation": "refines", "parent_id": "c_suite"},
        {"relation": "tightens", "parent_id": "c_suite"},
    ],
)
def test_a_forged_unreadable_unrelated_or_refines_answer_still_declines_an_exception(
    content: str, answer: dict
) -> None:
    verdict, reason, target, fallback = _verify(answer, content)
    assert verdict == "decline", reason
    assert target is not None
    assert fallback is False


def test_a_verified_refinement_is_admitted_without_the_fallback_count() -> None:
    content = "Archive staff must log every loan in the register."
    answer = {
        "relation": "refines",
        "parent_id": "c_ret",
        "uncovered_case_span": "log every loan",
        "kept_requirement": "logs are kept at most 14 days",
    }
    assert _verify(answer, content) == ("admit", "verified refinement", None, False)


def test_a_refines_claim_over_text_with_a_marker_does_not_pass() -> None:
    content = "Hotfix builds may be published without the full test suite."
    answer = {
        "relation": "refines",
        "parent_id": "c_suite",
        "uncovered_case_span": "Hotfix builds",
        "kept_requirement": "the suite still runs",
    }
    assert _verify(answer, content)[0] == "decline"


def test_an_unmarked_unreadable_answer_admits_and_is_counted() -> None:
    verdict, _reason, target, fallback = _verify({}, "The cafeteria opens at 08:00.")
    assert (verdict, target, fallback) == ("admit", None, True)


def test_a_marker_with_no_inherited_subject_admits() -> None:
    verdict, _reason, _target, fallback = _verify({}, "Staff may park in the east lot after 6 pm.")
    assert (verdict, fallback) == ("admit", True)


@pytest.mark.parametrize(
    "content",
    [
        "Never skip the review.",
        "There are no exceptions to the sign-off.",
        "Do not work alone in the cryo room.",
        "Archive volumes may be borrowed for a maximum of 7 days.",  # the rule's own phrasing
    ],
)
def test_a_negated_marker_or_the_rules_own_phrasing_is_not_a_relaxation(content: str) -> None:
    assert _relaxation_marker(content, [_BORROW]) is None


@pytest.mark.parametrize(
    "content",
    [
        "Hotfixes may skip the review.",
        "The author's sign-off alone is enough.",
        "Vans turn straight back on the road with no walk-round.",
        "No need to run the suite for docs-only changes.",
    ],
)
def test_the_shared_relaxation_markers_fire(content: str) -> None:
    assert _relaxation_marker(content) is not None


def test_a_polarity_reversal_on_a_shared_object_subject_is_a_conflict_without_a_marker() -> None:
    verdict, reason, target, _f = _verify({}, "Anyone on nights runs the fuel dock pumps on.")
    assert verdict == "decline"
    assert "polarity" in reason
    assert target == ("c_dock", "g_ops")


def test_two_directives_sharing_only_modals_and_time_words_are_not_the_same_subject() -> None:
    content = "Cuttings in the propagation bench must be misted at 12:00 every day."
    seedlings = ("c_seed", "g_org", "Seedlings must be watered at or before 07:00 every day.")
    assert verify_directive_relation({}, content, [seedlings])[0] == "admit"


def test_the_door_an_unreadable_answer_over_a_markered_exception_sharing_no_word_is_admitted() -> (
    None
):
    """Stated limit: no named parent and no shared content word means no subject to decline on."""
    content = "Checkout hotfixes skip QA; a green smoke check is enough."
    verdict, _reason, target, fallback = _verify({}, content)
    assert (verdict, target, fallback) == ("admit", None, True)


# ---------------------------------------------------------------------------
# Step 3: the whole flow through judge() and judge_batch()
# ---------------------------------------------------------------------------

from dataclasses import replace  # noqa: E402

from strata.fleet_config import Scope, Stratum  # noqa: E402
from strata.record_store import Contribution, ContributorRef  # noqa: E402
from strata.scope_manager import ScopeManager  # noqa: E402
from strata.summary_store import Directive, ScopeSummary  # noqa: E402

from .test_scope_manager import _fake_response  # noqa: E402

_SCOPE = Scope(id="g_web", name="Web", stratum_id="L1")
_STRATUM = Stratum(id="L1", name="Team", ordinal=1)
_PARENT = Directive(
    id="c_suite",
    content="Run the full test suite before publishing a release.",
    source_scope_id="g_org",
    created_at="2026-09-01T09:00:00+00:00",
)
_ANCESTORS = [("g_org", [_PARENT])]
_SUMMARY = ScopeSummary(
    scope_id="g_web", directives=[], context="", updated_at="2026-09-01T09:00:00+00:00"
)
_ME = ContributorRef(
    scope_id="g_web", skill="lead", session_id="s1", ts="2026-10-01T09:00:00+00:00"
)
_CONTRIBUTION = Contribution(
    id="c_new",
    scope_id="g_web",
    content="Hotfix builds may be published without the full test suite.",
    proposed_classification="directive",
    subject=None,
    supersedes=None,
    contributor=_ME,
    created_at="2026-10-01T09:00:00+00:00",
)
_ADMIT = {
    "decision": "accept_as_directive",
    "reasoning": "A rule.",
    "directive_ops": [{"op": "append"}],
    "new_context": None,
}
_EXEMPTS = {
    "relation": "exempts",
    "parent_id": "c_suite",
    "exempted_case_span": "without the full test suite",
    "reasoning": "skips the suite",
}
_OWNER_NOTE = (
    "Contradicts inherited c_suite (g_org). An exception to it is g_org's to make; "
    "propose it there."
)


def _flow(content: str, reask, **overrides):  # noqa: ANN001, ANN003, ANN202
    client = MagicMock()
    steps = [_fake_response(_ADMIT)]
    if reask is not None:
        steps.append(reask if isinstance(reask, Exception) else _fake_response(reask))
    client.messages.create.side_effect = steps
    contribution = replace(_CONTRIBUTION, content=content, **overrides.pop("contribution", {}))
    kwargs = {
        "scope": _SCOPE,
        "stratum": _STRATUM,
        "current_summary": _SUMMARY,
        "recent_contributions": [],
        "new_contribution": contribution,
        "ancestor_directives": _ANCESTORS,
    }
    kwargs.update(overrides)
    judgment = ScopeManager(client=client).judge(**kwargs)
    return judgment, client


def _names(client) -> list[str]:  # noqa: ANN001
    return [c.kwargs["tools"][0]["name"] for c in client.messages.create.call_args_list]


def test_an_admitted_exception_is_declined_with_the_owner_reason() -> None:
    judgment, client = _flow(_CONTRIBUTION.content, _EXEMPTS)
    assert _names(client) == ["submit_judgment", "classify_directive_relation"]
    assert judgment.decision == "decline"
    assert judgment.reasoning == _OWNER_NOTE
    assert judgment.directive_ops == []
    assert judgment.new_summary is None and judgment.new_context is None
    assert judgment.directive_relation["verdict"] == "decline"
    assert judgment.directive_relation["fallback"] is False


@pytest.mark.parametrize(
    "content",
    [
        "The platform team retains application logs for at most 7 days.",
        "A release is published only after the full test suite has passed on staging.",
    ],
)
def test_a_legitimate_directive_is_admitted_after_one_reask(content: str) -> None:
    judgment, client = _flow(
        content, {"relation": "unrelated", "parent_id": "c_suite", "reasoning": "other subject"}
    )
    assert _names(client) == ["submit_judgment", "classify_directive_relation"]
    assert judgment.decision == "accept_as_directive"
    assert [op.op for op in judgment.directive_ops] == ["append"]
    assert judgment.directive_relation["verdict"] == "admit"


def test_a_failed_reask_with_a_marker_still_declines_and_without_one_admits() -> None:
    judgment, _client = _flow(_CONTRIBUTION.content, RuntimeError("provider down"))
    assert judgment.decision == "decline"
    assert "re-ask failed" in judgment.directive_relation["reason"]
    judgment, _client = _flow("The cafeteria opens at 08:00.", RuntimeError("provider down"))
    assert judgment.decision == "accept_as_directive"
    assert judgment.directive_relation["fallback"] is True


def test_the_first_judge_call_is_unchanged_by_the_reask() -> None:
    _j, client = _flow(_CONTRIBUTION.content, _EXEMPTS)
    reask = client.messages.create.call_args_list[1].kwargs
    assert reask["max_tokens"] == 400
    assert reask["tool_choice"]["name"] == "classify_directive_relation"
    text = reask["messages"][0]["content"]
    assert text.startswith("INHERITED DIRECTIVES:\n- c_suite: Run the full test suite")
    assert "NEW DIRECTIVE:\nHotfix builds may be published" in text


def test_no_reask_without_inherited_directives_or_for_another_position_or_mode() -> None:
    judgment, client = _flow(_CONTRIBUTION.content, None, ancestor_directives=None)
    assert _names(client) == ["submit_judgment"]
    assert judgment.directive_relation is None
    other = ContributorRef(scope_id="g_elsewhere", skill="x", session_id="s", ts="2026-10-01")
    judgment, client = _flow(_CONTRIBUTION.content, None, contribution={"contributor": other})
    assert _names(client) == ["submit_judgment"]
    judgment, client = _flow(_CONTRIBUTION.content, None, mode="input_change_refresh")
    assert _names(client) == ["submit_judgment"]


def test_no_reask_for_a_decline_a_context_admit_or_a_hold() -> None:
    for first in (
        {"decision": "decline", "reasoning": "no", "directive_ops": [], "new_context": None},
    ):
        client = MagicMock()
        client.messages.create.side_effect = [_fake_response(first)]
        ScopeManager(client=client).judge(
            scope=_SCOPE,
            stratum=_STRATUM,
            current_summary=_SUMMARY,
            recent_contributions=[],
            new_contribution=_CONTRIBUTION,
            ancestor_directives=_ANCESTORS,
        )
        assert "classify_directive_relation" not in _names(client)
    # a hold (an outright value conflict) is decided without a re-ask
    parent = _PARENT.model_copy(
        update={"content": "Access tokens expire after at most 15 minutes."}
    )
    judgment, client = _flow(
        "Access tokens expire after 20 minutes.",
        None,
        ancestor_directives=[("g_org", [parent])],
    )
    assert _names(client) == ["submit_judgment"]
    assert judgment.decision == "decline"
    assert judgment.inherited_holds


def test_a_batch_declines_the_exception_member_and_keeps_the_other() -> None:
    from .test_inherited_check import _batch_input  # noqa: PLC0415

    first = replace(_CONTRIBUTION, id="c_a", content=_CONTRIBUTION.content)
    second = replace(
        _CONTRIBUTION, id="c_b", content="The group holds its daily standup at 09:30 CET."
    )
    client = MagicMock()
    client.messages.create.side_effect = [
        _fake_response(
            _batch_input(
                verdicts=[
                    {"contribution_id": "c_a", "decision": "accept_as_directive", "reasoning": "a"},
                    {"contribution_id": "c_b", "decision": "accept_as_directive", "reasoning": "b"},
                ],
                directive_ops=[
                    {"op": "append", "contribution_id": "c_a"},
                    {"op": "append", "contribution_id": "c_b"},
                ],
                new_context="Judge's context.",
            )
        ),
        _fake_response(_EXEMPTS),
        _fake_response({"relation": "unrelated", "parent_id": "c_suite", "reasoning": "x"}),
    ]
    result = ScopeManager(client=client).judge_batch(
        scope=_SCOPE,
        stratum=_STRATUM,
        current_summary=_SUMMARY,
        recent_contributions=[],
        new_contributions=[first, second],
        ancestor_directives=_ANCESTORS,
    )
    decisions = {v.contribution_id: v.decision for v in result.verdicts}
    assert decisions == {"c_a": "decline", "c_b": "accept_as_directive"}
    assert result.verdicts[0].reasoning == _OWNER_NOTE
    assert [op.contribution_id for op in result.directive_ops] == ["c_b"]
    assert len(result.directive_relations) == 2
    assert result.new_summary is not None
    assert [d.content for d in result.new_summary.directives] == [second.content]


# ---------------------------------------------------------------------------
# j1-050: an enforcement of the rule is not a narrowing of it
# ---------------------------------------------------------------------------

_DEPLOY_RULE = "All deploys require a rollback plan documented in the deploy ticket."


def test_an_enforcement_point_for_an_all_noun_rule_is_not_a_narrowing() -> None:
    enforcing = (
        "Deploy tickets must include a filled-out rollback-plan section before merge is allowed."
    )
    assert inherited_conflict(enforcing, _DEPLOY_RULE) is None


def test_a_narrowing_that_relaxes_or_drops_the_requirement_is_still_a_conflict() -> None:
    assert (
        inherited_conflict(
            "Deploys of the billing service may skip the rollback plan.", _DEPLOY_RULE
        )
        is not None
    )
    assert inherited_conflict("Staging deploys need a plan.", _DEPLOY_RULE) is not None
    # the same noun restated for a part of it is still read as a narrowing
    assert (
        inherited_conflict(
            "Hotfix deploys require a rollback plan documented in the deploy ticket.", _DEPLOY_RULE
        )
        is not None
    )


def test_j1_050_is_admitted_end_to_end_when_the_reask_calls_it_a_tightening() -> None:
    parent = Directive(
        id="c_pe102",
        content=_DEPLOY_RULE,
        source_scope_id="s_platform_eng",
        created_at="2026-05-11T09:00:00+00:00",
    )
    text = "Deploy tickets must include a filled-out rollback-plan section before merge is allowed."
    client = MagicMock()
    client.messages.create.side_effect = [
        _fake_response(
            {
                "decision": "accept_as_directive",
                "reasoning": "Enforces the parent rule locally.",
                "directive_ops": [{"op": "publish", "content": text}],
                "new_context": None,
            }
        ),
        _fake_response(
            {
                "relation": "tightens",
                "parent_id": "c_pe102",
                "reasoning": "adds an enforcement point",
            }
        ),
    ]
    judgment = ScopeManager(client=client).judge(
        scope=_SCOPE,
        stratum=_STRATUM,
        current_summary=_SUMMARY,
        recent_contributions=[],
        new_contribution=replace(_CONTRIBUTION, content=text),
        ancestor_directives=[("s_platform_eng", [parent])],
    )
    assert judgment.decision == "accept_as_directive"
    assert judgment.inherited_holds == []
    assert judgment.directive_relation["verdict"] == "admit"


# ---------------------------------------------------------------------------
# The synonym gap: "sign off" and "approvals" are the same act
# ---------------------------------------------------------------------------

from strata.scope_manager import _context_covers, verify_inherited_relation  # noqa: E402

_TWO_APPROVALS = "Releases require two approvals."


@pytest.mark.parametrize(
    "text",
    [
        "Anyone may sign off on a release.",
        "Any engineer can approve a release.",
        "Ops can authorize a release alone.",
    ],
)
def test_sign_off_and_approval_are_one_subject(text: str) -> None:
    assert _context_covers(_TWO_APPROVALS, text)
    verdict, _reason = verify_inherited_relation(
        {"kind": "unrelated", "inherited_id": "p"}, text, _TWO_APPROVALS
    )
    assert verdict == "decline"


def test_an_unrelated_sentence_does_not_gain_a_subject_from_the_synonyms() -> None:
    assert not _context_covers(_TWO_APPROVALS, "The cafeteria opens at 08:00 on weekdays.")
