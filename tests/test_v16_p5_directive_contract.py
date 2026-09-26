"""v1.16 P5 — the judge-facing half of the directive-target contract (ADR 0017 P5,
"directive consequences go upward"): a directive `acted_on` target narrows to
held/failed/decline (never the four-way context enum), for either a scope-held
directive or an operator directive. The engine-raise mechanism itself (§B) is a
separate, later piece — this only pins the render and parse contract.

Base fixtures (`tests/fixtures/judge_prompts_v1160/directive_target.txt`,
`tests/fixtures/judge_tools_v1160/directive_target_tool.json`) were captured at
release/v1.16.0 @ dc7c527, before any of this file's code changes landed — see
`tests/_judge_prompt_corpus_v1165_p5.py`'s own commit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strata.operator import OperatorItem
from strata.scope_manager import (
    ActedOnTarget,
    _judge_tool_for,
    _MalformedDisposition,
    _render_outcome_block,
    _resolve_acted_on_decision,
)
from tests._judge_prompt_corpus_v1160 import ACTED_ON_TARGET, capture_v1160_corpus
from tests._judge_prompt_corpus_v1165_p5 import (
    DIRECTIVE_ACTED_ON_TARGET,
    capture_v1165_p5_corpus,
)

_V1160_GOLDEN_DIR = Path(__file__).parent / "fixtures" / "judge_prompts_v1160"
_V1160_TOOL_GOLDEN_DIR = Path(__file__).parent / "fixtures" / "judge_tools_v1160"


def _golden_text(dir_path: Path, name: str) -> str:
    return (dir_path / f"{name}.txt").read_text(encoding="utf-8")


def _golden_json(dir_path: Path, name: str) -> dict:
    return json.loads((dir_path / f"{name}.json").read_text(encoding="utf-8"))


def test_context_target_render_is_untouched_by_p5() -> None:
    """The context-target path (decision="accept_as_context") never reaches the
    new directive branch. Compared against 1b's own already-pinned output, not the
    pre-1b base fixture on disk: 1b's worked-example fix landed and merged before
    P5 started, so that one line's change belongs to 1b, not to this contract."""
    from tests.test_v1160_1b_worked_example import _NEW_LINE, _OLD_LINE

    before = _golden_text(_V1160_GOLDEN_DIR, "acted_on_target")
    after = capture_v1160_corpus()["acted_on_target"]
    assert after == before.replace(_OLD_LINE, _NEW_LINE)


def test_context_target_tool_still_offers_the_four_way_enum() -> None:
    tool = _judge_tool_for(ACTED_ON_TARGET)
    assert tool["input_schema"]["properties"]["decision"]["enum"] == [
        "held",
        "failed_corrected",
        "failed_superseded",
        "decline",
    ]


def test_directive_target_render_differs_from_base_and_drops_the_four_way_language() -> None:
    """The base render (pre-P5) offered failed_corrected/failed_superseded and no
    'does not own this directive' sentence; the new render must not."""
    base = _golden_text(_V1160_GOLDEN_DIR, "directive_target")
    after = capture_v1165_p5_corpus()["directive_target"]
    assert "failed_corrected" in base
    assert "failed_superseded" in base
    assert after != base
    assert "failed_corrected" not in after
    assert "failed_superseded" not in after
    assert "does not own this directive" in after
    assert "held" in after and "failed" in after and "decline" in after


def test_directive_target_tool_enum_is_three_way_and_base_was_four_way() -> None:
    base_tool = _golden_json(_V1160_TOOL_GOLDEN_DIR, "directive_target_tool")
    assert base_tool["input_schema"]["properties"]["decision"]["enum"] == [
        "held",
        "failed_corrected",
        "failed_superseded",
        "decline",
    ]
    tool = _judge_tool_for(DIRECTIVE_ACTED_ON_TARGET)
    assert tool["input_schema"]["properties"]["decision"]["enum"] == ["held", "failed", "decline"]


def test_resolve_acted_on_decision_rejects_the_wrong_enum_for_each_target_kind() -> None:
    """A directive target must never accept failed_corrected/failed_superseded, and
    a context target must never silently accept the bare `failed` value — each
    fails closed rather than being coerced onto its neighbor's meaning."""
    with pytest.raises(_MalformedDisposition):
        _resolve_acted_on_decision("failed_corrected", is_directive=True)
    with pytest.raises(_MalformedDisposition):
        _resolve_acted_on_decision("failed", is_directive=False)
    assert _resolve_acted_on_decision("held", is_directive=True) == ("accept_as_context", "held")
    assert _resolve_acted_on_decision("failed", is_directive=True) == (
        "accept_as_context",
        "failed",
    )
    assert _resolve_acted_on_decision("decline", is_directive=True) == ("decline", "decline")


def test_operator_directive_target_renders_with_no_contributor_line() -> None:
    """No scope-held contribution backs an operator directive target — the
    rendered block must not fabricate a contributor for it."""
    operator_item = OperatorItem(
        id="op_p5_test",
        kind="directive",
        content="Never deploy on a Friday.",
        subject="deploy-window",
        created_at="2026-09-01T09:00:00+00:00",
    )
    target = ActedOnTarget(contribution=None, decision=None, operator_item=operator_item)
    assert target.is_directive
    assert target.target_id == "op_p5_test"
    assert target.target_content == "Never deploy on a Friday."
    block = _render_outcome_block(target)
    assert "issuer: operator" in block
    assert "contributor:" not in block
    assert "Never deploy on a Friday." in block
    tool = _judge_tool_for(target)
    assert tool["input_schema"]["properties"]["decision"]["enum"] == ["held", "failed", "decline"]
