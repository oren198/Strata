"""Tests for :mod:`strata.scope_manager`.

All unit tests use :mod:`unittest.mock` — no real Anthropic API calls are
made.  The optional integration test (marked ``pytest.mark.integration``) is
skipped unless ``STRATA_RUN_INTEGRATION=1`` is set in the environment.

Decision 2 tests (parent summary in user message):
- Test 11: parent summary renders under "PARENT SCOPE SUMMARY (inherited)" header.
- Test 12: parent_summary=None (L0 root) — header is omitted entirely.
- Test 13: parent directive text appears in user message when parent provided.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from strata.fleet_config import Scope, Stratum
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import ScopeManager, ScopeManagerJudgment, _summary_word_count
from strata.summary_store import Directive, ScopeSummary

# ---------------------------------------------------------------------------
# Fixtures — shared domain objects
# ---------------------------------------------------------------------------

STRATUM = Stratum(id="L1", name="function", ordinal=1)
SCOPE = Scope(id="g_abc123", name="architecture", stratum_id="L1")

CONTRIBUTOR = ContributorRef(
    scope_id="g_def456",
    skill="code-writer",
    session_id="sess_001",
    ts="2026-05-01T10:00:00+00:00",
)

NEW_CONTRIBUTION = Contribution(
    id="c_001abc",
    scope_id=SCOPE.id,
    content="All new modules must include type annotations.",
    proposed_classification="directive",
    subject="type-annotations",
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-05-01T10:00:00+00:00",
)

EXISTING_DIRECTIVE = Directive(
    id="c_old001",
    content="Use snake_case for all identifiers.",
    subject="naming",
    source_scope_id=SCOPE.id,
    source_skill="architect",
    created_at="2026-04-01T09:00:00+00:00",
)

CURRENT_SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[EXISTING_DIRECTIVE],
    context="The architecture team favours minimal abstractions.",
    updated_at="2026-04-01T09:00:00+00:00",
)

RECENT_CONTRIBUTION = Contribution(
    id="c_prev01",
    scope_id=SCOPE.id,
    content="Previous observation about code style.",
    proposed_classification="context",
    subject=None,
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-04-15T08:00:00+00:00",
)

# ---------------------------------------------------------------------------
# Helper — build a fake Anthropic response carrying one tool_use block
# ---------------------------------------------------------------------------


def _fake_response(tool_input: dict) -> MagicMock:
    """Return a mock Anthropic Message-like object with one tool_use block."""
    block = MagicMock()
    block.type = "tool_use"
    block.input = tool_input

    response = MagicMock()
    response.content = [block]
    return response


def _accept_directive_input() -> dict:
    return {
        "decision": "accept_as_directive",
        "reasoning": "The contribution establishes a clear, enforceable coding standard.",
        "new_summary": {
            "directives": [
                {
                    "id": EXISTING_DIRECTIVE.id,
                    "content": EXISTING_DIRECTIVE.content,
                    "subject": EXISTING_DIRECTIVE.subject,
                    "source_scope_id": EXISTING_DIRECTIVE.source_scope_id,
                    "source_skill": EXISTING_DIRECTIVE.source_skill,
                    "created_at": EXISTING_DIRECTIVE.created_at,
                },
                {
                    "id": NEW_CONTRIBUTION.id,
                    "content": NEW_CONTRIBUTION.content,
                    "subject": NEW_CONTRIBUTION.subject,
                    "source_scope_id": SCOPE.id,
                    "source_skill": CONTRIBUTOR.skill,
                    "created_at": NEW_CONTRIBUTION.created_at,
                },
            ],
            "context": "The architecture team favours minimal abstractions with full type safety.",
        },
    }


def _accept_context_input() -> dict:
    return {
        "decision": "accept_as_context",
        "reasoning": "The contribution is informative but not binding.",
        "new_summary": {
            "directives": [
                {
                    "id": EXISTING_DIRECTIVE.id,
                    "content": EXISTING_DIRECTIVE.content,
                    "subject": EXISTING_DIRECTIVE.subject,
                    "source_scope_id": EXISTING_DIRECTIVE.source_scope_id,
                    "source_skill": EXISTING_DIRECTIVE.source_skill,
                    "created_at": EXISTING_DIRECTIVE.created_at,
                }
            ],
            "context": "Updated context with new observations.",
        },
    }


def _decline_input() -> dict:
    return {
        "decision": "decline",
        "reasoning": "The contribution duplicates an existing directive.",
        "new_summary": None,
    }


# ---------------------------------------------------------------------------
# Helper — instantiate ScopeManager with mocked client
# ---------------------------------------------------------------------------


def _make_manager(tool_input: dict) -> tuple[ScopeManager, MagicMock]:
    """Return (manager, mock_client) with messages.create stubbed."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(tool_input)
    manager = ScopeManager(client=mock_client)
    return manager, mock_client


