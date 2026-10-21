"""1.18.3 round 5: a practice-start date ("as of 3 October", "from 3 October on") dates the start
of a practice, not one occurrence, so the engine's exception answer declines it. A plain dated
act with the same verb is still a report. Neutral fleets only; every call is the engine's
verifier with a forged re-ask answer.
"""

from __future__ import annotations

import pytest

from strata.scope_manager import (
    _CHANGE_OF_PRACTICE_RE,
    _PAST_TENSE_RE,
    verify_inherited_relation,
)

PUMPS = "Fuel dock pumps must be switched off at or before 20:00 every evening."


def _exception(text: str) -> dict:
    return {"kind": "exception", "inherited_id": "x", "instead_span": text.rstrip(".")}


STARTS = [
    "As of 3 October we shut the pumps at 23:00.",
    "As of 3 October the crew shut the pumps at 23:00.",
    "From 3 October on we shut the pumps at 23:00.",
    "Effective 3 October we shut the pumps at 23:00.",
    "Starting 3 October the crew shut the pumps at 23:00.",
    "Beginning on 3 October the crew shut the pumps at 23:00.",
    "From 3 October onwards the crew shut the pumps at 23:00.",
    "As of October 3 we shut the pumps at 23:00.",
    "As of 2026-10-03 we shut the pumps at 23:00.",
    "After 3 October we reset the pumps at 23:00.",
    "From 3 October we reset the pumps at 23:00.",
    "Post 3 October we reset the pumps at 23:00.",
    "After 3 October we shut the pumps at 23:00.",
    "From 3 October we shut the pumps at 23:00.",
    "After 2 March we ran the pumps 24/7.",
    "Effective immediately we shut the pumps at 23:00.",
]


@pytest.mark.parametrize("note", STARTS)
def test_a_practice_start_date_declines_under_the_exception_answer(note):
    verdict, _ = verify_inherited_relation(_exception(note), note, PUMPS)
    assert verdict == "decline"


@pytest.mark.parametrize(
    "note",
    [
        "On 3 October we shut the pumps at 23:00.",
        "On 3 October the crew shut the pumps at 23:00.",
        "After the storm on 3 October we shut the pumps at 23:00.",
    ],
)
def test_one_dated_act_stays_a_consequence_report(note):
    verdict, _ = verify_inherited_relation(_exception(note), note, PUMPS)
    assert verdict == "consequence_report"


@pytest.mark.parametrize("end", ["to", "through", "until", "-", "\u2013"])
def test_a_dated_range_stays_a_consequence_report(end):
    note = f"The pumps ran 24/7 from 2 March {end} 8 March."
    verdict, _ = verify_inherited_relation(_exception(note), note, PUMPS)
    assert verdict == "consequence_report"


def test_a_from_on_inside_a_dated_act_is_not_a_cue():
    note = "On 3 October we pumped fuel from the dock on the north side past 20:00."
    assert not _CHANGE_OF_PRACTICE_RE.search(note)
    verdict, _ = verify_inherited_relation(_exception(note), note, PUMPS)
    assert verdict != "decline"


@pytest.mark.parametrize(
    "text",
    [
        "fuel from the dock on the north side",
        "pumps shut from 3 October to 5 October",
        "pumps shut from 3 October through 5 October",
        "On 3 October we shut the pumps at 23:00.",
        "as of the audit we shut the pumps",
    ],
)
def test_the_cue_needs_a_real_date_and_a_bare_from_needs_on(text):
    assert not _CHANGE_OF_PRACTICE_RE.search(text)


@pytest.mark.parametrize(
    "word",
    ["dwelt", "spilt", "forsook", "oversaw", "slew", "overheard"],
)
def test_the_added_irregular_past_forms_match(word):
    assert _PAST_TENSE_RE.search(f"On 3 October the crew {word} it.")


@pytest.mark.parametrize("word", ["cast", "fit", "rid", "reset", "forecast", "misread", "sublet"])
def test_same_form_present_and_past_verbs_are_not_added(word):
    assert not _PAST_TENSE_RE.search(f"On 3 October the crew {word} it.")
