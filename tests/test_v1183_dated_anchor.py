"""1.18.3: a slash date anchors a report only when it carries a year.

A bare day/month (``24/7``, ``3/10``) is not a date. ``d/m/yy`` and ``d/m/yyyy`` are.
"""

from __future__ import annotations

from strata.scope_manager import _DATED_ANCHOR_RE, _is_dated_past_report


def test_a_bare_ratio_anchors_nothing() -> None:
    text = "The pumps ran 24/7."
    assert _DATED_ANCHOR_RE.search(text) is None
    assert _is_dated_past_report(text) is False


def test_last_week_is_the_only_anchor_beside_a_bare_ratio() -> None:
    text = "The pumps ran 24/7 last week."
    match = _DATED_ANCHOR_RE.search(text)
    assert match is not None
    assert match.group(0) == "last week"
    assert _is_dated_past_report(text) is True


def test_a_slash_date_with_a_year_is_anchored() -> None:
    text = "On 3/10/2026 the pumps ran."
    match = _DATED_ANCHOR_RE.search(text)
    assert match is not None
    assert match.group(0) == "3/10/2026"
    short = "On 3/10/26 the pumps ran."
    short_match = _DATED_ANCHOR_RE.search(short)
    assert short_match is not None
    assert short_match.group(0) == "3/10/26"
    # A bare day/month in the same sentence is not a second anchor.
    assert _DATED_ANCHOR_RE.search("On 3/10 the pumps ran.") is None
    assert _DATED_ANCHOR_RE.findall("On 3/10/2026 the pumps ran 24/7.") == ["3/10/2026"]
    assert _is_dated_past_report(text) is True
