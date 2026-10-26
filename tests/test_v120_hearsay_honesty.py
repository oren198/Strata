"""v1.20 item 4, Arm A: a "Hearsay:" claim on a contribution that records no telling act is
dropped from the reasoning (reason text only; the decision is never touched)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from strata.scope_manager import _NO_TELLING_NOTE, ScopeManager, _drop_false_hearsay
from tests.test_scope_manager import (
    BATCH,
    CURRENT_SUMMARY,
    NEW_CONTRIBUTION,
    SCOPE,
    SECOND_CONTRIBUTION,
    STRATUM,
    THIRD_CONTRIBUTION,
    _batch_input,
    _fake_response,
)
from tests.test_v18_reason_honesty import _accept, _judge_plain
from tests.test_v120_telling import WITHOUT_ONE  # noqa: E402

# -- the reasoning rewrite -------------------------------------------------------------------

FALSE_HEARSAY = [
    (
        "Hearsay: the hatch-keeper reported replacing the brooder thermometer after the noon "
        "round. Context only: an informant's word is never a directive.",
        "A keeper replaced the brooder thermometer after the noon round.",
    ),
    (
        "Hearsay: informant (pit-hand) told the agent about barrowing fresh dry sand into the "
        "pit after inspection. This extends the context around sand handling.",
        "Fresh dry sand was barrowed into the pit after the inspection.",
    ),
    (
        "Hearsay: the contributor, a skiff-hand at s_buoy_skiff, reports their own observation "
        "of the event. This adds a conflicting account to the context.",
        "The harbor bell rang after the buoy lamp was already lit.",
    ),
    (
        "Hearsay: skiff-hand (informant) told the agent about the missing coil. Condensed "
        "context to meet budget.",
        "A coil of small stuff went missing from the chandlery hook.",
    ),
]


@pytest.mark.parametrize(("reasoning", "content"), FALSE_HEARSAY)
def test_a_hearsay_claim_on_a_note_with_no_telling_act_is_replaced_by_the_engine_clause(
    reasoning: str, content: str
) -> None:
    out = _drop_false_hearsay(reasoning, content)
    assert out.endswith(_NO_TELLING_NOTE)
    assert "earsay" not in out and "informant" not in out
    assert out != reasoning


def test_what_the_judge_said_besides_the_hearsay_claim_is_kept() -> None:
    out = _drop_false_hearsay(FALSE_HEARSAY[1][0], FALSE_HEARSAY[1][1])
    assert out.startswith("This extends the context around sand handling.")


def test_genuine_hearsay_keeps_its_label_untouched() -> None:
    reasoning = (
        "Hearsay: informant 'the evening note' told the agent the west brooder thermometer "
        "reads warm. Context only: an informant's word is never a directive."
    )
    content = "The evening note says the west brooder thermometer reads warm."
    assert _drop_false_hearsay(reasoning, content) == reasoning


def test_without_a_content_the_old_behaviour_is_exactly_kept() -> None:
    reasoning = FALSE_HEARSAY[0][0]
    assert _drop_false_hearsay(reasoning) == reasoning
    assert _drop_false_hearsay(reasoning, None) == reasoning


def test_a_reasoning_with_no_hearsay_claim_is_unchanged() -> None:
    reasoning = "First-hand observation by the contributor; no decline grounds apply."
    assert _drop_false_hearsay(reasoning, WITHOUT_ONE[0]) == reasoning


# -- wiring: reason text only, never the decision ------------------------------------------


def test_judge_rewrites_the_reason_and_leaves_the_decision_and_the_context_alone() -> None:
    content = "A keeper replaced the brooder thermometer after the noon round."
    reasoning, context = FALSE_HEARSAY[0][0], "ctx after the round"
    client = MagicMock()
    client.messages.create.side_effect = [_accept(reasoning, context)]
    judgment = _judge_plain(client, content)
    assert judgment.decision == "accept_as_context" and judgment.new_context == context
    assert judgment.directive_ops == []
    assert judgment.reasoning.endswith(_NO_TELLING_NOTE) and "earsay" not in judgment.reasoning
    assert client.messages.create.call_count == 1  # no extra call


def test_judge_leaves_genuine_hearsay_alone() -> None:
    content = "Priya from security told me the rotation moved to Fridays."
    reasoning = "Hearsay: informant Priya told the agent the rotation moved to Fridays."
    client = MagicMock()
    client.messages.create.side_effect = [_accept(reasoning)]
    assert _judge_plain(client, content).reasoning == reasoning


def test_judge_batch_rewrites_per_member_against_its_own_text() -> None:
    first = NEW_CONTRIBUTION
    contributions = [
        first.__class__(**{**first.__dict__, "content": "A keeper replaced the thermometer."}),
        SECOND_CONTRIBUTION.__class__(
            **{
                **SECOND_CONTRIBUTION.__dict__,
                "content": "Priya from security told me the rotation moved.",
            }
        ),
        THIRD_CONTRIBUTION,
    ]
    payload = _batch_input(
        verdicts=[
            {
                "contribution_id": contributions[0].id,
                "decision": "accept_as_context",
                "reasoning": "Hearsay: informant (keeper) told the agent. Fine otherwise.",
            },
            {
                "contribution_id": contributions[1].id,
                "decision": "accept_as_context",
                "reasoning": "Hearsay: informant Priya told the agent.",
            },
            {
                "contribution_id": contributions[2].id,
                "decision": "decline",
                "reasoning": "not useful",
            },
        ],
        directive_ops=[],
    )
    client = MagicMock()
    client.messages.create.side_effect = [_fake_response(payload)]
    batch = ScopeManager(client=client).judge_batch(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contributions=contributions,
    )
    by_id = {v.contribution_id: v for v in batch.verdicts}
    assert by_id[contributions[0].id].reasoning.endswith(_NO_TELLING_NOTE)
    assert by_id[contributions[1].id].reasoning == "Hearsay: informant Priya told the agent."
    assert [v.decision for v in batch.verdicts] == [
        "accept_as_context",
        "accept_as_context",
        "decline",
    ]
    assert BATCH  # the shared fixture list is importable


def test_a_decline_that_names_the_missing_informant_is_never_rewritten() -> None:
    declined = (
        "Manufactured attribution: no one spoke - no informant, not even one identified only "
        "by role, told the agent this."
    )
    client = MagicMock()
    client.messages.create.side_effect = [
        _fake_response(
            {"decision": "decline", "reasoning": declined, "directive_ops": [], "new_context": None}
        )
    ]
    judgment = _judge_plain(client, "Billing's incident record shows the outage was caused by us.")
    assert judgment.decision == "decline" and judgment.reasoning.startswith(declined)


def test_a_negated_mention_is_not_a_hearsay_claim() -> None:
    reasoning = "First-hand report. No informant is involved; nothing here is hearsay."
    assert _drop_false_hearsay(reasoning, WITHOUT_ONE[0]) == reasoning
