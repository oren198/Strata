"""v1.17.1 — a child may tighten an inherited rule, never change it.

After an ordinary judgment admits a directive owned by a session bound to the
judged scope, the engine checks the admitted text against every rendered
inherited directive (ancestor and operator). A covered subject must pass the
same tighten test the relation re-check uses; a conflict holds the
contribution as context under an engine-written note. Mechanical: no judge
call, no judge-input change.
"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from strata.record_store import ContributorRef
from strata.scope_manager import (
    Directive,
    OperatorItem,
    ScopeManager,
    inherited_conflict,
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

PARENT_RULE = "All services must use TLS 1.3 or later."
DEPLOY_RULE = "Deploys must finish within 5 minutes."

ANCESTOR = Directive(
    id="c_par001",
    content=PARENT_RULE,
    subject="tls",
    source_scope_id="g_arch",
    source_skill="architect",
    created_at="2026-03-01T09:00:00+00:00",
)
ANCESTORS = [("g_arch", [ANCESTOR])]
OPERATOR = OperatorItem(
    id="op_tls123",
    kind="directive",
    content=PARENT_RULE,
    subject="tls",
    created_at="2026-01-01T00:00:00+00:00",
)
OPERATOR_MEMORY = [("g_exec", [OPERATOR])]

NOTE = (
    "[Held: conflicts with inherited directive c_par001 (g_arch); "
    "a child may tighten an inherited rule, not change it.]"
)
OP_NOTE = (
    "[Held: conflicts with inherited directive op_tls123 (operator, g_exec); "
    "a child may tighten an inherited rule, not change it.]"
)

# -- the pure function ------------------------------------------------------


@pytest.mark.parametrize(
    ("op_text", "reason"),
    [
        ("All services, except billing, must use TLS 1.3 or later.", "exemption language"),
        ("All services must use TLS 1.2 or later.", "parent's value not kept"),
        (
            "The TLS rule for services is outdated; services may use TLS 1.2.",
            "asserts the inherited directive is outdated",
        ),
        ("Most services must use TLS 1.3 or later.", "quantifier softened"),
        ("All services must not use TLS 1.3 or later.", "polarity flip"),
        # Dropping the parent's "All" reads as narrowing the rule: held.
        ("Services stay on TLS 1.3 or later.", "narrows when/where the rule applies"),
    ],
)
def test_a_covered_change_is_a_conflict(op_text: str, reason: str) -> None:
    assert inherited_conflict(op_text, PARENT_RULE) == reason


@pytest.mark.parametrize(
    "op_text",
    [
        # An uncovered subject is a genuine refinement.
        "Logging pipelines must retain entries for 30 days.",
        # The fact restated, plus an own constraint.
        "All services must use TLS 1.3 or later and rotate certificates yearly.",
        # A restatement citing the directive's id: the id is a reference, not a value.
        "All services must use TLS 1.3 or later, per operator directive op_tls123.",
    ],
)
def test_an_uncovered_or_restating_text_passes(op_text: str) -> None:
    assert inherited_conflict(op_text, PARENT_RULE) is None


def test_a_tightening_passes_and_a_loosening_does_not() -> None:
    assert inherited_conflict("Deploys must finish within 3 minutes.", DEPLOY_RULE) is None
    assert (
        inherited_conflict("Deploys must finish within 10 minutes.", DEPLOY_RULE)
        == "not stricter in the same direction"
    )


# -- the single path --------------------------------------------------------


def _judge(
    content: str,
    *,
    ancestors=ANCESTORS,  # noqa: ANN001
    operator_memory=None,  # noqa: ANN001
    contributor: ContributorRef | None = None,
    ops: list[dict] | None = None,
):  # noqa: ANN202
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        {
            "decision": "accept_as_directive",
            "reasoning": "An enforceable rule.",
            "directive_ops": ops or [{"op": "append"}],
            "new_context": None,
        }
    )
    contribution = replace(NEW_CONTRIBUTION, content=content, subject=None)
    if contributor is not None:
        contribution = replace(contribution, contributor=contributor)
    judgment = ScopeManager(client=mock_client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=contribution,
        ancestor_directives=ancestors,
        operator_memory=operator_memory,
    )
    return judgment, mock_client, contribution


def test_a_looser_value_is_held_as_context_with_the_engine_note() -> None:
    content = "All services must use TLS 1.2 or later."
    judgment, mock_client, contribution = _judge(content)

    assert mock_client.messages.create.call_count == 1  # no extra judge call
    assert judgment.decision == "accept_as_context"
    assert judgment.directive_ops == []
    assert judgment.reasoning == f"An enforceable rule. {NOTE}"
    assert judgment.new_summary is not None
    assert judgment.new_summary.directives == CURRENT_SUMMARY.directives
    assert judgment.new_summary.context == (
        f"{CURRENT_SUMMARY.context}\n[{contribution.id}] code-writer (g_abc123) "
        f"proposed: {content} — {NOTE}"
    )
    assert judgment.inherited_holds == [
        {
            "contribution_id": contribution.id,
            "directive_id": "c_par001",
            "origin": "g_arch",
            "reason": "parent's value not kept",
        }
    ]


@pytest.mark.parametrize(
    "content",
    [
        "All services, except billing, must use TLS 1.3 or later.",
        "The TLS rule for services is outdated; services may use TLS 1.2.",
        "Most services must use TLS 1.3 or later.",
    ],
)
def test_an_exemption_an_outdating_claim_and_a_softened_universal_are_held(content: str) -> None:
    judgment, _client, _c = _judge(content)
    assert judgment.decision == "accept_as_context"
    assert judgment.directive_ops == []
    assert len(judgment.inherited_holds) == 1


@pytest.mark.parametrize(
    "content",
    [
        "All services must use TLS 1.3 or later and rotate certificates yearly.",
        "Logging pipelines must retain entries for 30 days.",
    ],
)
def test_a_restatement_with_an_own_constraint_and_an_uncovered_refinement_are_admitted(
    content: str,
) -> None:
    judgment, _client, _c = _judge(content)
    assert judgment.decision == "accept_as_directive"
    assert [op.op for op in judgment.directive_ops] == ["append"]
    assert judgment.inherited_holds == []
    assert judgment.new_summary is not None
    assert judgment.new_summary.directives[-1].content == content


def test_a_tightening_is_admitted() -> None:
    parent = ANCESTOR.model_copy(update={"content": DEPLOY_RULE, "subject": "deploys"})
    judgment, _c, _x = _judge(
        "Deploys must finish within 3 minutes.", ancestors=[("g_arch", [parent])]
    )
    assert judgment.decision == "accept_as_directive"
    assert judgment.inherited_holds == []


def test_an_operator_directive_is_an_inherited_source_too() -> None:
    judgment, _c, _x = _judge(
        "All services must use TLS 1.2 or later.", ancestors=None, operator_memory=OPERATOR_MEMORY
    )
    assert judgment.decision == "accept_as_context"
    assert judgment.reasoning == f"An enforceable rule. {OP_NOTE}"
    assert judgment.inherited_holds[0]["origin"] == "operator, g_exec"


def test_an_operator_context_item_is_not_inherited_directive_text() -> None:
    context_item = replace(OPERATOR, kind="context")
    judgment, _c, _x = _judge(
        "All services must use TLS 1.2 or later.",
        ancestors=None,
        operator_memory=[("g_exec", [context_item])],
    )
    assert judgment.decision == "accept_as_directive"


def test_a_publish_op_is_checked_on_its_own_words() -> None:
    judgment, _c, _x = _judge(
        "Agreed.",
        ops=[{"op": "publish", "content": "All services must use TLS 1.2 or later."}],
    )
    assert judgment.decision == "accept_as_context"
    assert judgment.inherited_holds


def test_a_supersede_riding_a_conflicting_append_is_dropped_with_it() -> None:
    judgment, _c, _x = _judge(
        "All services must use TLS 1.2 or later.",
        ops=[{"op": "append"}, {"op": "supersede", "id": "c_old001"}],
    )
    assert judgment.decision == "accept_as_context"
    assert judgment.directive_ops == []
    assert judgment.new_summary is not None
    assert judgment.new_summary.directives == CURRENT_SUMMARY.directives


def test_only_a_session_bound_to_the_judged_scope_is_checked() -> None:
    """Another position is the position gate's, never this check's."""
    other = ContributorRef(
        scope_id="g_elsewhere", skill="code-writer", session_id="s9", ts="2026-05-01T10:00:00+00:00"
    )
    judgment, _c, _x = _judge("All services must use TLS 1.2 or later.", contributor=other)
    assert judgment.inherited_holds == []


def test_a_decline_is_left_alone() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        {"decision": "decline", "reasoning": "no", "directive_ops": [], "new_context": None}
    )
    judgment = ScopeManager(client=mock_client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=replace(NEW_CONTRIBUTION, content="All services must use TLS 1.2."),
        ancestor_directives=ANCESTORS,
    )
    assert judgment.decision == "decline"
    assert judgment.reasoning == "no"
    assert judgment.inherited_holds == []


# -- input identity ---------------------------------------------------------


def test_the_judge_inputs_are_byte_identical_with_inherited_directives_present() -> None:
    """The check is post-judgment only: the first call carries no new input.

    Pinned the way tests/test_same_scope_provenance.py pins it — the same
    fixed scenario hashes to the same digest whether or not the check has
    anything to compare against; ancestors render in the same place as ever.
    """
    calls = []
    for ancestors in (None, ANCESTORS):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _fake_response(
            {
                "decision": "accept_as_context",
                "reasoning": "r",
                "directive_ops": [],
                "new_context": "x",
            }
        )
        ScopeManager(client=mock_client).judge(
            scope=SCOPE,
            stratum=STRATUM,
            current_summary=CURRENT_SUMMARY,
            recent_contributions=[],
            new_contribution=NEW_CONTRIBUTION,
            ancestor_directives=ancestors,
        )
        calls.append(mock_client.messages.create.call_args.kwargs)
    without, with_ancestors = calls
    # The no-ancestor scenario is the pre-1.17.1 pinned one.
    assert _sha_json(without["messages"]) == _SINGLE_MESSAGES_SHA
    assert without["tools"] == with_ancestors["tools"]
    assert without["system"] == with_ancestors["system"]
    assert "c_par001" in json.dumps(with_ancestors["messages"])  # rendered, as before


def test_the_check_adds_no_judge_call_on_a_hold() -> None:
    _j, mock_client, _c = _judge("All services must use TLS 1.2 or later.")
    assert mock_client.messages.create.call_count == 1


# -- the batch path ---------------------------------------------------------


def _batch(contents: tuple[str, str], *, ancestors=ANCESTORS, operator_memory=None):  # noqa: ANN001, ANN202
    first = replace(NEW_CONTRIBUTION, content=contents[0], subject=None)
    second = replace(SECOND_CONTRIBUTION, content=contents[1], subject=None)
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        _batch_input(
            verdicts=[
                {"contribution_id": first.id, "decision": "accept_as_directive", "reasoning": "a"},
                {"contribution_id": second.id, "decision": "accept_as_directive", "reasoning": "b"},
            ],
            directive_ops=[
                {"op": "append", "contribution_id": first.id},
                {"op": "append", "contribution_id": second.id},
            ],
            new_context="Judge's context.",
        )
    )
    result = _judge_batch(
        mock_client,
        contributions=[first, second],
        ancestor_directives=ancestors,
        operator_memory=operator_memory,
    )
    return result, first, second, mock_client


def test_a_batch_holds_only_the_conflicting_member() -> None:
    result, first, second, mock_client = _batch(
        (
            "All services must use TLS 1.2 or later.",
            "Logging pipelines must retain entries for 30 days.",
        )
    )
    assert mock_client.messages.create.call_count == 1
    decisions = {v.contribution_id: v.decision for v in result.verdicts}
    assert decisions == {first.id: "accept_as_context", second.id: "accept_as_directive"}
    assert [op.contribution_id for op in result.directive_ops] == [second.id]
    assert result.verdicts[0].reasoning == f"a {NOTE}"
    assert result.verdicts[1].reasoning == "b"
    assert result.new_context == (
        f"Judge's context.\n[{first.id}] code-writer (g_abc123) proposed: {first.content} — {NOTE}"
    )
    assert result.inherited_holds == [
        {
            "contribution_id": first.id,
            "directive_id": "c_par001",
            "origin": "g_arch",
            "reason": "parent's value not kept",
        }
    ]
    assert result.new_summary is not None
    assert [d.content for d in result.new_summary.directives][-1] == second.content
    assert first.content not in [d.content for d in result.new_summary.directives]


def test_a_batch_without_conflicts_is_untouched() -> None:
    result, first, second, _c = _batch(
        ("Logging pipelines must retain entries for 30 days.", "Backups run nightly.")
    )
    assert result.inherited_holds == []
    assert {v.decision for v in result.verdicts} == {"accept_as_directive"}
    assert result.new_context == "Judge's context."


def test_a_batch_checks_operator_directives_too() -> None:
    result, first, _second, _c = _batch(
        ("All services must use TLS 1.2 or later.", "Backups run nightly."),
        ancestors=None,
        operator_memory=OPERATOR_MEMORY,
    )
    assert result.inherited_holds[0]["origin"] == "operator, g_exec"
    assert result.verdicts[0].reasoning == f"a {OP_NOTE}"


# -- 1.17.2: three false holds the drift set found ---------------------------

PUMPS = "Fuel dock pumps must be switched off at or before 20:00 every evening."
VOLUMES = "Archive volumes may be borrowed for a maximum of 14 days."
SAMPLES = "Cell samples must never be stored at a temperature above -70 °C."


@pytest.mark.parametrize(
    ("op_text", "parent"),
    [
        # (a) an upper bound: the smaller value is the stricter one
        (
            "In the map room, archive volumes may be borrowed for a maximum of 7 days "
            "and are returned in a sealed case.",
            VOLUMES,
        ),
        (
            "In the cryo room, cell samples must never be stored above -80 °C, "
            "and every freezer's temperature is logged twice a day.",
            SAMPLES,
        ),
        # (b) an added own clause is not a polarity flip
        (
            "On night-shift, fuel dock pumps must be switched off at or before 19:30 "
            "every evening and the nozzles locked.",
            PUMPS,
        ),
        # (c) a shared modifier with a different head noun is a different subject
        ("The fuel dock spill kit must be checked every Monday.", PUMPS),
    ],
)
def test_the_four_drift_false_holds_pass(op_text: str, parent: str) -> None:
    assert inherited_conflict(op_text, parent) is None


@pytest.mark.parametrize(
    ("op_text", "parent", "reason"),
    [
        (
            "Archive volumes may be borrowed for a maximum of 21 days.",
            VOLUMES,
            "not stricter in the same direction",
        ),
        (
            "Cell samples must never be stored above -60 °C.",
            SAMPLES,
            "not stricter in the same direction",
        ),
        (
            "On night-shift, fuel dock pumps must be switched off at or before 20:30 "
            "every evening and the nozzles locked.",
            PUMPS,
            "not stricter in the same direction",
        ),
        # The same head noun, narrowing when the rule applies, is still held.
        (
            "Fuel dock pumps must be switched off every Monday.",
            PUMPS,
            "narrows when/where the rule applies",
        ),
    ],
)
def test_the_violating_twins_are_still_held(op_text: str, parent: str, reason: str) -> None:
    assert inherited_conflict(op_text, parent) == reason


@pytest.mark.parametrize(
    ("parent", "op_text", "held"),
    [
        ("Samples must stay below 8 °C.", "Samples must stay below 5 °C.", False),
        ("Samples must stay below 8 °C.", "Samples must stay below 12 °C.", True),
        ("Samples must be kept above 2 °C.", "Samples must be kept above 4 °C.", False),
        ("Samples must be kept above 2 °C.", "Samples must be kept above 1 °C.", True),
        (
            "Samples must never be stored below -80 °C.",
            "Samples must never be stored below -70 °C.",
            False,
        ),
        (
            "Samples must never be stored below -80 °C.",
            "Samples must never be stored below -90 °C.",
            True,
        ),
        ("Loans are for at most 14 days.", "Loans are for at most 10 days.", False),
        ("Loans are for no more than 14 days.", "Loans are for no more than 30 days.", True),
        ("Loans run for up to 14 days.", "Loans run for up to 7 days.", False),
        ("Loans must be a minimum of 3 days.", "Loans must be a minimum of 2 days.", True),
    ],
)
def test_the_direction_comes_from_the_parents_own_words(
    parent: str, op_text: str, held: bool
) -> None:
    assert (inherited_conflict(op_text, parent) is not None) is held


def test_a_restated_polarity_pair_is_held_only_when_the_parents_term_is_reversed() -> None:
    assert inherited_conflict(
        "Pumps must be switched on at 20:00.", "Pumps must be switched off at 20:00."
    )
    assert (
        inherited_conflict(
            "Pumps must be switched off at 20:00 and the valves closed.",
            "Pumps must be switched off at 20:00.",
        )
        is None
    )
