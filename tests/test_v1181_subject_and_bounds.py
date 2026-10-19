"""1.18.1: a shared product, team or site name is not a shared subject; rate and count bounds
read the right way.

The fleet is six inherited rules that all name the product `acme-api`. A child's legitimate
tightening is compared with every one of them; before 1.18.1 an unrelated rule that only
shared the product name declined it. Every path that runs the subject test is pinned here, and
each test names the retention rule when an exception is caught: the right rule, not any rule.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from strata.change_events import find_chain_conflicts
from strata.scope_manager import (
    Directive,
    ScopeManager,
    _first_inherited_conflict,
    _tighten_value_check,
    inherited_conflict,
    inherited_context_words,
    inherited_for_context,
    verify_directive_relation,
    verify_inherited_relation,
    verify_relation_ground,
)
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

RETENTION = "Customer data in acme-api is retained for a maximum of 14 days."
DEPLOY = "Every production deploy of acme-api requires two reviewer approvals before merge."
TESTS = "Run the full test suite before publishing any release of acme-api."
VERSION = "The version of acme-api lives only in VERSION.txt as a plain semver string."
TOKENS = "Access tokens issued by acme-api expire after at most 15 minutes."
LOGS = "Customer email addresses and phone numbers are always redacted in all acme-api logs."
FLEET = [
    ("c_deploy", "g_org", DEPLOY),
    ("c_tests", "g_org", TESTS),
    ("c_retention", "g_org", RETENTION),
    ("c_version", "g_org", VERSION),
    ("c_tokens", "g_org", TOKENS),
    ("c_logs", "g_org", LOGS),
]
TEXTS = [text for *_, text in FLEET]

TIGHTEN_RETENTION = "Customer data in acme-api is retained for a maximum of 7 days."
TIGHTEN_TOKENS = "Access tokens issued by acme-api expire after at most 10 minutes."
EXCEPTION_RETENTION = "Customer data in acme-api is retained for a maximum of 90 days."
UNRELATED = "Release notes for acme-api are written in plain English."


def _directives() -> list[tuple[str, list[Directive]]]:
    return [
        (
            "g_org",
            [
                Directive(
                    id=i,
                    content=t,
                    subject="acme",
                    source_scope_id="g_org",
                    source_skill="lead",
                    created_at="2026-03-01T09:00:00+00:00",
                )
                for i, _o, t in FLEET
            ],
        )
    ]


# -- the context words ------------------------------------------------------


def test_a_name_that_only_qualifies_the_subject_is_a_context_word() -> None:
    words = inherited_context_words(TEXTS)
    assert "acme" in words
    assert "retain" not in words and "deploy" not in words


def test_a_word_in_a_head_phrase_is_never_a_context_word() -> None:
    siblings = [
        "Frozen pallets must be stored at -18 C or colder.",
        "Frozen pallets are labelled with the packing date.",
    ]
    words = inherited_context_words(siblings)
    assert "frozen" not in words and "pallet" not in words


def test_one_inherited_rule_has_no_context_words() -> None:
    assert inherited_context_words([RETENTION]) == frozenset()


# -- step 1, the pure comparison --------------------------------------------


def test_a_tightening_has_no_conflict_with_any_sibling_rule() -> None:
    inherited = list(FLEET)
    assert _first_inherited_conflict(TIGHTEN_RETENTION, inherited) is None
    assert _first_inherited_conflict(TIGHTEN_TOKENS, inherited) is None
    assert _first_inherited_conflict(UNRELATED, inherited) is None


def test_the_exception_is_caught_by_its_own_rule() -> None:
    hit = _first_inherited_conflict(EXCEPTION_RETENTION, list(FLEET))
    assert hit is not None
    assert hit[0] == "c_retention"
    assert hit[2] == "not stricter in the same direction"


def test_the_pairwise_call_without_the_set_is_the_old_behaviour() -> None:
    """The default is the empty set, and that is exactly the 1.18.0 defect: callers pass it."""
    assert inherited_conflict(TIGHTEN_RETENTION, DEPLOY) is not None
    assert inherited_conflict(TIGHTEN_RETENTION, DEPLOY, inherited_context_words(TEXTS)) is None


# -- step 1, the single and batch judge paths --------------------------------


def _judge_single(content: str):  # noqa: ANN202
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        {
            "decision": "accept_as_directive",
            "reasoning": "An enforceable rule.",
            "directive_ops": [{"op": "append"}],
            "new_context": None,
        }
    )
    return ScopeManager(client=mock_client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=replace(NEW_CONTRIBUTION, content=content, subject=None),
        ancestor_directives=_directives(),
    )


def test_single_path_admits_the_tightening() -> None:
    judgment = _judge_single(TIGHTEN_RETENTION)
    assert judgment.decision == "accept_as_directive"
    assert judgment.inherited_holds == []


def test_single_path_declines_the_exception_by_the_retention_rule() -> None:
    judgment = _judge_single(EXCEPTION_RETENTION)
    assert judgment.decision == "decline"
    assert judgment.inherited_holds[0]["directive_id"] == "c_retention"


def _judge_two(first_text: str, second_text: str):  # noqa: ANN202
    first = replace(NEW_CONTRIBUTION, content=first_text, subject=None)
    second = replace(SECOND_CONTRIBUTION, content=second_text, subject=None)
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
        mock_client, contributions=[first, second], ancestor_directives=_directives()
    )
    return result, first, second


def test_batch_path_admits_the_tightening_and_declines_the_exception() -> None:
    result, first, second = _judge_two(TIGHTEN_TOKENS, EXCEPTION_RETENTION)
    decisions = {v.contribution_id: v.decision for v in result.verdicts}
    assert decisions == {first.id: "accept_as_directive", second.id: "decline"}
    assert [h["directive_id"] for h in result.inherited_holds] == ["c_retention"]


# -- step 3, the loose matcher ------------------------------------------------


def test_step3_admits_the_tightening_and_an_unrelated_rule() -> None:
    for text in (TIGHTEN_RETENTION, TIGHTEN_TOKENS, UNRELATED):
        verdict, reason, target, _fb = verify_directive_relation({}, text, FLEET)
        assert verdict == "admit", (text, reason)
        assert target is None


def test_step3_names_the_retention_rule_for_the_exception() -> None:
    verdict, _reason, target, _fb = verify_directive_relation({}, EXCEPTION_RETENTION, FLEET)
    assert verdict == "decline"
    assert target == ("c_retention", "g_org")


def test_step3_loose_test_does_not_count_the_product_name() -> None:
    """The loose matcher has its own subject test; two shared stems used to be enough."""
    child = "Staging builds of acme-api are deleted after 30 days."
    verdict, _r, _t, _fb = verify_directive_relation({}, child, FLEET)
    assert verdict == "admit"


def test_step3_a_trimmed_list_needs_the_set_from_the_whole_list() -> None:
    trimmed = [FLEET[0], FLEET[2]]
    with_set = verify_directive_relation(
        {}, TIGHTEN_RETENTION, trimmed, inherited_context_words(TEXTS)
    )
    assert with_set[0] == "admit"


# -- #237 rescue ---------------------------------------------------------------


def test_rescue_refines_a_sibling_subject_only_with_the_set() -> None:
    child = "Staging builds of acme-api are deleted after 30 days."
    answer = {
        "relation": "refines",
        "parent_id": "c_retention",
        "other_grounds_clear": True,
        "subject_span": "Staging builds of acme-api",
        "classification": "directive",
    }
    without, why, _c, _x = verify_relation_ground(answer, RETENTION, child, "directive")
    with_set, why2, _c2, _x2 = verify_relation_ground(
        answer, RETENTION, child, "directive", inherited_context_words(TEXTS)
    )
    assert not without and "covered subject" in why
    assert with_set, why2


# -- #242 context check ---------------------------------------------------------


def test_context_about_an_unrelated_rule_is_not_covered_by_the_product_name() -> None:
    text = "Deploys of acme-api went out at 14:00 on the third and nothing broke."
    verdict, reason = verify_inherited_relation(
        {"kind": "unrelated", "inherited_id": "c_retention"},
        text,
        RETENTION,
        [t for t in TEXTS if t != RETENTION],
    )
    assert verdict == "admit", reason


def test_context_that_undercuts_the_retention_rule_is_still_declined() -> None:
    text = "Customer data in acme-api may be kept for 90 days."
    verdict, _reason = verify_inherited_relation(
        {"kind": "unrelated", "inherited_id": "c_retention"},
        text,
        RETENTION,
        [t for t in TEXTS if t != RETENTION],
    )
    assert verdict == "decline"


# -- #243 publication check --------------------------------------------------------


def _publication_check(text: str, kind: str = "directive"):  # noqa: ANN202
    return ScopeManager(client=MagicMock()).check_publication_exception(
        scope=SCOPE,
        content=text,
        kind=kind,  # type: ignore[arg-type]
        ancestor_directives=_directives(),
        operator_memory=None,
    )


def test_publication_check_lets_a_tightening_through_and_refuses_the_exception() -> None:
    assert _publication_check(TIGHTEN_RETENTION).decision == "accept"
    refused = _publication_check(EXCEPTION_RETENTION)
    assert refused.decision == "refuse"
    assert refused.note is not None and "c_retention" in refused.note


# -- D8 chain conflicts --------------------------------------------------------------


def test_a_newly_inherited_set_does_not_conflict_with_an_own_tightening() -> None:
    own = [("c_own_ret", TIGHTEN_RETENTION), ("c_own_tok", TIGHTEN_TOKENS)]
    assert find_chain_conflicts(scope_id="g_web", own=own, gained=list(FLEET)) == []


def test_a_newly_inherited_set_conflicts_by_the_retention_rule_only() -> None:
    found = find_chain_conflicts(
        scope_id="g_web", own=[("c_own", EXCEPTION_RETENTION)], gained=list(FLEET)
    )
    assert [f["inherited_id"] for f in found] == ["c_retention"]


# -- the re-ask display ------------------------------------------------------------


def test_the_shown_rules_are_ranked_without_the_product_name() -> None:
    """Sibling rules that share the product name must not push the true rule out of the
    re-ask's character budget."""
    padded = [
        (f"c_pad{i}", "g_org", f"Export jobs of acme-api run nightly, batch {i}.")
        for i in range(60)
    ]
    inherited = [*padded, ("c_true", "g_org", "Customer data must not leave the EU.")]
    shown = inherited_for_context("Customer data in acme-api is exported to the US.", inherited)
    assert "c_true" in [d[0] for d in shown]


