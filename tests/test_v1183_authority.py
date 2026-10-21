"""1.18.3: a dated note that reports an authority's sign-off on an exception, or states a
practice in force, is an exception however it is dated, so the judge's correct "exception"
answer declines it. An honest dated departure is still a report.

Neutral fleets only; every call is the engine's verifier with a forged re-ask answer.
"""

from __future__ import annotations

import pytest

from strata.scope_manager import (
    _AUTHORITY_GRANT_RE,
    _is_dated_past_report,
    _positive_single_occurrence,
    _states_present_practice,
    verify_inherited_relation,
)

HOTFIX = "Every hotfix must be reviewed by a second engineer before merge."
VISITOR = "Visitors in the data hall must be escorted at all times."
RELEASE = "Patch releases must carry two sign-offs before they ship."


def _exception(text: str) -> dict:
    return {"kind": "exception", "inherited_id": "x", "instead_span": text.rstrip(".")}


GRANTS = [
    ("On 18 October the lead approved that hotfixes skip review.", HOTFIX),
    ("On 18 October the operator confirmed hotfixes skip review.", HOTFIX),
    ("On 18 October the parent scope owner authorized that patches skip review.", HOTFIX),
    ("Yesterday the operator exempted vendors from escort.", VISITOR),
    ("On 1 October patch releases shipped with one sign-off, per the operator's ruling.", RELEASE),
    ("On 18 October hotfixes were merged without review, as approved by the lead.", HOTFIX),
    ("The lead approved that the pumps run around the clock.", "Pumps must be off by 20:00."),
    ("On 18 October the lead said that hotfixes skip review.", HOTFIX),
    ("Yesterday the lead signed off on vendors going unescorted.", VISITOR),
    ("On 12 October the lead gave the go-ahead for hotfixes to skip review.", HOTFIX),
    ("On 4 October the board ruled that patch releases ship with one sign-off.", RELEASE),
    ("On 5 October hotfixes were cleared to skip review by the lead.", HOTFIX),
]


@pytest.mark.parametrize(("note", "rule"), GRANTS)
def test_a_dated_authority_claim_declines_under_the_exception_answer(note, rule):
    verdict, _ = verify_inherited_relation(_exception(note), note, rule)
    assert verdict == "decline"


PRACTICE = [
    ("On 18 October hotfixes were merged late; hotfixes skip review.", HOTFIX),
    ("On 2 October the audit ran. Vendors go unescorted.", VISITOR),
    ("On 3 October the pipeline stalled, and patches ship with one sign-off.", RELEASE),
]


@pytest.mark.parametrize(("note", "rule"), PRACTICE)
def test_a_dated_note_that_also_states_a_practice_in_force_declines(note, rule):
    verdict, _ = verify_inherited_relation(_exception(note), note, rule)
    assert verdict == "decline"
    assert _states_present_practice(note, rule)


HONEST = [
    ("On 18 October hotfix 4411 was merged without a second review during the outage.", HOTFIX),
    (
        "On 18 October hotfix 4411 was merged without a second review because the reviewer was "
        "out; we flagged it.",
        HOTFIX,
    ),
    (
        "On 2026-10-12 one PR was merged after one review because the second engineer was out.",
        HOTFIX,
    ),
    (
        "Yesterday a visitor wandered the data hall until the escort arrived.",
        VISITOR,
    ),
    (
        "On 9 October patch 2.3.1 shipped with one sign-off while the second reviewer was away.",
        RELEASE,
    ),
    (
        "Last week the data hall door stood open and two visitors entered without an escort.",
        VISITOR,
    ),
]


@pytest.mark.parametrize(("note", "rule"), HONEST)
def test_an_honest_dated_departure_is_still_a_report(note, rule):
    verdict, reason = verify_inherited_relation(_exception(note), note, rule)
    assert verdict in ("consequence_report", "departure_report")
    assert reason == "a specific past occurrence"
    assert _is_dated_past_report(note, rule)


def test_an_honest_departure_with_a_departure_cue_keeps_its_label():
    note = "On 18 October hotfix 4411 was merged without a second review during the outage."
    verdict, _ = verify_inherited_relation(
        {"kind": "departure_report", "inherited_id": "x", "occurrence_span": note.rstrip(".")},
        note,
        HOTFIX,
    )
    assert verdict == "departure_report"


