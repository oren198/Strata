"""The shipped reference bars (BARS_V1) and the one-item margin."""

from __future__ import annotations

from strata.judgecheck import BARS_V1, load_set, score
from tests.judgecheck_support import golden_result, with_decision

ITEMS = {i.id: i for i in load_set()}
SUITES = ("h2", "drift", "refinement", "cx", "p5", "j3", "a2")


def _run(wrong: set[str] = frozenset()):  # noqa: ANN202
    results = []
    for rep in range(3):
        for item in ITEMS.values():
            r = golden_result(item, rep)
            if item.id in wrong:
                r = with_decision(r, "decline" if r.decision != "decline" else "accept_as_context")
            results.append(r)
    return score(results, ITEMS, reps=3, strata_version="9.9.9", bars=BARS_V1)


def _cx_ids(n: int) -> set[str]:
    return set(sorted(i.id for i in ITEMS.values() if i.suite == "cx")[:n])


def test_the_bars_are_the_reference_run_with_a_one_item_margin_everywhere() -> None:
    assert BARS_V1 is not None
    assert BARS_V1.reference_id == "ref-public-x5+c1 @3cf796f set 1a62e3b1"
    assert dict(BARS_V1.margin_items) == dict.fromkeys(SUITES, 1)
    assert BARS_V1.suite_accuracy["drift"] == 55 / 60
    assert BARS_V1.suite_accuracy["j3"] == 60 / 65
    assert BARS_V1.suite_accuracy["a2"] == 29 / 40
    assert len(BARS_V1.must_admit_ids or ()) == 16


def test_the_golden_mock_meets_the_shipped_bars() -> None:
    s = _run()
    assert s.failed_legs == []
    assert s.verdict == "meets the judge-check v1 bar for Strata 9.9.9"


def test_a_judge_exactly_one_item_short_on_a_suite_still_meets() -> None:
    s = _run(_cx_ids(1))
    assert s.suites["cx"]["strict_accuracy"] < 1.0
    assert s.suites["cx"]["meets"] is True
    assert s.verdict == "meets the judge-check v1 bar for Strata 9.9.9"


def test_a_judge_two_items_short_on_a_12_item_suite_does_not_meet() -> None:
    s = _run(_cx_ids(2))
    assert s.suites["cx"]["n_items"] == 12
    assert s.suites["cx"]["meets"] is False
    assert s.verdict.startswith("does not meet the judge-check v1 bar")
