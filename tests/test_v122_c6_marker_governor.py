"""C6 (broad-directive false declines): on an ``unrelated`` answer, a marker no longer declines
when the clause carrying it is the contributor's OWN act or artefact. Engine floor only: no
judge field, a closed artefact list, and fail closed (an unclassifiable clause declines as today).

Pure tests on :func:`verify_inherited_relation`. The switch ``_MARKER_GOVERNOR_CHECK`` is tested
in both positions; off reproduces today's verdicts.
"""

from __future__ import annotations

import pytest

from strata import scope_manager as sm
from strata.scope_manager import verify_inherited_relation

MISSION = (
    "The operator's mission is shared memory for agent fleets. "
    "Every contribution is judged by the configured judge provider before it counts. "
    "Users own their memory and may delete it. "
    "Reviews must not be skipped for any release."
)
UNRELATED = {"kind": "unrelated"}

# Must-admit shapes the old test declines (marker + one shared stem with the whole directive).
ADMIT_FIRST_PERSON = "I added an optional JUDGE_PROVIDER environment variable."
ADMIT_ARTEFACT = "This PR adds an optional JUDGE_PROVIDER environment variable."
ADMIT_KEEPS = "My edit keeps the judge provider list unchanged."

# Must-decline twins: same marker, governed by a person, a relayed party, or the rule itself.
DECLINE_PERSON = "The operator said staff may skip the judge provider check."
DECLINE_CREATES_EXCEPTION = "I added an optional flag to skip the review."
DECLINE_RULE_SUBJECT = "My edit keeps the review optional."
DECLINE_FIRST_PERSON_HABIT = "We skip the judge provider check for hotfixes."
DECLINE_NORM_IN_OWN_ACT = "I added a note that staff may skip the judge provider check."
DECLINE_MIXED = "This PR adds an optional judge provider variable, and staff may skip review."
DECLINE_UNCLASSIFIED = "Adds an optional judge provider variable."
DECLINE_MODAL_OWN = "We may skip the judge provider check."


def _verdict(content: str) -> tuple[str, str]:
    return verify_inherited_relation(UNRELATED, content, MISSION)


@pytest.mark.parametrize(
    "content, token",
    [
        (ADMIT_FIRST_PERSON, "own-act:first_person"),
        (ADMIT_ARTEFACT, "own-act:artefact"),
        (ADMIT_KEEPS, "own-act:artefact"),
    ],
    ids=["first-person", "artefact", "keeps"],
)
def test_an_own_act_marker_clause_is_admitted_and_says_how(content: str, token: str) -> None:
    verdict, reason = _verdict(content)
    assert verdict == "admit"
    assert token in reason


@pytest.mark.parametrize(
    "content",
    [
        DECLINE_PERSON,
        DECLINE_CREATES_EXCEPTION,
        DECLINE_RULE_SUBJECT,
        DECLINE_FIRST_PERSON_HABIT,
        DECLINE_NORM_IN_OWN_ACT,
        DECLINE_MIXED,
        DECLINE_UNCLASSIFIED,
        DECLINE_MODAL_OWN,
    ],
    ids=[
        "person",
        "creates-exception",
        "rule-subject",
        "first-person-habit",
        "norm-in-own-act",
        "mixed-clauses",
        "unclassified",
        "modal-own",
    ],
)
def test_every_other_marker_clause_still_declines(content: str) -> None:
    verdict, reason = _verdict(content)
    assert verdict == "decline", reason


def test_an_own_act_that_creates_the_exception_declines_with_its_reason() -> None:
    verdict, reason = _verdict(DECLINE_CREATES_EXCEPTION)
    assert verdict == "decline"
    assert "covers an inherited subject" in reason


def test_a_note_with_no_marker_is_untouched() -> None:
    assert _verdict("This PR adds the JUDGE_PROVIDER environment variable.")[0] == "admit"


@pytest.mark.parametrize("kind", [{"kind": "exception"}, {"kind": "garbage"}, {}])
def test_only_a_readable_unrelated_answer_is_affected(kind: dict) -> None:
    """A forged or unreadable answer must not open the own-act path."""
    verdict, _ = verify_inherited_relation(kind, ADMIT_FIRST_PERSON, MISSION)
    assert verdict == "decline"


def test_the_artefact_list_is_closed() -> None:
    assert (
        frozenset(
            {
                "edit",
                "change",
                "pr",
                "patch",
                "commit",
                "field",
                "flag",
                "option",
                "variable",
                "version",
                "draft",
            }
        )
        == sm._OWN_ARTEFACT_NOUNS
    )


def test_the_switch_is_on_by_default() -> None:
    assert sm._MARKER_GOVERNOR_CHECK is True


@pytest.mark.parametrize("content", [ADMIT_FIRST_PERSON, ADMIT_ARTEFACT, ADMIT_KEEPS])
def test_switch_off_declines_exactly_as_today(
    content: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sm, "_MARKER_GOVERNOR_CHECK", False)
    verdict, reason = _verdict(content)
    assert verdict == "decline"
    assert "own-act" not in reason


@pytest.mark.parametrize(
    "content",
    [DECLINE_PERSON, DECLINE_CREATES_EXCEPTION, DECLINE_RULE_SUBJECT, DECLINE_UNCLASSIFIED],
)
def test_switch_off_leaves_the_twins_declined(
    content: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sm, "_MARKER_GOVERNOR_CHECK", False)
    assert _verdict(content)[0] == "decline"