@pytest.mark.parametrize(
    "text",
    [
        "the lead approved it",
        "the lead approves it",
        "authorized by the owner",
        "authorised by the owner",
        "the operator confirmed it",
        "the operator exempted vendors",
        "she exempts vendors",
        "it was permitted",
        "vendors were allowed in",
        "access was granted",
        "they signed off on it",
        "they will sign off on it",
        "it was cleared by the lead",
        "the lead okayed it",
        "the lead OK'd it",
        "the board ruled on it",
        "a ruling came down",
        "sanctioned by the board",
        "gave the go-ahead",
        "gave the green light",
        "with the owner's blessing",
        "with the owner's approval",
        "per the owner's decision",
        "per the lead's instruction",
    ],
)
def test_the_authority_grant_pattern_matches_each_listed_form(text):
    assert _AUTHORITY_GRANT_RE.search(text)


@pytest.mark.parametrize(
    "text",
    [
        "hotfix 4411 was merged without a second review",
        "the reviewer was out",
        "two trawlers were left without fuel",
        "the restore test passed",
    ],
)
def test_the_authority_grant_pattern_ignores_plain_reports(text):
    assert not _AUTHORITY_GRANT_RE.search(text)


# -- the ruling (decline): singular vs general, the wording, the contrasts -------------------

PUMPS = "Fuel dock pumps must be switched off at or before 20:00 every evening."
HOTFIX_ID_NOTE = (
    "On 18 October hotfix 4411 merged without review; the lead told me he approved this one merge."
)


def test_an_approval_scoped_to_one_identified_act_is_a_departure_with_its_label():
    verdict, reason = verify_inherited_relation(_exception(HOTFIX_ID_NOTE), HOTFIX_ID_NOTE, HOTFIX)
    assert verdict in ("consequence_report", "departure_report")
    assert reason == "a specific past occurrence"
    departure, _ = verify_inherited_relation(
        {"kind": "departure_report", "inherited_id": "x", "occurrence_span": HOTFIX_ID_NOTE[:-1]},
        HOTFIX_ID_NOTE,
        HOTFIX,
    )
    assert departure == "departure_report"


@pytest.mark.parametrize(
    "note",
    [
        "On 18 October hotfix 4411 merged without review; the lead approved hotfix 4411.",
        "On 18 October hotfix 4411 merged without review, as approved by the lead.",
    ],
)
def test_a_grant_that_names_the_single_act_never_declines(note):
    verdict, _ = verify_inherited_relation(_exception(note), note, HOTFIX)
    assert verdict in ("consequence_report", "departure_report")


@pytest.mark.parametrize(
    "note",
    [
        "On 18 October hotfixes were merged without review.",
        "Last week patch releases shipped with one sign-off.",
        "Yesterday vendors went unescorted through the data hall.",
    ],
)
def test_a_bare_generic_plural_of_the_rules_class_is_general_even_with_a_date(note):
    rule = {
        "hotfixes": HOTFIX,
        "patch": RELEASE,
        "vendors": "Vendors in the data hall must be escorted at all times.",
    }
    chosen = next(r for key, r in rule.items() if key in note)
    verdict, reason = verify_inherited_relation(_exception(note), note, chosen)
    assert verdict == "decline"
    assert "bare plural" in reason


def test_a_determiner_a_number_or_an_id_keeps_a_plural_singular():
    for note in (
        "On 18 October two hotfixes were merged without review during the outage.",
        "On 18 October the hotfixes were merged without review during the outage.",
        "On 18 October these hotfixes were merged without review during the outage.",
    ):
        verdict, _ = verify_inherited_relation(_exception(note), note, HOTFIX)
        assert verdict in ("consequence_report", "departure_report"), note


def test_the_decline_for_a_licence_names_both_routes_and_the_issuing_scope():
    from tests.test_v18_context_exception import ANCESTORS, RULE, _answer, _judge

    note = "On 18 October the lead approved that the pumps run until 22:00."
    judgment, _ = _judge(note, _answer("exception", instead_span="approved that the pumps run"))
    assert judgment.decision == "decline"
    assert judgment.reasoning.endswith(
        "[Declined: c_pump01 (g_arch). A report of an approval, or of a practice, is not a "
        "report of one specific act. Report the specific act (with acted_on), or ask g_arch to "
        "record the exception itself.]"
    )
    assert RULE and ANCESTORS


