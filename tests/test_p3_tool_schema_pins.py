"""v1.15 P3 rev2: the tool SCHEMA a judge call sees is part of what the judge reads,
just as much as the rendered prompt text — the golden corpus in
tests/test_p1_judge_prompt_unchanged.py pins the text, this file pins the schema.

Fixtures were captured from fa5ef01 (release/v1.15.0's head immediately before P3
touched scope_manager.py), in their own commit, before any P3 code change (same
discipline as the prompt corpus's 3710c9c). An ordinary judge call — no acted_on — and
the batch/publication/bootstrap tools must stay byte-identical to that pre-P3 state
forever: the M1/#212 lesson is that a new field on EVERY call, even schema-only, even
unused, measurably degrades general judging.
"""

from __future__ import annotations

import json
from pathlib import Path

from strata.scope_manager import (
    BOOTSTRAP_JUDGE_TOOL,
    JUDGE_BATCH_TOOL,
    JUDGE_TOOL,
    PUBLICATION_JUDGE_TOOL,
    _judge_tool_for,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "judge_tools_v1150"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_judge_tool_constant_is_unchanged_from_pre_p3() -> None:
    assert _load("JUDGE_TOOL") == JUDGE_TOOL


def test_judge_tool_for_with_no_acted_on_target_is_unchanged_from_pre_p3() -> None:
    # This is the tool dict actually passed to the judge for an ordinary contribution
    # — the exact call site scope_manager.judge() uses when acted_on_target is None.
    assert _judge_tool_for(None) == _load("JUDGE_TOOL")


def test_judge_tool_for_with_acted_on_target_widens_decision_only() -> None:
    # ADR 0017 P3 rev 3 (ruling c′): no sidecar field — the acted_on variant differs
    # from the base tool in exactly one property, `decision` (its enum widens to the
    # four dispositions); every other property, and the tool's name/description/
    # required list, stay byte-identical to the base.
    from strata.record_store import ContributorRef
    from strata.scope_manager import ActedOnTarget, Contribution

    target = ActedOnTarget(
        contribution=Contribution(
            id="c1",
            scope_id="g_x",
            content="x",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=ContributorRef(
                scope_id="g_x", skill=None, session_id="s1", ts="2026-01-01T00:00:00Z"
            ),
            created_at="2026-01-01T00:00:00Z",
        ),
        decision="accept_as_context",
    )
    variant = _judge_tool_for(target)
    base = _load("JUDGE_TOOL")

    assert set(variant["input_schema"]["properties"]) == set(base["input_schema"]["properties"])
    without_decision = {
        k: v for k, v in variant["input_schema"]["properties"].items() if k != "decision"
    }
    base_without_decision = {
        k: v for k, v in base["input_schema"]["properties"].items() if k != "decision"
    }
    assert without_decision == base_without_decision
    variant_decision = variant["input_schema"]["properties"]["decision"]
    base_decision = base["input_schema"]["properties"]["decision"]
    assert variant_decision != base_decision
    assert variant_decision["enum"] == ["held", "failed_corrected", "failed_superseded", "decline"]
    assert variant["input_schema"]["required"] == base["input_schema"]["required"]
    assert variant["name"] == base["name"]
    assert variant["description"] == base["description"]

    # Deep-copied, not mutated in place: the shared JUDGE_TOOL constant is untouched.
    assert base_decision["enum"] == ["accept_as_directive", "accept_as_context", "decline"]


def test_judge_batch_tool_is_unchanged_from_pre_p3() -> None:
    assert _load("JUDGE_BATCH_TOOL") == JUDGE_BATCH_TOOL


def test_publication_judge_tool_is_unchanged_from_pre_p3() -> None:
    assert _load("PUBLICATION_JUDGE_TOOL") == PUBLICATION_JUDGE_TOOL


def test_bootstrap_judge_tool_is_unchanged_from_pre_p3() -> None:
    assert _load("BOOTSTRAP_JUDGE_TOOL") == BOOTSTRAP_JUDGE_TOOL
