"""Helpers for the judge-check tests: results that follow the goldens, and small mutations."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from strata.judgecheck import Item, ItemResult


def _ops_for(item: Item) -> tuple[dict[str, Any], ...]:
    golden = item.golden
    ops: list[dict[str, Any]] = []
    expected = golden.get("expected_admit_op")
    if expected == "append":
        ops.append(
            {"op": "append", "id": None, "content": None, "subject": None, "supersedes": None}
        )
    elif expected == "publish":
        ops.append(
            {"op": "publish", "id": None, "content": "x", "subject": None, "supersedes": None}
        )
    for target in golden.get("expected_supersedes", []):
        ops.append(
            {"op": "supersede", "id": target, "content": None, "subject": None, "supersedes": None}
        )
    for target in golden.get("expected_retires", []):
        ops.append(
            {"op": "retire", "id": target, "content": None, "subject": None, "supersedes": None}
        )
    return tuple(ops)


def golden_result(item: Item, rep: int = 0, *, label: str = "cand") -> ItemResult:
    """The result of a judge that gives exactly the golden answer for *item*."""
    golden = item.golden
    decision = golden["decision"]
    published: tuple[str, ...] = ()
    if item.suite == "p5":
        content = item.data["proposal"]["content"]
        if decision == "accept":
            departs = golden.get("departs_from")
            text = f"Departure from {departs} (g_x): {content}" if departs else content
            published = (text,)
        decision = "accept_as_context" if decision == "accept" else "decline"
    new_summary = None
    if item.suite == "j3":
        scenario = item.data["scenario"]
        directives = list((scenario.get("current_summary") or {}).get("directives", []))
        params = item.data.get("post_check_params", {})
        context = params.get("marker", "") or ""
        new_summary = {"directives": directives, "context": context}
    return ItemResult(
        item_id=item.id,
        suite=item.suite,
        rep=rep,
        judge_label=label,
        decision=decision,
        directive_ops=_ops_for(item),
        new_summary=new_summary,
        published_texts=published,
        calls=1,
    )


def with_decision(result: ItemResult, decision: str, **kw: Any) -> ItemResult:
    return replace(result, decision=decision, **kw)
