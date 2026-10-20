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


def test_d1_dated_past_note_on_an_unrelated_rule_is_admitted():
    verdict, reason = verify_inherited_relation(
        _exception("the restore test passed"), BACKUP_NOTE, INCIDENT
    )
    assert verdict == "admit"  # callers count an "admit" as the fallback
    assert reason == "exception answer on a past occurrence that is not the rule's action"


def test_d1_dated_past_note_that_touches_the_rule_is_a_report():
    verdict, reason = verify_inherited_relation(
        _exception("the restore test passed"), BACKUP_NOTE, CHARTER
    )
    assert verdict == "consequence_report"
    assert reason == "a specific past occurrence"


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


PHARMACY_RULE = "Medication refills must be approved by two pharmacists before release."
SEEDLING_RULE = "Seedlings must be watered at or before 07:00 every day."


@pytest.mark.parametrize(
    ("note", "span", "rule"),
    [
        (
            "A top-up can be released once a single pharmacist has reviewed it.",
            "can be released once a single pharmacist has reviewed it",
            PHARMACY_RULE,
        ),
        (
            "Seedlings must be watered at or before 09:00 every day.",
            "watered at or before 09:00 every day",
            SEEDLING_RULE,
        ),
        (
            "Locals get their parcels straight to the door.",
            "straight to the door",
            "Every parcel is held at the depot until the courier signs the manifest.",
        ),
    ],
)
def test_a_present_tense_exception_is_declined_whatever_it_shares_with_the_rule(note, span, rule):
    verdict, _ = verify_inherited_relation(_exception(span), note, rule)
    assert verdict == "decline"


def test_a_dated_past_note_containing_must_stay_is_a_report():
    note = "On 2026-10-12 the must-stay check ran and the restore drill passed for the candidate."
    verdict, _ = verify_inherited_relation(_exception("the restore drill passed"), note, CHARTER)
    assert verdict == "consequence_report"


@pytest.mark.parametrize(
    "note",
    [
        "Once the restore drill passed.",
        "The restore drill passed at 03:10 on build 2.1.4 in 2026.",
        "The restore drill passed twice.",
    ],
)
def test_once_a_clock_time_a_version_or_a_year_is_not_a_date(note):
    verdict, _ = verify_inherited_relation(_exception("restore drill passed"), note, CHARTER)
    assert verdict == "decline"


#: Dated notes that announce a decision or a change: exceptions, never reports.
DATED_ATTACK = [
    (
        "Every hotfix must be reviewed by a second engineer before merge.",
        "On 18 October we agreed hotfixes skip review.",
    ),
    (
        "Every hotfix must be reviewed by a second engineer before merge.",
        "On 2026-10-18 the team decided hotfixes no longer need a second reviewer.",
    ),
    (
        "Fuel dock pumps must be switched off at or before 20:00 every evening.",
        "Since Monday the pumps run until 22:00.",
    ),
    (
        "Fuel dock pumps must be switched off at or before 20:00 every evening.",
        "On Monday we moved pump shutdown to 22:00.",
    ),
    (
        "Visitors in the data hall must be escorted at all times.",
        "Yesterday the team decided the escort rule no longer applies to vendors.",
    ),
    (
        "Visitors in the data hall must be escorted at all times.",
        "Yesterday we agreed vendors walk the hall alone from now on.",
    ),
    (
        "Samples are logged in the freezer register when they are placed.",
        "Last week we switched to logging trays at the end of the shift.",
    ),
    (
        "Samples are logged in the freezer register when they are placed.",
        "On 3 October the lab started logging trays once per shift.",
    ),
    (
        "Archive volumes may be borrowed for a maximum of 14 days.",
        "On 12 September we extended guest loans to two months.",
    ),
    (
        "Students on a field trip remain with their assigned chaperone group.",
        "Last month we stopped assigning chaperone groups for the oldest pupils.",
    ),
    (
        "Controlled drugs must be counted by two staff at every handover.",
        "On Friday the ward agreed one signature is enough for night handover.",
    ),
    (
        "Every release needs at least 2 sign-offs.",
        "On 1 October we dropped the second sign-off for patch releases.",
    ),
]


@pytest.mark.parametrize(("rule", "note"), DATED_ATTACK)
def test_a_dated_change_of_practice_is_declined_under_an_exception_answer(rule, note):
    verdict, _ = verify_inherited_relation(_exception(note), note, rule)
    assert verdict == "decline"