# ---------------------------------------------------------------------------
# Test 1: accept_as_directive — judgment parses correctly, scope_id matches
# ---------------------------------------------------------------------------


def test_accept_as_directive_parses_correctly() -> None:
    manager, _ = _make_manager(_accept_directive_input())

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[RECENT_CONTRIBUTION],
        new_contribution=NEW_CONTRIBUTION,
    )

    assert isinstance(judgment, ScopeManagerJudgment)
    assert judgment.decision == "accept_as_directive"
    assert judgment.reasoning
    assert judgment.new_summary is not None
    assert judgment.new_summary.scope_id == SCOPE.id
    assert len(judgment.new_summary.directives) == 2
    # The newly accepted contribution should appear as a directive
    directive_ids = [d.id for d in judgment.new_summary.directives]
    assert NEW_CONTRIBUTION.id in directive_ids


# ---------------------------------------------------------------------------
# Test 2: accept_as_context — judgment parses correctly
# ---------------------------------------------------------------------------


def test_accept_as_context_parses_correctly() -> None:
    manager, _ = _make_manager(_accept_context_input())

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    assert judgment.decision == "accept_as_context"
    assert judgment.new_summary is not None
    assert judgment.new_summary.scope_id == SCOPE.id
    assert judgment.new_summary.context == "Updated context with new observations."


# ---------------------------------------------------------------------------
# Test 3: decline — new_summary is None
# ---------------------------------------------------------------------------


def test_decline_returns_no_summary() -> None:
    manager, _ = _make_manager(_decline_input())

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    assert judgment.decision == "decline"
    assert judgment.new_summary is None


# ---------------------------------------------------------------------------
# Test 4: decline with non-null new_summary → ValueError
# ---------------------------------------------------------------------------


def test_decline_with_nonnull_summary_raises() -> None:
    bad_input = {
        "decision": "decline",
        "reasoning": "Declining.",
        "new_summary": {
            "directives": [],
            "context": "Should not be here.",
        },
    }
    manager, _ = _make_manager(bad_input)

    with pytest.raises(ValueError, match="decline"):
        manager.judge(
            scope=SCOPE,
            stratum=STRATUM,
            current_summary=CURRENT_SUMMARY,
            recent_contributions=[],
            new_contribution=NEW_CONTRIBUTION,
        )


# ---------------------------------------------------------------------------
# Test 5: accept_as_directive with new_summary=None → ValueError
# ---------------------------------------------------------------------------


def test_accept_with_null_summary_raises() -> None:
    bad_input = {
        "decision": "accept_as_directive",
        "reasoning": "Accepting.",
        "new_summary": None,
    }
    manager, _ = _make_manager(bad_input)

    with pytest.raises(ValueError, match="accept_as_directive"):
        manager.judge(
            scope=SCOPE,
            stratum=STRATUM,
            current_summary=CURRENT_SUMMARY,
            recent_contributions=[],
            new_contribution=NEW_CONTRIBUTION,
        )


# ---------------------------------------------------------------------------
# Test 6: response with no tool_use block → ValueError
# ---------------------------------------------------------------------------


def test_missing_tool_use_block_raises() -> None:
    # Response with only a text block, no tool_use
    text_block = MagicMock()
    text_block.type = "text"

    response = MagicMock()
    response.content = [text_block]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = response
    manager = ScopeManager(client=mock_client)

    with pytest.raises(ValueError, match="tool_use"):
        manager.judge(
            scope=SCOPE,
            stratum=STRATUM,
            current_summary=CURRENT_SUMMARY,
            recent_contributions=[],
            new_contribution=NEW_CONTRIBUTION,
        )


# ---------------------------------------------------------------------------
# Test 7: user message includes scope name, contribution content, recent IDs
# ---------------------------------------------------------------------------


