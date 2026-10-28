"""The reference bars of judge-check v1.

Measured on the reference run: qwen/qwen3-235b-a22b-2507 on Parasail, the frozen 78-item set,
5 reps public plus the hold-out (``ref-public-x5+c1 @3cf796f set 1a62e3b1``). A build without
bars still computes the hard legs and the operational counts, and says "unmeasured: no
reference bars in this build" instead of "meets".

Margin floor: margin floor of 1 item set 2026-10-26 after the reference run, before any
candidate run; the formula alone gave 0 on 6 of 7 suites. (The formula is the reference's own
rep-to-rep spread in items; the reference spread was 0 everywhere except a2 = 1. CEO ruling
20261026T161216Z: margin = max(spread, 1).) The hard legs keep no margin.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Bars:
    """What a candidate is held to.

    *suite_accuracy*: the reference's strict accuracy per suite (0..1). *margin_items*: the
    reference's own rep-to-rep spread per suite, in whole items (max - min across its reps,
    rounded up); a candidate's bar is the reference accuracy less that many items. There is no
    margin on the hard legs. *must_admit_ids*: the must-admit items the reference admitted in
    every one of its reps; when None, every golden-admit item of the must-admit suites is
    required. *reference_id*: the run these numbers came from.
    """

    suite_accuracy: Mapping[str, float]
    margin_items: Mapping[str, int]
    must_admit_ids: frozenset[str] | None
    reference_id: str


BARS_V1: Bars | None = Bars(
    suite_accuracy={
        "h2": 1.0,
        "drift": 55 / 60,
        "refinement": 1.0,
        "cx": 1.0,
        "p5": 1.0,
        "j3": 60 / 65,
        "a2": 29 / 40,
    },
    margin_items={"h2": 1, "drift": 1, "refinement": 1, "cx": 1, "p5": 1, "j3": 1, "a2": 1},
    must_admit_ids=frozenset(
        {
            "j1r-002",
            "j1r-005",
            "j1r-010",
            "j1r-013",
            "j1r-018",
            "j1r-022",
            "p243-03",
            "p243-04",
            "p243-05",
            "p243-10",
            "p243-12",
            "p243-17",
            "p243-18",
            "p243-21",
            "p243-22",
            "p243-30",
        }
    ),
    reference_id="ref-public-x5+c1 @3cf796f set 1a62e3b1",
)
"""The reference run's numbers (a separate, labelled commit)."""
