"""The reference bars of judge-check v1.

Empty until the reference run (qwen/qwen3-235b-a22b-2507 on Parasail, 5 reps on the frozen set)
is done; the numbers are then filled in by a separate commit that names the reference run id.
A build without bars still computes the hard legs and the operational counts, and says
"unmeasured: no reference bars in this build" instead of "meets".
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


BARS_V1: Bars | None = None
"""Set by the labelled commit that records the reference run."""
