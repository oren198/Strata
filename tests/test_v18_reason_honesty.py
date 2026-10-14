"""Judge reason honesty (v1.18): a decline names its span; an informant the reasoning names
must occur in the contribution; reading an artifact first-hand is a telling event."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from strata.fleet_config import Scope, Stratum
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import (
    ScopeManager,
    _drop_false_hearsay,
    _informant_names,
    _informants_missing_from,
    _telling_span_problem_for,
)
from strata.summary_store import ScopeSummary

from .test_scope_manager import _fake_response

# -- 1. each verifier failure reason contains the span verbatim --------------

_TEXT = "Per the architect, the budget is frozen. The CEO told me the README is mine to draft."


@pytest.mark.parametrize(
    ("telling_span", "content", "fragment"),
    [
        ("the architect said so", "no such words here", "invented informant"),
        ("from the architect", "from the architect, budgets are frozen", "has no telling event"),
        (
            "said budgets are frozen",
            "the lead said budgets are frozen",
            "names no one it was told to",
        ),
        ("as discussed", "as discussed, ship it", "has no telling event"),
    ],
)
def test_every_telling_failure_names_the_informant_and_the_span(
    telling_span: str, content: str, fragment: str
) -> None:
    problem, kind = _telling_span_problem_for(
        telling_span, content=content, informant_span="the architect"
    )
    assert kind is None
    assert problem is not None and fragment in problem
    assert "'the architect'" in problem
    assert repr(telling_span) in problem or telling_span in problem


def test_without_an_informant_span_the_reasons_still_read() -> None:
    problem, _ = _telling_span_problem_for("from the architect", content="from the architect, x")
    assert problem is not None and "the claim has no telling event" in problem


# -- 2b. reading an artifact is a telling event ------------------------------

_MARY_2 = (
    "2026-10-12: I drafted it in .work/marketing/readme-judge-models.md (not in the repo) from "
    "the architect's facts file (.work/judge-models-facts.md), which is provisional."
)
_MARY_SPAN = (
    "I drafted it in .work/marketing/readme-judge-models.md (not in the repo) from the "
    "architect's facts file"
)


def test_a_first_person_reading_with_the_artifacts_locator_is_a_telling_event() -> None:
    problem, kind = _telling_span_problem_for(
        _MARY_SPAN, content=_MARY_2, informant_span="the architect"
    )
    assert (problem, kind) == (None, "document")


def test_a_reading_with_no_locator_after_the_artifact_is_refused() -> None:
    content = _MARY_2.replace(" (.work/judge-models-facts.md)", "")
    problem, kind = _telling_span_problem_for(
        _MARY_SPAN, content=content, informant_span="the architect"
    )
    assert kind is None and problem is not None
    assert "no locator" in problem  # a path elsewhere in the span does not count


def test_the_locator_must_follow_the_attributed_artifact_not_precede_it() -> None:
    content = "I read .work/other.md and the architect's spec, which says X is provisional."
    problem, kind = _telling_span_problem_for(
        "I read .work/other.md and the architect's spec", content=content
    )
    assert kind is None and problem is not None


def test_a_source_attribution_without_a_first_person_act_is_refused() -> None:
    problem, kind = _telling_span_problem_for(
        "from the architect's facts file", content=_MARY_2, informant_span="the architect"
    )
    assert kind is None
    assert "no first-person reading act" in problem


def test_an_elided_span_is_refused() -> None:
    problem, kind = _telling_span_problem_for(
        "I drafted it ... from the architect's facts file", content=_MARY_2
    )
    assert kind is None and problem.startswith("invented informant")


@pytest.mark.parametrize(
    "text",
    [
        "I read the architect's spec (docs/spec.md) and it marks the table provisional.",
        "I reviewed the platform team's report at reports/q3.pdf and it lists 12 incidents.",
        "I opened the risk page by Dana (https://wiki.example/risk) and it shows three owners.",
    ],
)
def test_other_accepted_forms(text: str) -> None:
    span = text.split(" and ")[0]
    problem, kind = _telling_span_problem_for(span, content=text)
    assert (problem, kind) == (None, "document"), problem


# -- the judge flow for a document telling event -----------------------------

_STAMP = "2026-10-12T10:00:00+00:00"
_SCOPE = Scope(id="g_mkt", name="Marketing", stratum_id="L1")
_STRATUM = Stratum(id="L1", name="Team", ordinal=1)
_SUMMARY = ScopeSummary(scope_id="g_mkt", directives=[], context="", updated_at=_STAMP)
_ME = ContributorRef(scope_id="g_mkt", skill="marketing", session_id="s", ts=_STAMP)


def _contribution(content: str) -> Contribution:
    return Contribution(
        id="c_new",
        scope_id="g_mkt",
        content=content,
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=_ME,
        created_at=_STAMP,
    )


def _accept(reasoning: str, new_context: str = "ctx") -> MagicMock:
    return _fake_response(
        {
            "decision": "accept_as_context",
            "reasoning": reasoning,
            "directive_ops": [],
            "new_context": new_context,
        }
    )


def test_a_document_reading_is_admitted_as_the_authors_word_as_of_the_reading() -> None:
    from .test_225_interior_assertions import _judge, _ordinary_accept  # noqa: PLC0415

    content = "I read 225-other-scope's spec (docs/spec.md) and it marks the table provisional."
    client = MagicMock()
    client.messages.create.side_effect = [
        _ordinary_accept(content),
        _fake_response(
            {
                "classification": "informant",
                "informant_span": "225-other-scope",
                "telling_span": "I read 225-other-scope's spec (docs/spec.md)",
                "reasoning": "reads a file",
            }
        ),
    ]
    judgment, _ = _judge(client, content)
    assert judgment.decision == "accept_as_context"
    assert "reports reading 225-other-scope's document" in judgment.new_context
    assert "as of that reading it says" in judgment.new_context
    assert judgment.interior_assertion["result"] == "admitted (context replaced)"


def test_a_reading_with_no_locator_is_still_declined_through_the_judge() -> None:
    from .test_225_interior_assertions import _judge, _ordinary_accept  # noqa: PLC0415

    content = "I read 225-other-scope's spec and it marks the table provisional."
    client = MagicMock()
    client.messages.create.side_effect = [
        _ordinary_accept(content),
        _fake_response(
            {
                "classification": "informant",
                "informant_span": "225-other-scope",
                "telling_span": "I read 225-other-scope's spec",
                "reasoning": "reads a file",
            }
        ),
    ]
    judgment, _ = _judge(client, content)
    assert judgment.decision == "decline"
    assert "the claim attributed to '225-other-scope'" in judgment.reasoning
    assert "no locator" in judgment.reasoning


# -- 3. an informant the reasoning names must occur in the contribution -------


def test_informant_extraction_strips_articles_and_role_words() -> None:
    assert _informant_names("Hearsay: the contributor reports being told by the CEO.") == ["ceo"]
    assert _informant_names("Hearsay: a decision relayed, told by Dana Lee.") == ["dana lee"]
    assert _informant_names("The CEO (a named informant) relayed it.") == ["ceo"]
    assert _informant_names("It rests on informant the architect.") == ["architect"]
    assert _informant_names("A plain first-hand report.") == []


def test_a_named_informant_present_in_the_text_is_not_missing() -> None:
    reasoning = "Hearsay: the contributor reports being told by the CEO."
    assert _informants_missing_from(reasoning, "The CEO told me to draft it.") == []
    assert _informants_missing_from(reasoning, "Marketing drafted the README section.") == ["ceo"]


def test_a_false_hearsay_label_on_a_first_hand_reasoning_is_dropped() -> None:
    assert (
        _drop_false_hearsay("Hearsay: this is a first-hand report of what the writer did.")
        == "this is a first-hand report of what the writer did."
    )
    assert _drop_false_hearsay("Hearsay: told by the CEO.") == "Hearsay: told by the CEO."


def _judge_plain(client, content: str):  # noqa: ANN001, ANN202
    return ScopeManager(client=client).judge(
        scope=_SCOPE,
        stratum=_STRATUM,
        current_summary=_SUMMARY,
        recent_contributions=[],
        new_contribution=_contribution(content),
    )


_NO_CEO = "Marketing will ship the README Judge-models section after the baseline."


def test_a_reasoning_naming_an_absent_informant_gets_one_reask_then_an_engine_reason() -> None:
    client = MagicMock()
    ceo = "Hearsay: the contributor reports being told by the CEO that this is approved."
    client.messages.create.side_effect = [_accept(ceo), _accept(ceo)]
    judgment = _judge_plain(client, _NO_CEO)
    assert client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_context"  # the verdict stands
    assert judgment.reasoning == (
        "Reason withdrawn: the judge described content that is not in this contribution "
        "(ceo). Verdict: accept_as_context."
    )
    assert any("named an informant that does not occur" in n for n in judgment.protocol_notes)
    assert "CEO" not in judgment.record_notes.split("Reason withdrawn")[0]


def test_the_reask_can_fix_the_reasoning_and_then_nothing_is_withdrawn() -> None:
    client = MagicMock()
    client.messages.create.side_effect = [
        _accept("Hearsay: told by the CEO."),
        _accept("Marketing plans to ship a README section."),
    ]
    judgment = _judge_plain(client, _NO_CEO)
    assert judgment.reasoning == "Marketing plans to ship a README section."
    assert client.messages.create.call_count == 2
    second = client.messages.create.call_args_list[1].kwargs["messages"]
    assert "does not appear in the NEW CONTRIBUTION" in str(second)
    assert "ceo" in str(second)


def test_a_named_informant_present_in_the_text_makes_no_reask() -> None:
    client = MagicMock()
    client.messages.create.side_effect = [_accept("Hearsay: told by the CEO.")]
    judgment = _judge_plain(client, "The CEO told me the README is mine to draft.")
    assert client.messages.create.call_count == 1
    assert judgment.reasoning == "Hearsay: told by the CEO."


def test_the_first_judge_call_is_unchanged_by_the_check() -> None:
    client = MagicMock()
    client.messages.create.side_effect = [_accept("Hearsay: told by the CEO."), _accept("ok")]
    _judge_plain(client, _NO_CEO)
    plain = MagicMock()
    plain.messages.create.side_effect = [_accept("ok")]
    _judge_plain(plain, _NO_CEO)
    first = client.messages.create.call_args_list[0].kwargs
    reference = plain.messages.create.call_args_list[0].kwargs
    for key in ("system", "tools", "tool_choice", "max_tokens", "messages"):
        assert first[key] == reference[key]


def test_the_batch_path_checks_each_members_reasoning_against_its_own_text() -> None:
    from .test_inherited_check import _batch_input  # noqa: PLC0415

    a = replace(_contribution("The CEO told me the README is mine."), id="c_a")
    b = replace(_contribution(_NO_CEO), id="c_b")
    first = _fake_response(
        _batch_input(
            verdicts=[
                {
                    "contribution_id": "c_a",
                    "decision": "accept_as_context",
                    "reasoning": "Hearsay: told by the CEO.",
                },
                {
                    "contribution_id": "c_b",
                    "decision": "accept_as_context",
                    "reasoning": "Hearsay: told by the CEO.",
                },
            ],
            directive_ops=[],
            new_context="ctx",
        )
    )
    client = MagicMock()
    client.messages.create.side_effect = [first, first]
    result = ScopeManager(client=client).judge_batch(
        scope=_SCOPE,
        stratum=_STRATUM,
        current_summary=_SUMMARY,
        recent_contributions=[],
        new_contributions=[a, b],
    )
    assert client.messages.create.call_count == 2
    by_id = {v.contribution_id: v for v in result.verdicts}
    assert by_id["c_a"].reasoning == "Hearsay: told by the CEO."
    assert by_id["c_b"].reasoning.startswith("Reason withdrawn:")
    assert {v.decision for v in result.verdicts} == {"accept_as_context"}


# -- review fixes: extraction, the re-ask adopts only the reasoning, negation, the switch --


@pytest.mark.parametrize(
    ("reasoning", "expected"),
    [
        ("No informant is named in this contribution.", []),
        ("No informant from another team is cited.", []),
        ("The text names no informant or operator directive.", []),
        ("Not an informant claim; this is not applicable.", []),
        ("Any informant would be unnamed here.", []),
        ("Informant is not applicable: this is a plain fact.", []),
        ("The informant 'night crew' told the agent the shift ends at six.", ["night crew"]),
        ("Informant Tom told the agent he would check.", ["tom"]),
        ("Hearsay: the contributor reports being told by two people.", ["two people"]),
    ],
)
def test_informant_extraction_has_no_false_names(reasoning: str, expected: list[str]) -> None:
    assert _informant_names(reasoning) == expected


def test_a_true_informant_in_the_text_is_never_withdrawn() -> None:
    for reasoning, text in (
        ("The informant 'night crew' told the agent the shift ends at six.", "night crew said six"),
        ("Informant Tom told the agent he would check.", "Tom told me he would check the rota."),
        (
            "Hearsay: the contributor reports being told by two people.",
            "Two people told me the lift is out.",
        ),
    ):
        assert _informants_missing_from(reasoning, text) == [], reasoning


def test_the_reask_takes_only_the_reasoning_from_the_retry() -> None:
    client = MagicMock()
    first = _accept("Hearsay: told by the CEO.", new_context="first context")
    retry = _fake_response(
        {
            "decision": "accept_as_context",
            "reasoning": "Marketing plans to ship a README section.",
            "directive_ops": [{"op": "append"}],  # a retry that tries to add a directive
            "new_context": "retry context",
        }
    )
    client.messages.create.side_effect = [first, retry]
    judgment = _judge_plain(client, _NO_CEO)
    assert judgment.reasoning == "Marketing plans to ship a README section."
    assert judgment.directive_ops == []
    assert judgment.new_context == "first context"
    assert [d.id for d in judgment.new_summary.directives] == []


def test_not_first_hand_keeps_its_hearsay_label() -> None:
    text = "Hearsay: not first-hand; told by the CEO."
    assert _drop_false_hearsay(text) == text
    assert _drop_false_hearsay("Hearsay: no first-hand report, told by Dana.").startswith(
        "Hearsay:"
    )


def test_the_switch_on_wires_the_check_and_off_makes_no_extra_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import strata.scope_manager as sm  # noqa: PLC0415

    ceo = "Hearsay: the contributor reports being told by the CEO that this is approved."
    monkeypatch.setattr(sm, "_REASON_INFORMANT_CHECK", True)
    on = MagicMock()
    on.messages.create.side_effect = [_accept(ceo), _accept(ceo)]
    on_judgment = _judge_plain(on, _NO_CEO)
    assert on.messages.create.call_count == 2
    assert on_judgment.reasoning.startswith("Reason withdrawn:")

    monkeypatch.setattr(sm, "_REASON_INFORMANT_CHECK", False)
    off = MagicMock()
    off.messages.create.side_effect = [_accept(ceo)]
    off_judgment = _judge_plain(off, _NO_CEO)
    assert off.messages.create.call_count == 1
    assert off_judgment.reasoning == ceo  # untouched
    hearsay = MagicMock()
    hearsay.messages.create.side_effect = [_accept("Hearsay: this is a first-hand report.")]
    assert _judge_plain(hearsay, _NO_CEO).reasoning == "Hearsay: this is a first-hand report."
