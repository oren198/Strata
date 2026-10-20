"""1.18.2: a context note is declined as an exception only when it is ABOUT the rule.

The fleet is neutral: a long operations charter (several sentences, one subject each) and an
incident rule. Every test calls the engine's verifier with a forged re-ask answer, so no judge
is involved: the engine decides what a verbatim span is worth.
"""

from __future__ import annotations

import pytest

from strata.scope_manager import (
    _blank_non_marker_clauses,
    _exception_marker,
    _general_marker,
    _note_covers_rule,
    verify_inherited_relation,
)

CHARTER = (
    "Every release candidate must pass the restore drill before it is published. "
    "A gate report must name the judge model and the dataset it ran on. "
    "A contributor must not decline a report without stating the reason. "
    "The reviewer signs off on each merge into the release branch."
)
INCIDENT = (
    "Every production incident that lasts more than 15 minutes must be declared SEV-1 "
    "by the on-call engineer and logged in the incident tracker."
)

BACKUP_NOTE = (
    "On 2026-10-19 the nightly backup of the build server finished at 03:10 "
    "and the restore test passed."
)
GATE_NOTE = (
    "On 2026-10-18 the gate report for the nightly run named the judge model and the dataset "
    "it ran on, and the reviewer signed off on the merge."
)
SEV_NOTE = (
    "Last night's 20-minute outage didn't get formally declared SEV-1 by the on-call engineer "
    "even though it should have been -- noting the gap so we can review our tooling, "
    "not proposing we stop doing SEV-1 declarations."
)
SEV_SPAN = "didn't get formally declared SEV-1 by the on-call engineer"
EXEMPT_NOTE = (
    "Changes the owner marks as low risk are exempt from the incident declaration requirement, "
    "so we can move faster on small outages."
)
SKIP_NOTE = "not proposing we stop, we just skip it on nights"


def _exception(span: str) -> dict:
    return {"kind": "exception", "instead_span": span}


def test_d1_exception_answer_on_an_uncovered_note_is_admitted():
    verdict, reason = verify_inherited_relation(
        _exception("the restore test passed"), BACKUP_NOTE, CHARTER
    )
    assert verdict == "admit"  # callers count an "admit" as the fallback
    assert reason == "exception answer on a rule the note does not cover"


def test_d1_gate_report_shape_is_a_report_not_a_decline():
    verdict, reason = verify_inherited_relation(
        _exception("named the judge model and the dataset it ran on"), GATE_NOTE, CHARTER
    )
    assert verdict in ("departure_report", "consequence_report")
    assert reason == "a specific past occurrence"


def test_d1_gate_report_without_action_overlap_is_admitted():
    verdict, _ = verify_inherited_relation(
        _exception("the dataset"), GATE_NOTE.replace("judge model", "referee"), INCIDENT
    )
    assert verdict == "admit"


def test_d1_a_real_exception_on_the_rule_it_covers_is_still_declined():
    note = (
        "Release candidates built from a hotfix branch skip the restore drill "
        "before they are published."
    )
    verdict, _ = verify_inherited_relation(_exception("skip the restore drill"), note, CHARTER)
    assert verdict == "decline"


def test_d2_filler_words_do_not_cover_a_rule():
    note = "This must come from the full run with the same settings."
    rule = "A contributor must not decline a report from this scope with the full text."
    assert not _note_covers_rule(rule, note)


def test_d2_two_shared_subject_stems_cover_a_rule():
    note = "The restore drill for the release candidate was run twice by the reviewer."
    assert _note_covers_rule(CHARTER, note)


@pytest.mark.parametrize(
    "kind", ["departure_report", "consequence_report", "unrelated", "unreadable", "exception"]
)
def test_d3_departure_report_text_is_never_declined(kind):
    answer = {"kind": kind, "occurrence_span": SEV_SPAN, "instead_span": SEV_SPAN}
    verdict, _ = verify_inherited_relation(answer, SEV_NOTE, INCIDENT)
    assert verdict not in ("decline", "decline_unspecific")


def test_d3_departure_report_answer_is_admitted_as_a_departure():
    answer = {"kind": "departure_report", "occurrence_span": SEV_SPAN}
    verdict, reason = verify_inherited_relation(answer, SEV_NOTE, INCIDENT)
    assert verdict == "departure_report"
    assert reason == "a specific past occurrence"


def test_d3_purpose_and_counterfactual_clauses_are_not_markers():
    assert _exception_marker(SEV_NOTE, INCIDENT) is None
    assert _general_marker(SEV_NOTE) is None


def test_d3_an_exempt_note_with_a_purpose_clause_still_declines():
    answer = {"kind": "unreadable"}
    verdict, _ = verify_inherited_relation(answer, EXEMPT_NOTE, INCIDENT)
    assert verdict == "decline"


def test_d3_a_real_marker_beside_a_negated_proposal_still_fires():
    assert _exception_marker(SKIP_NOTE, INCIDENT) is not None
    verdict, _ = verify_inherited_relation({"kind": "unreadable"}, SKIP_NOTE, INCIDENT)
    assert verdict == "decline"


def test_d3_a_hyphen_inside_a_word_is_not_a_clause_end():
    text = "declined to declare SEV-1 so we can review SEV-1 tooling, then moved on"
    blanked = _blank_non_marker_clauses(text)
    assert blanked.count("SEV-1") == 1  # the clause before "so" is kept whole
    assert "tooling" not in blanked
    assert "then moved on" in blanked


PUMP_RULE = "Fuel dock pumps must be switched off at or before 20:00 every evening."
PUMP_EXCEPTION = "FYI, on nights when the fleet returns the pumps may stay running until 22:00."
PUMP_SPAN = "the pumps may stay running until 22:00"


@pytest.mark.parametrize("kind", ["exception", "unrelated", "unreadable"])
def test_one_shared_stem_plus_an_exception_marker_covers_the_rule(kind):
    answer = {"kind": kind, "instead_span": PUMP_SPAN}
    verdict, _ = verify_inherited_relation(answer, PUMP_EXCEPTION, PUMP_RULE)
    assert verdict == "decline"


def test_one_shared_stem_without_a_marker_stays_uncovered():
    note = "The harbour crane was greased on 2026-10-12 using oil from the pumps store."
    verdict, reason = verify_inherited_relation(
        _exception("using oil from the pumps store"), note, PUMP_RULE
    )
    assert verdict == "admit"
    assert reason == "exception answer on a rule the note does not cover"


def test_zero_shared_stems_with_a_marker_stay_uncovered():
    note = "Staff may stay late on the night of the audit."
    verdict, _ = verify_inherited_relation(_exception("may stay late"), note, PUMP_RULE)
    assert verdict == "admit"
