"""1.18.3 F4: characterise the checks a report passes through. No behaviour change.

A case that admits wording the rule does not support is pinned as it behaves
today and marked ``# F4-SUSPECT``. Nothing here is fixed.
"""

from __future__ import annotations

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


def test_a_clock_time_digit_makes_an_unrelated_span_the_rules_action() -> None:
    # F4-SUSPECT: weighing crate 20 shares the digit in "20:00", so an exception
    # answer about that crate is admitted as a report of following the pump rule.
    span = "crate 20 was weighed"
    text = "On 5 October crate 20 was weighed on the quay."
    assert _action_overlap(span, PUMP) is True
    verdict, reason = verify_inherited_relation(
        {"kind": "exception", "instead_span": span}, text, PUMP
    )
    assert verdict == "consequence_report"
    assert reason == "a specific past occurrence"


# -- _context_covers --------------------------------------------------------


def test_the_pumps_own_words_cover_the_pump_rule() -> None:
    assert _context_covers(PUMP, "The dock pumps were switched off at 20:00.") is True


def test_an_unrelated_sentence_does_not_cover_the_pump_rule() -> None:
    assert _context_covers(PUMP, "The quay stayed lit.") is False


def test_the_restore_drill_covers_the_charter() -> None:
    note = "The restore drill for the release candidate was run twice by the reviewer."
    assert _context_covers(CHARTER, note) is True


def test_a_waiver_of_the_rules_clock_time_is_not_covered() -> None:
    # F4-SUSPECT: "The 20:00 shutdown is waived." is this rule, and an unrelated
    # answer is admitted because the note shares no subject stem with it.
    note = "The 20:00 shutdown is waived."
    assert _context_covers(PUMP, note) is False
    verdict, reason = verify_inherited_relation({"kind": "unrelated"}, note, PUMP)
    assert verdict == "admit"
    assert reason == "answered unrelated; no covered exception"


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


def test_every_evening_is_admitted_as_one_occurrence() -> None:
    # F4-SUSPECT: "every evening" is a practice, and the report branch admits it.
    # "every evening" is not one of the general markers ("every time", "each evening").
    text = "On 5 October the dock pumps were switched off every evening."
    verdict, reason = verify_inherited_relation(
        _report("the dock pumps were switched off"), text, PUMP
    )
    assert verdict == "consequence_report"
    assert reason == "a specific past occurrence"


def test_since_meaning_because_declines_the_report() -> None:
    text = "On 5 October the dock pumps were switched off since the quay was dark."
    verdict, reason = verify_inherited_relation(
        _report("the dock pumps were switched off"), text, PUMP
    )
    assert verdict == "decline"
    assert "since" in reason