def test_user_message_contains_scope_and_contribution_details() -> None:
    manager, mock_client = _make_manager(_accept_directive_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[RECENT_CONTRIBUTION],
        new_contribution=NEW_CONTRIBUTION,
    )

    call_kwargs = mock_client.messages.create.call_args
    messages = call_kwargs.kwargs["messages"]
    user_message_content = messages[0]["content"]

    assert SCOPE.name in user_message_content
    assert NEW_CONTRIBUTION.content in user_message_content
    assert RECENT_CONTRIBUTION.id in user_message_content


# ---------------------------------------------------------------------------
# Test 8: current_summary=None → user message contains sentinel text
# ---------------------------------------------------------------------------


def test_no_current_summary_produces_sentinel_in_message() -> None:
    manager, mock_client = _make_manager(_accept_directive_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=None,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    call_kwargs = mock_client.messages.create.call_args
    messages = call_kwargs.kwargs["messages"]
    user_message_content = messages[0]["content"]

    assert "(this scope has no summary yet)" in user_message_content


# ---------------------------------------------------------------------------
# Test 9: system prompt and tool definition carry cache_control
# ---------------------------------------------------------------------------


def test_cache_control_applied_to_system_and_tools() -> None:
    manager, mock_client = _make_manager(_accept_directive_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    call_kwargs = mock_client.messages.create.call_args
    system_blocks = call_kwargs.kwargs["system"]
    tools = call_kwargs.kwargs["tools"]

    # System prompt has cache_control
    assert any(block.get("cache_control") == {"type": "ephemeral"} for block in system_blocks), (
        "System prompt should have cache_control={'type': 'ephemeral'}"
    )

    # Tool definition has cache_control
    assert any(tool.get("cache_control") == {"type": "ephemeral"} for tool in tools), (
        "Tool definition should have cache_control={'type': 'ephemeral'}"
    )


# ---------------------------------------------------------------------------
# Test 10: tool_choice forces submit_judgment
# ---------------------------------------------------------------------------


def test_tool_choice_forces_submit_judgment() -> None:
    manager, mock_client = _make_manager(_accept_directive_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    call_kwargs = mock_client.messages.create.call_args
    tool_choice = call_kwargs.kwargs["tool_choice"]

    assert tool_choice["type"] == "tool"
    assert tool_choice["name"] == "submit_judgment"
    assert tool_choice["disable_parallel_tool_use"] is True


# ---------------------------------------------------------------------------
# Decision 2 fixtures — parent scope / parent summary
# ---------------------------------------------------------------------------

PARENT_STRATUM = Stratum(id="L0", name="executive", ordinal=0)
PARENT_SCOPE = Scope(id="g_exec", name="Executive", stratum_id="L0")

PARENT_DIRECTIVE = Directive(
    id="c_parent01",
    content="All sub-teams must adhere to the company security policy.",
    subject="security-policy",
    source_scope_id=PARENT_SCOPE.id,
    source_skill="scope-manager",
    created_at="2026-01-01T00:00:00+00:00",
)

PARENT_SUMMARY = ScopeSummary(
    scope_id=PARENT_SCOPE.id,
    directives=[PARENT_DIRECTIVE],
    context="The executive context sets overall fleet direction.",
    updated_at="2026-01-01T00:00:00+00:00",
)


# ---------------------------------------------------------------------------
# Test 11: parent summary renders under PARENT SCOPE SUMMARY (inherited) header
# ---------------------------------------------------------------------------


def test_parent_summary_renders_under_inherited_header() -> None:
    """parent_summary renders into the user message with the correct section label."""
    manager, mock_client = _make_manager(_accept_directive_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        parent_summary=PARENT_SUMMARY,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    call_kwargs = mock_client.messages.create.call_args
    messages = call_kwargs.kwargs["messages"]
    user_message_content = messages[0]["content"]

    assert "PARENT SCOPE SUMMARY (inherited)" in user_message_content
    assert PARENT_SCOPE.id in user_message_content


# ---------------------------------------------------------------------------
# Test 12: parent_summary=None (L0 root) — inherited header is absent
# ---------------------------------------------------------------------------


def test_no_parent_summary_omits_inherited_header() -> None:
    """When parent_summary=None (root scope), the PARENT SCOPE SUMMARY section must be absent."""
    manager, mock_client = _make_manager(_accept_directive_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        parent_summary=None,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    call_kwargs = mock_client.messages.create.call_args
    messages = call_kwargs.kwargs["messages"]
    user_message_content = messages[0]["content"]

    assert "PARENT SCOPE SUMMARY (inherited)" not in user_message_content


# ---------------------------------------------------------------------------
# Test 13: parent directive text appears in user message
# ---------------------------------------------------------------------------


def test_parent_directive_content_in_user_message() -> None:
    """The parent's directive content must appear in the user message for the manager."""
    manager, mock_client = _make_manager(_accept_directive_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        parent_summary=PARENT_SUMMARY,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    call_kwargs = mock_client.messages.create.call_args
    messages = call_kwargs.kwargs["messages"]
    user_message_content = messages[0]["content"]

    assert PARENT_DIRECTIVE.content in user_message_content


# ---------------------------------------------------------------------------
# Issue #63 fixtures — overflow re-ask
# ---------------------------------------------------------------------------


def _summary_input_with_context(context: str) -> dict:
    """An accept_as_directive payload whose summary context is *context*."""
    return {
        "decision": "accept_as_directive",
        "reasoning": "The contribution establishes a clear, enforceable coding standard.",
        "new_summary": {
            "directives": [
                {
                    "id": EXISTING_DIRECTIVE.id,
                    "content": EXISTING_DIRECTIVE.content,
                    "subject": EXISTING_DIRECTIVE.subject,
                    "source_scope_id": EXISTING_DIRECTIVE.source_scope_id,
                    "source_skill": EXISTING_DIRECTIVE.source_skill,
                    "created_at": EXISTING_DIRECTIVE.created_at,
                },
            ],
            "context": context,
        },
    }


# ---------------------------------------------------------------------------
# Test 14: within-budget first response — exactly one API call
# ---------------------------------------------------------------------------


def test_within_budget_makes_exactly_one_call() -> None:
    manager, mock_client = _make_manager(_summary_input_with_context("Short context."))

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        summary_max_words=500,
    )

    assert mock_client.messages.create.call_count == 1
    assert judgment.decision == "accept_as_directive"


# ---------------------------------------------------------------------------
# Test 15: over-budget first response — exactly two calls, overflow re-ask,
# and the returned judgment is the second response's.
# ---------------------------------------------------------------------------


def test_over_budget_triggers_one_corrective_retry() -> None:
    over_budget_context = " ".join(f"word{i}" for i in range(20))
    first_input = _summary_input_with_context(over_budget_context)

    second_context = "Condensed context."
    second_input = _summary_input_with_context(second_context)

    first_response = _fake_response(first_input)
    second_response = _fake_response(second_input)
    first_tool_use_id = first_response.content[0].id

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [first_response, second_response]
    manager = ScopeManager(client=mock_client)

    small_budget = 5  # first response's ~20-word context blows this budget

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        summary_max_words=small_budget,
    )

    assert mock_client.messages.create.call_count == 2

    first_call_kwargs = mock_client.messages.create.call_args_list[0].kwargs
    second_call_kwargs = mock_client.messages.create.call_args_list[1].kwargs
    second_messages = second_call_kwargs["messages"]

    # Original user turn preserved verbatim.
    assert second_messages[0] == first_call_kwargs["messages"][0]

    # Assistant turn containing the first response's content blocks.
    assert second_messages[1] == {"role": "assistant", "content": first_response.content}

    # User turn with a tool_result for the first tool_use id + overflow text.
    followup_user_turn = second_messages[2]
    assert followup_user_turn["role"] == "user"
    tool_result_blocks = [b for b in followup_user_turn["content"] if b["type"] == "tool_result"]
    assert len(tool_result_blocks) == 1
    assert tool_result_blocks[0]["tool_use_id"] == first_tool_use_id

    text_blocks = [b for b in followup_user_turn["content"] if b["type"] == "text"]
    assert len(text_blocks) == 1
    assert "BUDGET" in text_blocks[0]["text"]
    assert "VERBATIM" in text_blocks[0]["text"]
    assert str(small_budget) in text_blocks[0]["text"]

    # The returned judgment reflects the SECOND response, not the first.
    assert judgment.new_summary is not None
    assert judgment.new_summary.context == second_context


# ---------------------------------------------------------------------------
# Test 16: _summary_word_count counts context + directive content
# ---------------------------------------------------------------------------


def test_summary_word_count_counts_context_and_directives() -> None:
    summary = ScopeSummary(
        scope_id=SCOPE.id,
        directives=[
            Directive(
                id="d1",
                content="one two three",
                source_scope_id=SCOPE.id,
                source_skill="architect",
                created_at="2026-01-01T00:00:00+00:00",
            ),
            Directive(
                id="d2",
                content="four five",
                source_scope_id=SCOPE.id,
                source_skill="architect",
                created_at="2026-01-01T00:00:00+00:00",
            ),
        ],
        context="six seven eight nine",
        updated_at="2026-01-01T00:00:00+00:00",
    )

    # 3 (d1) + 2 (d2) + 4 (context) = 9
    assert _summary_word_count(summary) == 9


# ---------------------------------------------------------------------------
# Integration test (optional — requires STRATA_RUN_INTEGRATION=1)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("STRATA_RUN_INTEGRATION") != "1",
    reason="Set STRATA_RUN_INTEGRATION=1 to run integration tests.",
)
def test_integration_real_api() -> None:
    """Hit the real Anthropic API with a worked example.

    Asserts response *shape* only — LLM judgments are not deterministic.
    """
    anthropic = pytest.importorskip("anthropic")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        pytest.skip("ANTHROPIC_API_KEY not set.")

    client = anthropic.Anthropic(api_key=api_key)
    manager = ScopeManager(client=client)

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=None,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    assert isinstance(judgment, ScopeManagerJudgment)
    assert judgment.decision in ("accept_as_directive", "accept_as_context", "decline")
    assert isinstance(judgment.reasoning, str) and judgment.reasoning

    if judgment.decision != "decline":
        assert judgment.new_summary is not None
        assert judgment.new_summary.scope_id == SCOPE.id
        assert isinstance(judgment.new_summary.directives, list)
        assert isinstance(judgment.new_summary.context, str)
    else:
        assert judgment.new_summary is None


# ---------------------------------------------------------------------------
# Retry robustness (release-review findings): the FIRST judgment is
# authoritative — the corrective call may only replace its summary.
# ---------------------------------------------------------------------------


def test_retry_decline_keeps_first_judgment() -> None:
    """A formatting re-ask must never flip an accept into a decline."""
    over_budget_context = " ".join(f"word{i}" for i in range(20))
    first_input = _summary_input_with_context(over_budget_context)
    decline_input = {"decision": "decline", "reasoning": "changed my mind", "new_summary": None}

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(first_input),
        _fake_response(decline_input),
    ]
    manager = ScopeManager(client=mock_client)

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        summary_max_words=5,
    )

    assert mock_client.messages.create.call_count == 2
    assert judgment.decision != "decline", (
        "the retry's verdict reversal must be discarded — first judgment is authoritative"
    )
    assert judgment.new_summary is not None
    assert over_budget_context in judgment.new_summary.context


def test_retry_parse_failure_keeps_first_judgment() -> None:
    """A malformed second response must not destroy the valid first judgment."""
    over_budget_context = " ".join(f"word{i}" for i in range(20))
    first_input = _summary_input_with_context(over_budget_context)

    broken_response = MagicMock()
    broken_response.content = []  # no tool_use block at all (e.g. truncation)

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(first_input),
        broken_response,
    ]
    manager = ScopeManager(client=mock_client)

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        summary_max_words=5,
    )

    assert mock_client.messages.create.call_count == 2
    assert judgment.new_summary is not None, (
        "parse failure on the retry must fall back to the first judgment"
    )
    assert over_budget_context in judgment.new_summary.context


def test_retry_api_error_keeps_first_judgment() -> None:
    """A transient API failure on the retry falls back to the first judgment."""
    over_budget_context = " ".join(f"word{i}" for i in range(20))
    first_input = _summary_input_with_context(over_budget_context)

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(first_input),
        RuntimeError("api unavailable"),
    ]
    manager = ScopeManager(client=mock_client)

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        summary_max_words=5,
    )

    assert judgment.new_summary is not None
    assert over_budget_context in judgment.new_summary.context


def test_tool_choice_disables_parallel_tool_use() -> None:
    """Both calls must pin exactly one tool_use block per response."""
    manager, mock_client = _make_manager(_summary_input_with_context("Short."))
    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )
    tool_choice = mock_client.messages.create.call_args.kwargs["tool_choice"]
    assert tool_choice.get("disable_parallel_tool_use") is True
