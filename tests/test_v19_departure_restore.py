"""v1.19 FIX 6: a stored departure label survives every context write.

After ANY context write (ordinary, batch, refresh, overflow re-ask) a labelled "Departure from
<id> (<scope>): ..." line of the previous context that the new context no longer holds byte for
byte is re-inserted verbatim by the engine. No re-ask. departure_label_lost counts the losses
detected; departure_label_restored counts the re-insertions; the engine never drops a labelled
line, so an overflow it causes is recorded.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

from strata.scope_manager import ScopeManager, ScopeSummary
from tests.test_scope_manager import (
    NEW_CONTRIBUTION,
    SCOPE,
    SECOND_CONTRIBUTION,
    STRATUM,
    _batch_input,
    _fake_response,
)

NOTE = "On 2026-10-12 one PR was merged after one review because the second engineer was out."
LABELLED = f"Departure from c_review01 (this scope): {NOTE}"
OLD = "Older line about the release train."
NEWEST = "The newest substantive note."


def _summary(context: str) -> ScopeSummary:
    return ScopeSummary(
        scope_id=SCOPE.id, directives=[], context=context, updated_at="2026-04-01T09:00:00Z"
    )


def _accept(context: str) -> dict:
    return {
        "decision": "accept_as_context",
        "reasoning": "A note.",
        "directive_ops": [],
        "new_context": context,
    }


def _judge(*answers: dict, context: str, max_words: int = 500, mode="ordinary"):
    client = MagicMock()
    client.messages.create.side_effect = [_fake_response(a) for a in answers]
    judgment = ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=_summary(context),
        recent_contributions=[],
        new_contribution=replace(NEW_CONTRIBUTION, proposed_classification="context"),
        summary_max_words=max_words,
        mode=mode,
    )
    return judgment, client


PREVIOUS = f"{OLD}\n{LABELLED}\n{NEWEST}"


def test_a_rewrite_that_keeps_the_labelled_line_is_untouched():
    judgment, _ = _judge(_accept(f"{OLD} condensed.\n{LABELLED}\n{NEWEST}"), context=PREVIOUS)
    assert judgment.departure_label_lost == 0
    assert judgment.departure_label_restored == 0
    assert judgment.new_context == f"{OLD} condensed.\n{LABELLED}\n{NEWEST}"


def test_a_rewrite_that_drops_the_line_gets_it_back_where_it_was():
    judgment, _ = _judge(_accept(f"{OLD}\n{NEWEST} Plus more."), context=PREVIOUS)
    assert judgment.departure_label_lost == 1
    assert judgment.departure_label_restored == 1
    assert judgment.new_context.split("\n") == [OLD, LABELLED, f"{NEWEST} Plus more."]
    assert judgment.new_summary.context == judgment.new_context
    assert "departure_label_lost: 1" in judgment.protocol_notes
    assert "departure_label_restored: 1" in judgment.protocol_notes


def test_a_rewrite_that_makes_the_line_unlabelled_is_replaced_in_place_without_a_duplicate():
    judgment, _ = _judge(_accept(f"{OLD}\n{NOTE}\n{NEWEST}"), context=PREVIOUS)
    assert judgment.new_context.split("\n") == [OLD, LABELLED, NEWEST]
    assert judgment.departure_label_restored == 1


def test_when_no_neighbour_survives_the_line_goes_before_the_newest_note():
    judgment, _ = _judge(_accept("One wholly new line.\nThe newest one."), context=PREVIOUS)
    assert judgment.new_context.split("\n") == ["One wholly new line.", LABELLED, "The newest one."]


def test_an_overflow_reask_that_drops_the_line_is_restored_and_the_overflow_is_recorded():
    long_context = f"{OLD}\n{LABELLED}\n" + " ".join(f"word{i}" for i in range(60))
    judgment, client = _judge(
        _accept(long_context),  # over the budget: the judge is asked to condense
        _accept("Condensed: the release train and the newest note."),  # drops the label
        context=PREVIOUS,
        max_words=30,
    )
    assert client.messages.create.call_count == 2
    assert LABELLED in judgment.new_context.split("\n")
    assert judgment.departure_label_restored == 1
    assert any(n.startswith("departure_label_overflow:") for n in judgment.protocol_notes) == (
        len(judgment.new_context.split()) > 30
    )


def test_the_restored_line_is_kept_even_when_it_pushes_the_summary_over_budget():
    judgment, _ = _judge(_accept("Short."), context=PREVIOUS, max_words=5)
    assert LABELLED in judgment.new_context
    assert any(n.startswith("departure_label_overflow: ") for n in judgment.protocol_notes)


def test_a_refresh_context_write_restores_the_line_too():
    judgment, _ = _judge(
        _accept("Reconciled context."), context=PREVIOUS, mode="input_change_refresh"
    )
    assert LABELLED in judgment.new_context.split("\n")
    assert judgment.departure_label_restored == 1


def test_the_batch_path_restores_the_line():
    client = MagicMock()
    client.messages.create.side_effect = [
        _fake_response(
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
                new_context="A batch rewrite without the departure.",
            )
        )
    ]
    judgment = ScopeManager(client=client).judge_batch(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=_summary(PREVIOUS),
        recent_contributions=[],
        new_contributions=[
            replace(NEW_CONTRIBUTION, proposed_classification="context"),
            replace(SECOND_CONTRIBUTION, proposed_classification="context"),
        ],
    )
    assert LABELLED in judgment.new_context.split("\n")
    assert judgment.departure_label_restored == 1
    assert LABELLED in judgment.new_summary.context


def test_an_unlabelled_consequence_line_is_never_restored():
    previous = f"{OLD}\nA report of following the rule: it cost two trawlers fuel.\n{NEWEST}"
    judgment, _ = _judge(_accept(f"{NEWEST}"), context=previous)
    assert judgment.new_context == NEWEST
    assert judgment.departure_label_restored == 0
    assert judgment.departure_label_lost == 0


def test_a_decline_writes_no_context_so_nothing_is_restored():
    declined = {"decision": "decline", "reasoning": "no", "directive_ops": [], "new_context": None}
    judgment, _ = _judge(declined, context=PREVIOUS)
    assert judgment.new_context is None
    assert judgment.departure_label_restored == 0
