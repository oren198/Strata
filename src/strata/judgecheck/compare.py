"""Paired A/B: two judges' results over the same set and reps, item by item."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from strata.judgecheck.items import Item
from strata.judgecheck.scoring import ItemResult, grade


def compare(
    incumbent: Sequence[ItemResult], candidate: Sequence[ItemResult], items: Mapping[str, Item]
) -> dict[str, Any]:
    """Per-item strict flips between the two judges and the suite deltas.

    ``items`` is keyed by item id; a (item, rep) present for one judge only is ignored (an
    incomplete run). ``flips`` lists every (item, rep) where exactly one judge was strictly
    right; ``suites[s]`` has both strict counts over the common judgments."""
    cand = {(r.item_id, r.rep): r for r in candidate}
    flips: list[dict[str, Any]] = []
    suites: dict[str, dict[str, int]] = {}
    for a in incumbent:
        b = cand.get((a.item_id, a.rep))
        if b is None:
            continue
        item = items[a.item_id]
        sa, sb = grade(item, a)[0], grade(item, b)[0]
        entry = suites.setdefault(
            item.suite, {"judgments": 0, "incumbent_strict": 0, "candidate_strict": 0}
        )
        entry["judgments"] += 1
        entry["incumbent_strict"] += int(sa)
        entry["candidate_strict"] += int(sb)
        if sa != sb:
            flips.append(
                {
                    "item_id": a.item_id,
                    "suite": item.suite,
                    "rep": a.rep,
                    "incumbent": {"decision": a.decision, "strict": sa},
                    "candidate": {"decision": b.decision, "strict": sb},
                }
            )
    for entry in suites.values():
        entry["delta"] = entry["candidate_strict"] - entry["incumbent_strict"]
    flips.sort(key=lambda f: (f["suite"], f["item_id"], f["rep"]))
    return {"suites": suites, "flips": flips}
