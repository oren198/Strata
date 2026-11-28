"""judge-check reports `hearsay_labels_on_first_hand` (lexical, report-only, no bar)."""

from __future__ import annotations

from dataclasses import replace

from strata.judgecheck import load_set, score
from tests.judgecheck_support import golden_result

ITEMS = {i.id: i for i in load_set()}


def _item_with(has_cue: bool):  # noqa: ANN202
    from strata.telling import has_telling_act  # noqa: PLC0415

    for item in ITEMS.values():
        data = item.data.get("contribution") or item.data.get("proposal") or {}
        if has_telling_act(str(data.get("content", ""))) == has_cue:
            return item
    raise AssertionError("the set has no such item")


def _run(mutate=None):  # noqa: ANN001, ANN202
    results = []
    for item in ITEMS.values():
        r = golden_result(item, 0)
        results.append(mutate(item, r) if mutate else r)
    return score(results, ITEMS, reps=1, strata_version="9.9.9")


def test_a_clean_run_reports_zero() -> None:
    assert _run().operational["hearsay_labels_on_first_hand"] == 0


def test_a_hearsay_label_on_a_note_with_no_telling_act_is_counted() -> None:
    target = _item_with(False)
    s = _run(
        lambda item, r: (
            replace(r, reasoning="Hearsay: informant (keeper) told the agent.")
            if item.id == target.id
            else r
        )
    )
    assert s.operational["hearsay_labels_on_first_hand"] == 1


def test_a_hearsay_label_on_a_note_with_a_telling_act_is_not_counted() -> None:
    target = _item_with(True)
    s = _run(
        lambda item, r: (
            replace(r, reasoning="Hearsay: informant Priya told the agent.")
            if item.id == target.id
            else r
        )
    )
    assert s.operational["hearsay_labels_on_first_hand"] == 0


def test_the_counter_never_changes_the_verdict() -> None:
    target = _item_with(False)
    clean, flagged = (
        _run(),
        _run(lambda item, r: replace(r, reasoning="Hearsay: x") if item.id == target.id else r),
    )
    assert clean.verdict == flagged.verdict and clean.failed_legs == flagged.failed_legs
