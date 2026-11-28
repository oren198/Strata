"""Which must-decline items measure the JUDGE and which the ENGINE.

For a must-decline item, ask: if the judge wrongly ADMITS it, does the engine's own checks (the
inherited-context check, the directive-relation check, the position gate) still decline it? If
so the item is `rescued`: a weak judge cannot fail it, so it says little about the judge. If the
engine lets the admission through the item is `judge_dependent`: only the judge's own decision
keeps it out, and that is what the security leg is meant to measure.

The probe is offline and deterministic: a client that makes the judge admit and answers every
re-ask with "unrelated" (the answer that gives the engine's mechanical checks the least help).

    python -m strata.judgecheck.dependence ITEMS.jsonl [--json]

reads any items file (the shipped set's shape: one JSON object per line with `judgecheck_suite`
and `golden`), without the set's sha check, and prints one line per item.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Sequence
from types import SimpleNamespace
from typing import Any

from strata.judgecheck.items import Item, load_items_unchecked

RESCUED = "rescued"
JUDGE_DEPENDENT = "judge_dependent"
NOT_APPLICABLE = "not_applicable"

#: Suites whose must-decline items make up the security leg.
SECURITY_SUITES = ("h2", "drift", "j3")


class _AdmitClient:
    """An anthropic-shaped client whose judge ADMITS and whose re-asks say "unrelated"."""

    def __init__(self, classification: str) -> None:
        self.classification = classification
        self.messages = self
        self.api_key = "probe-not-a-key"
        self.base_url = "https://probe.invalid/v1"

    def create(self, **kwargs: Any) -> Any:
        tool = kwargs["tool_choice"]["name"]
        user = kwargs["messages"][0]["content"]
        listed = re.search(r"^- (\S+?): ", user, re.MULTILINE)
        first = listed.group(1) if listed else "unknown"
        if tool == "submit_judgment":
            directive = self.classification == "directive"
            payload: dict[str, Any] = {
                "decision": "accept_as_directive" if directive else "accept_as_context",
                "reasoning": "probe: admit",
                "directive_ops": [{"op": "append"}] if directive else [],
                "new_context": None if directive else "Context note.",
            }
        elif tool in ("classify_inherited_relation", "classify_own_directive_relation"):
            payload = {"kind": "unrelated", "inherited_id": first, "reasoning": "probe"}
        elif tool == "classify_directive_relation":
            payload = {"relation": "unrelated", "parent_id": first, "reasoning": "probe"}
        else:
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="no")],
                usage=SimpleNamespace(input_tokens=0, output_tokens=0),
            )
        block = SimpleNamespace(type="tool_use", name=tool, id="tu_probe", input=payload)
        return SimpleNamespace(
            content=[block], usage=SimpleNamespace(input_tokens=0, output_tokens=0)
        )


def classify_item(item: Item) -> str:
    """``rescued`` / ``judge_dependent`` for a must-decline item of the security suites;
    ``not_applicable`` for anything else (it takes no part in the security leg)."""
    from strata.judgecheck.cost import Budget  # noqa: PLC0415
    from strata.judgecheck.runner import (  # noqa: PLC0415
        CountingClient,
        JudgeSpec,
        isolated_environment,
        run_one,
    )

    if item.suite not in SECURITY_SUITES or item.golden.get("decision") != "decline":
        return NOT_APPLICABLE
    # A wrong judge can admit in either form (as a directive or as context), so the item is
    # rescued only if the engine declines BOTH admissions.
    for classification in ("directive", "context"):
        spec = JudgeSpec("probe", _AdmitClient(classification), "probe-model")
        with isolated_environment() as env:
            client = CountingClient(spec.client, Budget(price=None, max_usd=None))
            result = run_one(item, 0, spec, client, env)
        if result.decision in ("judge_error", "tool_error", "timeout"):
            # a broken probe must never read as "the engine let it through"
            raise RuntimeError(f"dependence probe failed on {item.id}: {result.reasoning}")
        if result.decision != "decline":
            return JUDGE_DEPENDENT
    return RESCUED


def classify_judge_dependence(items: Iterable[Item]) -> dict[str, str]:
    """``item id -> rescued | judge_dependent | not_applicable`` for every item."""
    return {item.id: classify_item(item) for item in items}


def judge_dependent_ids(items: Iterable[Item]) -> frozenset[str]:
    """The ids of the must-decline items only the judge keeps out (what ``score`` takes)."""
    return frozenset(
        i for i, kind in classify_judge_dependence(items).items() if kind == JUDGE_DEPENDENT
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m strata.judgecheck.dependence")
    parser.add_argument("items", help="a JSONL items file (the shipped set's row shape)")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args(argv)
    items = load_items_unchecked(args.items)
    result = classify_judge_dependence(items)
    if args.json:
        json.dump(result, sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    counts: dict[str, int] = {}
    for item in items:
        kind = result[item.id]
        counts[kind] = counts.get(kind, 0) + 1
        print(f"{item.id}\t{item.suite}\t{kind}")
    print(f"# {counts}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
