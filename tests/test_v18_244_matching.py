"""#244: inherited-rule matching on the shipping judge. Fix 1: the comparator."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.scope_manager import _tighten_value_check, inherited_conflict  # noqa: E402

_TOKENS = "Access tokens expire after at most 15 minutes."
_LOGS = "Application logs are retained for at most 14 days."
_PUMPS = "Pumps are switched off at or before 20:00."


@pytest.mark.parametrize(
    ("parent", "child"),
    [
        (_TOKENS, "Platform-issued access tokens expire after 10 minutes."),
        (_TOKENS, "Platform-issued access tokens expire after ten minutes."),
        (_TOKENS, "Platform-issued access tokens expire after at most 10 minutes."),
        (_LOGS, "The platform team retains application logs for 7 days."),
        (_LOGS, "The platform team retains application logs for seven days."),
        (_PUMPS, "Pumps in hall 2 are switched off at 19:00."),
        ("Tokens expire after at least 10 minutes.", "Tokens expire after 20 minutes."),
    ],
)
def test_a_bare_value_inside_the_bound_is_a_tightening(parent: str, child: str) -> None:
    assert inherited_conflict(child, parent) is None


@pytest.mark.parametrize(
    ("parent", "child"),
    [
        (_TOKENS, "Platform-issued access tokens expire after 20 minutes."),
        (_TOKENS, "Platform tokens expire after at least 10 minutes."),
        (_LOGS, "The platform team retains application logs for 30 days."),
        (_PUMPS, "Pumps in hall 2 are switched off at 21:00."),
        ("Tokens expire after at least 10 minutes.", "Tokens expire after 5 minutes."),
    ],
)
def test_a_bare_value_outside_the_bound_or_under_another_comparator_still_conflicts(
    parent: str, child: str
) -> None:
    assert inherited_conflict(child, parent) is not None


def test_two_different_same_unit_values_are_not_guessed() -> None:
    # Ambiguous: which one is the retention? Fails toward the rule.
    ok, _reason = _tighten_value_check(_LOGS, "Logs are kept 7 days locally and 30 days offsite.")
    assert not ok


def test_the_bare_value_must_have_the_parents_unit() -> None:
    ok, _reason = _tighten_value_check(_TOKENS, "Tokens expire after 10 hours.")
    assert not ok
