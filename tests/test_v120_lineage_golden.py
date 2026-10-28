"""v1.20 item 7(d): the exact bytes of the CONTEXT LINES block, on the single and batch paths.

The block is spliced into the user message by `judge()` / `judge_batch()` (the builders' source
is pinned elsewhere and must not move), so nothing else pins what an over-threshold prompt says.
These tests do: the wording of the block, where it sits, and that a call below the threshold
carries nothing. A change to the wording must change a golden here on purpose."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from strata.lineage import LineageLineView
from strata.scope_manager import ScopeManager
from strata.summary_store import ScopeSummary
from tests.test_scope_manager import (
    NEW_CONTRIBUTION,
    SCOPE,
    SECOND_CONTRIBUTION,
    STRATUM,
    _batch_input,
    _fake_response,
)

# the rule and the instruction, byte for byte
_RULE = (
    "CONTEXT LINES — this context is near its budget, so a condensation is likely.\n"
    "Under budget pressure drop, in order: context superseded by newer context; then "
    "unexamined context, oldest first; then examined context, oldest first.\n"
    "Report `context_lineage`: for every line of your new_context, the contribution ids "
    "it rests on (several if you merge notes; none if it rests on no admitted note).\n"
)
GOLDEN_WITHOUT_LINEAGE = _RULE + "\n"
GOLDEN_WITH_LINEAGE = (
    _RULE
    + "[1] rests on c_aaa (2026-10-01, unexamined), c_bbb (2026-10-02, examined)\n"
    + "[2] rests on no admitted note\n"
    + "\n"
)

CONTEXT = "first line of context\nsecond line of context"  # 8 words
OVER, UNDER = 10, 100  # 8 words is >= half of 10, and below half of 100
VIEW = [
    LineageLineView(
        1, (("c_aaa", "2026-10-01", "unexamined"), ("c_bbb", "2026-10-02", "examined"))
    ),
    LineageLineView(2, ()),
]
SUMMARY = ScopeSummary(
    scope_id=SCOPE.id, directives=[], context=CONTEXT, updated_at="2026-10-01T00:00:00+00:00"
)
MARKER = "CURRENT SUMMARY\n---\n"


def _single(budget: int, view) -> dict:  # noqa: ANN001
    client = MagicMock()
    client.messages.create.return_value = _fake_response(
        {
            "decision": "accept_as_context",
            "reasoning": "A note.",
            "directive_ops": [],
            "new_context": None,
        }
    )
    ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=SUMMARY,
        recent_contributions=[],
        new_contribution=replace(NEW_CONTRIBUTION, proposed_classification="context"),
        summary_max_words=budget,
        lineage_view=view,
    )
    return client.messages.create.call_args_list[0].kwargs


def _batch(budget: int, view) -> dict:  # noqa: ANN001
    client = MagicMock()
    client.messages.create.return_value = _fake_response(
        _batch_input(
            verdicts=[
                {
                    "contribution_id": NEW_CONTRIBUTION.id,
                    "decision": "decline",
                    "reasoning": "not for this scope",
                },
                {
                    "contribution_id": SECOND_CONTRIBUTION.id,
                    "decision": "decline",
                    "reasoning": "not for this scope",
                },
            ],
            directive_ops=[],
            new_context=None,
        )
    )
    ScopeManager(client=client).judge_batch(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=SUMMARY,
        recent_contributions=[],
        new_contributions=[NEW_CONTRIBUTION, SECOND_CONTRIBUTION],
        summary_max_words=budget,
        lineage_view=view,
    )
    return client.messages.create.call_args_list[0].kwargs


@pytest.mark.parametrize("build", [_single, _batch], ids=["single", "batch"])
class TestTheOverThresholdMessage:
    def test_the_block_with_stored_lineage_is_exactly_this_and_sits_before_the_summary(
        self, build
    ) -> None:
        message = build(OVER, VIEW)["messages"][0]["content"]
        assert message.count("CONTEXT LINES") == 1
        assert GOLDEN_WITH_LINEAGE + MARKER in message

    def test_the_block_without_stored_lineage_is_exactly_the_rule_alone(self, build) -> None:
        message = build(OVER, None)["messages"][0]["content"]
        assert GOLDEN_WITHOUT_LINEAGE + MARKER in message
        assert "[1]" not in message.split(MARKER)[0].rsplit("CONTEXT LINES", 1)[1]

    def test_below_the_threshold_there_is_no_block_and_the_message_is_unchanged(
        self, build
    ) -> None:
        under = build(UNDER, VIEW)["messages"][0]["content"]
        assert "CONTEXT LINES" not in under
        over = build(OVER, VIEW)["messages"][0]["content"]
        # taking the block out of the over-threshold message leaves the under-threshold one,
        # except for the budget line, which differs only in its number
        stripped = over.replace(GOLDEN_WITH_LINEAGE, "")
        assert stripped.replace(f"at most {OVER} words", f"at most {UNDER} words") == under

    def test_the_tool_variant_is_offered_only_with_the_block(self, build) -> None:
        def properties(kwargs: dict) -> dict:
            return kwargs["tools"][0]["input_schema"]["properties"]

        assert "context_lineage" in properties(build(OVER, VIEW))
        assert "context_lineage" in properties(build(OVER, None))
        assert "context_lineage" not in properties(build(UNDER, VIEW))
