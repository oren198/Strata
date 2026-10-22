"""1.18.3 F4: characterise the checks a report passes through. No behaviour change.

Three cases that admit wording the rule does not support are strict xfails of
the correct expectation, and the reason keeps the F4-SUSPECT marker. The other
cases pin behaviour that holds.
"""

from __future__ import annotations

import pytest

from strata.scope_manager import _action_overlap, _context_covers, verify_inherited_relation

PUMP = "Fuel dock pumps must be switched off at or before 20:00 every evening."
CHARTER = (
    "Every release candidate must pass the restore drill before it is published. "
    "A gate report must name the judge model and the dataset it ran on."
)


def _report(span: str) -> dict:
    return {"kind": "consequence_report", "occurrence_span": span}


# -- _action_overlap --------------------------------------------------------


def test_a_shared_subject_stem_overlaps_the_rule() -> None:
    assert _action_overlap("the pumps stayed on", PUMP) is True


def test_a_shared_number_overlaps_the_rule() -> None:
    assert _action_overlap("finished at 20:00", PUMP) is True


def test_words_outside_the_rule_do_not_overlap() -> None:
    assert _action_overlap("the quay stayed lit", PUMP) is False


@pytest.mark.xfail(
    strict=True,
    reason=(
        "F4-SUSPECT: weighing crate 20 still shares the digit in '20:00', so an "
        "exception answer is admitted as a report of following the pump rule"
    ),
)
def test_weighing_crate_20_is_not_admitted_as_a_report_of_the_pump_rule() -> None:
    """Crate 20 is not the pump rule's action, so the note is not a report of it."""
    span = "crate 20 was weighed"
    text = "On 5 October crate 20 was weighed on the quay."
    assert _action_overlap(span, PUMP) is False
    verdict, reason = verify_inherited_relation(
        {"kind": "exception", "instead_span": span}, text, PUMP
    )
    assert verdict == "admit"
    assert reason == "exception answer on a past occurrence that is not the rule's action"


# -- _context_covers --------------------------------------------------------


def test_the_pumps_own_words_cover_the_pump_rule() -> None:
    assert _context_covers(PUMP, "The dock pumps were switched off at 20:00.") is True


def test_an_unrelated_sentence_does_not_cover_the_pump_rule() -> None:
    assert _context_covers(PUMP, "The quay stayed lit.") is False


def test_the_restore_drill_covers_the_charter() -> None:
    note = "The restore drill for the release candidate was run twice by the reviewer."
    assert _context_covers(CHARTER, note) is True


@pytest.mark.xfail(
    strict=True,
    reason=(
        "F4-SUSPECT: 'The 20:00 shutdown is waived.' is this rule, and an unrelated "
        "answer is still admitted because the note shares no subject stem with it"
    ),
)
def test_a_waiver_of_the_rules_clock_time_is_declined() -> None:
    """A waiver of the rule's own clock time is this rule, so unrelated does not admit it."""
    note = "The 20:00 shutdown is waived."
    assert _context_covers(PUMP, note) is True
    verdict, reason = verify_inherited_relation({"kind": "unrelated"}, note, PUMP)
    assert verdict == "decline"
    assert "waived" in reason


# -- clause-blanked _general_marker, report branch -------------------------


def test_a_purpose_clause_is_blanked_and_the_report_stands() -> None:
    text = "On 5 October the dock pumps were switched off at 20:00 so we can review our tooling."
    verdict, reason = verify_inherited_relation(
        _report("the dock pumps were switched off at 20:00"), text, PUMP
    )
    assert verdict == "consequence_report"
    assert reason == "a specific past occurrence"


def test_a_negated_proposal_is_blanked_and_the_departure_stands() -> None:
    text = (
        "On 5 October the dock pumps were not switched off, "
        "not proposing we stop the evening shutdown."
    )
    verdict, reason = verify_inherited_relation(
        _report("the dock pumps were not switched off"), text, PUMP
    )
    assert verdict == "departure_report"
    assert reason == "a specific past occurrence"


def test_a_habit_beside_a_blanked_purpose_clause_still_declines() -> None:
    text = (
        "On 5 October the dock pumps were switched off at 20:00 so we can review, "
        "and we always skip the evening check."
    )
    verdict, reason = verify_inherited_relation(
        _report("the dock pumps were switched off at 20:00"), text, PUMP
    )
    assert verdict == "decline"
    assert "always" in reason


@pytest.mark.xfail(
    strict=True,
    reason=(
        "F4-SUSPECT: 'every evening' is a practice, and the report branch still "
        "admits it as one occurrence"
    ),
)
def test_every_evening_in_the_report_branch_is_declined() -> None:
    """'every evening' is a practice, so the report branch declines it."""
    text = "On 5 October the dock pumps were switched off every evening."
    verdict, reason = verify_inherited_relation(
        _report("the dock pumps were switched off"), text, PUMP
    )
    assert verdict == "decline"
    assert "every evening" in reason


def test_since_meaning_because_declines_the_report() -> None:
    text = "On 5 October the dock pumps were switched off since the quay was dark."
    verdict, reason = verify_inherited_relation(
        _report("the dock pumps were switched off"), text, PUMP
    )
    assert verdict == "decline"
    assert "since" in reason