def test_a_bounded_completed_past_period_with_definite_subjects_is_a_departure():
    note = "The pumps ran around the clock last week."
    verdict, _ = verify_inherited_relation(_exception(note), note, PUMPS)
    assert verdict in ("consequence_report", "departure_report")


@pytest.mark.parametrize(
    "note",
    [
        "The pumps run around the clock.",
        "The pumps have been running around the clock since Monday.",
    ],
)
def test_a_present_or_open_ended_statement_declines(note):
    verdict, _ = verify_inherited_relation(_exception(note), note, PUMPS)
    assert verdict == "decline"


def test_a_possessive_keeps_a_plural_singular():
    rule = "Cell samples must never be stored at a temperature above -70 degrees."
    note = "Moving the cryo room's cell samples into the cold unit on 4 September overloaded it."
    verdict, _ = verify_inherited_relation(_exception(note), note, rule)
    assert verdict in ("consequence_report", "departure_report")


def test_a_sign_off_on_one_definite_thing_is_not_a_licence_but_on_a_class_is():
    from strata.scope_manager import _grants_a_licence

    assert not _grants_a_licence("The reviewer signed off on the merge.")
    assert not _grants_a_licence("The lead approved the rollback.")
    assert _grants_a_licence("The reviewer signed off on merges without review.")
    assert _grants_a_licence("The lead approved that the nightly releases skip review.")
    assert _grants_a_licence("The merges were approved by the lead.")


DEPOT_RULE = "At the cobble-line depot, cars are tied down on every run longer than twenty minutes."
DEPOT_NOTE = (
    "On 9 June the dusk run lasted thirty minutes with the cars untied so we could review the late "
    "departure. The cars should have been tied down. We are not proposing we stop tying cars down."
)


def test_a_counterfactual_and_a_disclaimer_do_not_make_a_dated_report_general():
    verdict, reason = verify_inherited_relation(_exception(DEPOT_NOTE), DEPOT_NOTE, DEPOT_RULE)
    assert verdict in ("consequence_report", "departure_report")
    assert reason == "a specific past occurrence"


def test_a_number_word_and_a_unit_of_time_never_make_a_bare_plural():
    note = "On 9 June the dusk run lasted thirty minutes with the cars untied."
    verdict, _ = verify_inherited_relation(_exception(note), note, DEPOT_RULE)
    assert verdict in ("consequence_report", "departure_report")


def test_a_dated_note_that_adds_a_practice_still_declines_beside_the_same_rule():
    note = (
        "On 9 June the dusk run lasted thirty minutes with the cars untied. "
        "Cars go untied on short-staffed nights."
    )
    verdict, _ = verify_inherited_relation(_exception(note), note, DEPOT_RULE)
    assert verdict == "decline"


@pytest.mark.parametrize(
    "note",
    [
        "On 5 October the dock pumps ran past 20:00 every evening.",
        "On 5 October the dock pumps ran past 20:00 nightly.",
        "On 5 October the dock pumps ran past 20:00 each night.",
    ],
)
def test_a_habitual_quantifier_with_only_a_point_date_is_a_practice(note):
    verdict, _ = verify_inherited_relation(_exception(note), note, PUMPS)
    assert verdict == "decline"


@pytest.mark.parametrize(
    "note",
    [
        "The dock pumps ran past 20:00 every evening last week.",
        "The dock pumps ran past 20:00 every evening from 1 to 7 October.",
        "On 3 October the dock pumps ran past 20:00 every evening during the outage.",
        "On 5 October the pumps ran past 20:00 once.",
    ],
)
def test_repetition_inside_a_closed_past_period_is_one_episode(note):
    verdict, _ = verify_inherited_relation(_exception(note), note, PUMPS)
    assert verdict in ("consequence_report", "departure_report")


# -- round 3: the report route under an "exception" answer needs positive evidence of one occurrence