# -- defect (b): rate and count bounds -----------------------------------------------

_BOUND_ROWS = [
    # (parent, tighter child, looser child)
    (
        "Push notifications are limited to at most 3 per day.",
        "Push notifications are limited to at most 1 per day.",
        "Push notifications are limited to at most 10 per day.",
    ),
    (
        "Deploys happen no more than 4 times per week.",
        "Deploys happen no more than 2 times per week.",
        "Deploys happen no more than 7 times per week.",
    ),
    (
        "Backups run at least 2 times per day.",
        "Backups run at least 4 times per day.",
        "Backups run at least 1 time per day.",
    ),
    (
        "Every production deploy requires two reviewer approvals before merge.",
        "Every production deploy requires three reviewer approvals before merge.",
        "Every production deploy requires one reviewer approval before merge.",
    ),
    # forms that read correctly before 1.18.1 and must keep doing so
    (
        "Each user may send up to 50 messages a day.",
        "Each user may send up to 20 messages a day.",
        "Each user may send up to 200 messages a day.",
    ),
    (
        "Fire doors are tested at least once a week.",
        "Fire doors are tested at least twice a week.",
        "Fire doors are tested at least once a month.",
    ),
    (
        "Every release needs at least 2 sign-offs.",
        "Every release needs at least 3 sign-offs.",
        "Every release needs at least 1 sign-off.",
    ),
    (
        "Each on-call shift covers at most 12 hours.",
        "Each on-call shift covers at most 8 hours.",
        "Each on-call shift covers at most 16 hours.",
    ),
]


@pytest.mark.parametrize(("parent", "tighter", "looser"), _BOUND_ROWS)
def test_a_stricter_bound_passes_and_a_looser_one_is_caught(
    parent: str, tighter: str, looser: str
) -> None:
    assert inherited_conflict(tighter, parent) is None
    assert _tighten_value_check(parent, tighter)[0]
    assert inherited_conflict(looser, parent) == "not stricter in the same direction"


def test_a_rate_ceiling_is_not_read_as_an_interval_ceiling() -> None:
    """ "at most 3 per day" bounds the rate; read as "at most every 8 hours" it tightened the
    wrong way round."""
    parent = "Push notifications are limited to at most 3 per day."
    assert not _tighten_value_check(
        parent, "Push notifications are limited to at most 10 per day."
    )[0]
