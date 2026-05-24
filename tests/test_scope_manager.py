"""Tests for :mod:`strata.scope_manager`.

All unit tests use :mod:`unittest.mock` — no real Anthropic API calls are
made.  The optional integration test (marked ``pytest.mark.integration``) is
skipped unless ``STRATA_RUN_INTEGRATION=1`` is set in the environment.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from strata.record_store import Contribution, ContributorRef, Scope, Stratum
from strata.scope_manager import ScopeManager, ScopeManagerJudgment
from strata.summary_store import Directive, ScopeSummary

# ---------------------------------------------------------------------------
# Fixtures — shared domain objects
# ---------------------------------------------------------------------------

STRATUM = Stratum(id="L1", name="function", ordinal=1, created_at="2026-01-01T00:00:00+00:00")
SCOPE = Scope(
    id="g_abc123", name="architecture", stratum_id="L1", created_at="2026-01-01T00:00:00+00:00"
)

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

    assert tool_choice == {"type": "tool", "name": "submit_judgment"}


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