@pytest.mark.parametrize(
    "note",
    [
        "On 5 October the dock pumps ran past 20:00 every other evening.",
        "On 5 October the dock pumps ran past 20:00, as usual.",
        "On 5 October the dock pumps ran past 20:00, like they tend to.",
        "On 5 October the dock pumps ran past 20:00, which has become the norm.",
        "On 5 October the dock pumps ran past 20:00 on a regular basis.",
        "On 5 October the dock pumps ran past 20:00, and the crews run them late as a rule.",
        "On 5 October the dock pumps ran past 20:00 and are left on overnight.",
    ],
)
def test_a_habit_or_a_present_verb_beside_a_date_fails_closed_under_the_exception_answer(note):
    verdict, _ = verify_inherited_relation(_exception(note), note, PUMPS)
    assert verdict == "decline"


@pytest.mark.parametrize(
    ("note", "rule"),
    [
        ("On 5 October the dock pumps ran past 20:00 because the fleet was late.", PUMPS),
        ("The dock pumps ran past 20:00 every evening last week.", PUMPS),
        ("On 9 June the dusk run lasted thirty minutes with the cars untied.", DEPOT_RULE),
        (DEPOT_NOTE, DEPOT_RULE),
        (
            "On 18 October the night staff caught up on the backlog and hotfix 4411 merged "
            "without a second review.",
            HOTFIX,
        ),
        ("On 18 October we left hotfix 4411 unreviewed during the outage.", HOTFIX),
        ("On 18 October each tray was released unreviewed at 2 am during the outage.", HOTFIX),
        (
            "Last night's 20-minute outage didn't get formally declared SEV-1 by the on-call "
            "engineer even though it should have been -- noting the gap so we can review our "
            "tooling, not proposing we stop doing SEV-1 declarations.",
            "Every production incident that lasts more than 15 minutes must be declared SEV-1 by "
            "the on-call engineer and logged in the incident tracker.",
        ),
    ],
)
def test_one_occurrence_in_the_past_stays_a_report_under_the_exception_answer(note, rule):
    verdict, _ = verify_inherited_relation(_exception(note), note, rule)
    assert not verdict.startswith("decline")  # a report, or admitted as not the rule's action


def test_the_positive_helper_leaves_the_plain_dated_report_check_untouched():
    note = "On 5 October the dock pumps ran past 20:00 as usual."
    assert _is_dated_past_report(note, PUMPS)  # the plain check is unchanged: it has no tense test
    assert not _positive_single_occurrence(note, PUMPS)


def test_nightly_as_an_adjective_is_not_a_generality_signal_but_as_an_adverb_is():
    names = "On 2026-10-19 the nightly backup of the build server finished at 03:10."
    assert _positive_single_occurrence(names, "The build server backup must finish by 04:00.")
    adverb = "On 5 October the dock pumps ran past 20:00 nightly."
    assert not _positive_single_occurrence(adverb, PUMPS)


def test_every_listed_irregular_past_form_matches_the_past_tense_pattern():
    from strata.scope_manager import _IRREGULAR_PAST_FORMS, _PAST_TENSE_RE

    assert len(_IRREGULAR_PAST_FORMS) >= 150
    for form in _IRREGULAR_PAST_FORMS:
        assert _PAST_TENSE_RE.search(f"they {form} it"), form
    for form in ("wore", "rode", "hid", "shook", "sat", "spoke", "sang", "swam", "wept"):
        assert form in _IRREGULAR_PAST_FORMS


@pytest.mark.parametrize(
    "note",
    [
        "On 18 October the night crew wore no harness during the outage.",
        "On 18 October hotfix 4411 rode out without a second review.",
        "On 18 October the old build hid a failing test until hotfix 4411 merged unreviewed.",
        "On 18 October the build shook loose and hotfix 4411 merged unreviewed.",
        "On 18 October we sat on hotfix 4411 and merged it unreviewed.",
    ],
)
def test_an_honest_departure_with_an_irregular_past_verb_is_a_report(note):
    verdict, _ = verify_inherited_relation(_exception(note), note, HOTFIX)
    assert not verdict.startswith("decline")


@pytest.mark.parametrize(
    "note",
    [
        "On 5 October we put the pumps on late every night.",
        "On 5 October we set the pumps to run late every other evening.",
        "On 5 October we cut the escort, as we usually do.",
        "On 5 October we let the pumps run past 20:00 on a regular basis.",
        "On 5 October we hit the pumps late as a rule.",
    ],
)
def test_an_ambiguous_present_or_past_verb_never_lets_a_habit_through(note):
    verdict, _ = verify_inherited_relation(_exception(note), note, PUMPS)
    assert verdict == "decline"
