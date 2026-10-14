"""#244b: the comparator's false conflicts, by class. Legitimate tightenings stated in
another surface form (A), restating only part of a multi-valued parent (B), or adding a
requirement for a sub-case (C) must not conflict; the near misses still must."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.scope_manager import inherited_conflict, verify_directive_relation  # noqa: E402

# ---------------------------------------------------------------------------
# A: the same bound in another form
# ---------------------------------------------------------------------------

_A_LEGIT = [
    ("Backups must run every 30 minutes.", "Backups run twice an hour."),
    ("Backups must run every 30 minutes.", "Backups run four times an hour."),
    ("Backups must run every 30 minutes.", "Backups run half-hourly."),
    ("Backups must run every 12 hours.", "Backups run four times a day."),
    ("Results must be filed within 2 hours.", "Results must be filed within 90 minutes."),
    ("Pumps must be off at or before 20:00.", "Pumps must be off by 7 pm."),
    ("Pumps must be off at or before 20:00.", "Pumps must be off before noon."),
    ("Pumps must be off by 20:00.", "Pumps must be off by 6 pm."),
    ("Patients need at least two identifiers.", "Patients need at least three identifiers."),
    ("Doors open no earlier than 08:00.", "Doors open no earlier than 9 am."),
    (
        "Patients must be checked no more than 60 minutes apart.",
        "Patients are checked half-hourly.",
    ),
    (
        "Carts must be sealed no later than 90 minutes before departure.",
        "Carts are sealed no later than 120 minutes before departure.",
    ),
    ("Notices go out every 7 days.", "Notices go out every second day."),
]
_A_CONFLICT = [
    ("Backups must run every 30 minutes.", "Backups run hourly."),
    ("Backups must run every 12 hours.", "Backups run once a day."),
    ("Results must be filed within 90 minutes.", "Results must be filed within 2 hours."),
    ("Pumps must be off at or before 20:00.", "Pumps must be off by 9 pm."),
    ("Pumps must be off at or before 20:00.", "Pumps must be off by midnight."),
    ("Patients need at least two identifiers.", "Patients need at least one identifier."),
    ("Doors open no earlier than 08:00.", "Doors open no earlier than 7 am."),
    (
        "Carts must be sealed no later than 90 minutes before departure.",
        "Carts are sealed no later than 60 minutes before departure.",
    ),
]


@pytest.mark.parametrize(("parent", "child"), _A_LEGIT)
def test_a_stricter_bound_in_another_form_is_not_a_conflict(parent: str, child: str) -> None:
    assert inherited_conflict(child, parent) is None


@pytest.mark.parametrize(("parent", "child"), _A_CONFLICT)
def test_a_looser_bound_in_another_form_is_still_a_conflict(parent: str, child: str) -> None:
    assert inherited_conflict(child, parent) is not None


def test_units_that_do_not_convert_claim_no_conflict() -> None:
    assert (
        inherited_conflict("Water must stay below 2 inches.", "Water must stay below 5 cm.") is None
    )


def test_a_clock_time_is_not_mistaken_for_a_frequency() -> None:
    parent = "Seedlings must be watered at or before 07:00 every day."
    assert inherited_conflict("Seedlings must be watered at or before 09:00 every day.", parent)


# ---------------------------------------------------------------------------
# B: restating only part of the parent
# ---------------------------------------------------------------------------

_TOKENS = "Access tokens expire after at most 15 minutes and are rotated every 30 days."
_DOCK = "Dock doors must be closed and locked by 22:00, and the yard gate secured by 23:00."
_SAMPLES = "Reservoir chlorine residual must be sampled at 06:00, 14:00 and 22:00 each day."


@pytest.mark.parametrize(
    ("parent", "child"),
    [
        (_DOCK, "Dock doors at our depot are closed and locked by 21:30."),
        (_SAMPLES, "At the Eastside reservoir we add a 10:00 chlorine residual sample."),
        (
            "Carts must be sealed no later than 90 minutes before departure, "
            "chilled items at or below 5 C.",
            "Our dispatch team seals carts no later than 120 minutes before departure.",
        ),
        (
            "Logs are kept at most 14 days and must be encrypted with AES-256.",
            "Platform logs are kept at most 7 days.",
        ),
    ],
)
def test_omission_of_part_of_the_parent_is_not_exemption(parent: str, child: str) -> None:
    assert inherited_conflict(child, parent) is None


@pytest.mark.parametrize(
    ("parent", "child"),
    [
        (_DOCK, "Dock doors at our depot are closed and locked by 23:30."),
        (
            "Logs are kept at most 14 days and must be encrypted with AES-256.",
            "Platform logs are kept at most 7 days, encrypted with AES-128.",
        ),
        (
            "Logs are kept at most 14 days; archives are kept 90 days.",
            "Logs are kept at most 7 days; archives are kept 30 days.",
        ),
        ("Services must use TLS 1.3 or later.", "Services must use TLS 1.2 or later."),
        ("Backups run at 02:00.", "Backups run at 03:00."),
    ],
)
def test_a_changed_value_for_the_same_slot_is_still_a_conflict(parent: str, child: str) -> None:
    assert inherited_conflict(child, parent) is not None


def test_a_replacement_or_a_dropped_check_is_caught_by_the_directive_verifier() -> None:
    inherited = [("c_p", "g_up", "Overdue notices go out at 7 days, 14 days and 30 days.")]
    child = (
        "We send notices at 7 and 14 days, and a final notice at 45 days "
        "in place of the 30-day one."
    )
    assert verify_directive_relation({}, child, inherited)[0] == "decline"
    seals = [
        (
            "c_s",
            "g_up",
            "Trolley seals must be checked by two staff, at loading and at the aircraft door.",
        )
    ]
    dropped = "Two staff check every seal at the dock, and the aircraft door check is dropped."
    assert verify_directive_relation({}, dropped, seals)[0] == "decline"
    every_other = [
        ("c_l", "g_up", "Housekeeping must change the linens in every occupied room each day.")
    ]
    assert (
        verify_directive_relation(
            {}, "In long-stay rooms linens are changed every second day.", every_other
        )[0]
        == "decline"
    )


# ---------------------------------------------------------------------------
# C: a sub-case that adds a requirement is an addition, not a narrowing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parent", "child"),
    [
        (
            "Nurses must check the ward every shift.",
            "Night nurses must additionally log vital signs every hour.",
        ),
        (
            "Every delivery van is inspected before it leaves the depot.",
            "Refrigerated vans must also have the compressor logged before they leave.",
        ),
        (
            "All production deploys require an approved change ticket.",
            "For payments-service deploys the change ticket must additionally name a second "
            "reviewer, on top of the usual approved-ticket requirement.",
        ),
    ],
)
def test_a_sub_case_that_adds_a_requirement_does_not_narrow_the_universal_phrase(
    parent: str, child: str
) -> None:
    assert inherited_conflict(child, parent) is None


@pytest.mark.parametrize(
    ("parent", "child"),
    [
        (
            "Seedlings must be watered at or before 07:00 every day.",
            "Seedlings in house 3 must be watered at or before 06:00 on weekdays.",
        ),
        (
            "All production deploys require an approved change ticket.",
            "Deploys outside of office hours require a ticket.",
        ),
        (
            "All deploys require a rollback plan documented in the deploy ticket.",
            "Staging deploys need a plan.",
        ),
        (
            "All production deploys require an approved change ticket.",
            "Payments-service deploys need a ticket.",
        ),
    ],
)
def test_a_restriction_or_a_weaker_requirement_is_still_a_narrowing(
    parent: str, child: str
) -> None:
    assert inherited_conflict(child, parent) is not None


# ---------------------------------------------------------------------------
# Iteration 2: the rescue-then-decline regression, two-bound parents, class C
# without a modal, and polarity on another object
# ---------------------------------------------------------------------------

_RETENTION = "Application logs are retained for at most 14 days."
_TOKEN_LIFE = "Access tokens expire after at most 15 minutes."


def test_a_duration_bound_on_another_quantity_is_not_compared_across_units() -> None:
    child = "The auth squad keeps token-service logs for at most 7 days."
    assert inherited_conflict(child, _RETENTION) is None
    # the token parent's 15 minutes is a different quantity from the logs' 7 days
    assert inherited_conflict(child, _TOKEN_LIFE) is None


def test_the_same_quantity_in_another_unit_is_still_compared() -> None:
    assert (
        inherited_conflict("Access tokens expire after at most 2 hours.", _TOKEN_LIFE) is not None
    )
    assert (
        inherited_conflict("Access tokens expire after at most 600 seconds.", _TOKEN_LIFE) is None
    )


def test_a_rescued_grandchild_tightening_survives_the_inherited_check() -> None:
    from unittest.mock import MagicMock  # noqa: PLC0415

    from strata.fleet_config import Scope, Stratum  # noqa: PLC0415
    from strata.record_store import Contribution, ContributorRef  # noqa: PLC0415
    from strata.scope_manager import ScopeManager  # noqa: PLC0415
    from strata.summary_store import Directive, ScopeSummary  # noqa: PLC0415

    from .test_scope_manager import _fake_response  # noqa: PLC0415

    stamp = "2026-10-01T00:00:00+00:00"
    parents = [
        Directive(id="c_ret", content=_RETENTION, source_scope_id="g_org", created_at=stamp),
        Directive(id="c_tok", content=_TOKEN_LIFE, source_scope_id="g_org", created_at=stamp),
    ]
    text = "The auth squad keeps token-service logs for at most 7 days."
    client = MagicMock()
    client.messages.create.side_effect = [
        _fake_response(
            {
                "decision": "decline",
                "reasoning": "Contradicts inherited directive c_ret.",
                "directive_ops": [],
                "new_context": None,
            }
        ),
        _fake_response(
            {
                "relation": "tightens",
                "other_grounds_clear": True,
                "parent_id": "c_ret",
                "kept_span": "at most 14 days",
                "tighten_kind": "rule",
                "reasoning": "stricter retention",
            }
        ),
    ]
    judgment = ScopeManager(client=client).judge(
        scope=Scope(id="g_squad", name="Auth squad", stratum_id="L2"),
        stratum=Stratum(id="L2", name="squad", ordinal=2),
        current_summary=ScopeSummary(
            scope_id="g_squad", directives=[], context="", updated_at=stamp
        ),
        recent_contributions=[],
        new_contribution=Contribution(
            id="c_new",
            scope_id="g_squad",
            content=text,
            proposed_classification="directive",
            subject=None,
            supersedes=None,
            contributor=ContributorRef(
                scope_id="g_squad", skill=None, session_id="s", ts="2026-10-12T10:00:00+00:00"
            ),
            created_at=stamp,
        ),
        ancestor_directives=[("g_org", parents)],
    )
    assert judgment.decision == "accept_as_directive", judgment.reasoning
    assert judgment.inherited_holds == []


@pytest.mark.parametrize(
    ("child", "legit"),
    [
        (
            "Our chlorine residual must be at or above 0.8 mg/L, and the dose no more than 3 mg/L.",
            True,
        ),
        ("Our chlorine residual must be at or above 0.8 mg/L.", True),
        ("Our dose must be no more than 3 mg/L.", True),
        (
            "Our chlorine residual must be at or above 0.3 mg/L, and the dose no more than 3 mg/L.",
            False,
        ),
        (
            "Our chlorine residual must be at or above 0.8 mg/L, and the dose no more than 6 mg/L.",
            False,
        ),
    ],
)
def test_a_two_bound_parent_is_compared_bound_by_bound(child: str, legit: bool) -> None:
    parent = (
        "Chlorine residual must be at or above 0.5 mg/L, and the dose must be no more than 4 mg/L."
    )
    assert (inherited_conflict(child, parent) is None) is legit


def test_a_present_indicative_sub_case_with_an_added_requirement_is_an_addition() -> None:
    parent = "Every delivery van is inspected before it leaves the depot."
    child = (
        "Refrigerated vans: every van is inspected before it leaves the depot, and the driver "
        "also logs the cabinet temperature."
    )
    assert inherited_conflict(child, parent) is None
    assert (
        inherited_conflict("Short-run vans: every van is inspected only on Mondays.", parent)
        is not None
    )


def test_a_state_word_on_another_object_is_not_a_polarity_flip() -> None:
    parent = "Fuel dock pumps must be switched off at or before 20:00 every evening."
    assert (
        inherited_conflict(
            "Fuel dock pumps must be switched off by 19:30 and the nozzles locked.", parent
        )
        is None
    )
    assert inherited_conflict("Night-shift fuel dock pumps are kept on until 22:00.", parent)
