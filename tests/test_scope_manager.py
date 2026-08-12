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

import json
import os
from unittest.mock import MagicMock

import pytest

from strata.fleet_config import EntitlementView, Scope, Stratum
from strata.operator import OperatorItem
from strata.publication import PublishedItem
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import (
    _BOOTSTRAP_SYSTEM_PROMPT,
    _PUBLICATION_SYSTEM_PROMPT,
    _SYSTEM_PROMPT,
    BOOTSTRAP_JUDGE_TOOL,
    JUDGE_TOOL,
    PUBLICATION_JUDGE_TOOL,
    BootstrapJudgment,
    PublicationJudgment,
    ScopeManager,
    ScopeManagerJudgment,
    _build_user_message,
    _render_contributor,
    _render_recent_contributions,
    _summary_word_count,
)
from strata.summary_store import Directive, ScopeSummary, _render_summary

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


# ---------------------------------------------------------------------------
# Issue #121 — skill optional in provenance rendering + parse
# ---------------------------------------------------------------------------


def _skilless_contributor() -> ContributorRef:
    return ContributorRef(
        scope_id="g_def456",
        skill=None,
        session_id="sess_001",
        ts="2026-05-01T10:00:00+00:00",
    )


def test_render_contributor_omits_skill_when_absent() -> None:
    """A skill-less contributor renders the scope alone, never ``skill=None``."""
    rendered = _render_contributor(_skilless_contributor())
    assert "skill=" not in rendered
    assert "None" not in rendered
    assert "scope=g_def456" in rendered


def test_render_contributor_includes_skill_when_present() -> None:
    """A skill-bearing contributor keeps the ``skill=`` field (no regression)."""
    rendered = _render_contributor(CONTRIBUTOR)
    assert "skill=code-writer" in rendered
    assert "scope=g_def456" in rendered


def test_render_recent_contributions_skilless_shows_scope_alone() -> None:
    """The recent-contributions slice renders ``by <scope>`` for a skill-less item."""
    contribution = Contribution(
        id="c_ns",
        scope_id=SCOPE.id,
        content="observation",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=_skilless_contributor(),
        created_at="2026-05-01T10:00:00+00:00",
    )
    rendered = _render_recent_contributions([contribution])
    assert "by g_def456" in rendered
    assert "@" not in rendered
    assert "None" not in rendered


def test_build_user_message_skilless_contributor_has_no_none() -> None:
    """The full judge user message never renders a ``None`` skill placeholder."""
    contribution = Contribution(
        id="c_msg",
        scope_id=SCOPE.id,
        content="a skill-less contribution",
        proposed_classification="directive",
        subject="topic",
        supersedes=None,
        contributor=_skilless_contributor(),
        created_at="2026-05-01T10:00:00+00:00",
    )
    message = _build_user_message(
        scope=SCOPE,
        stratum=STRATUM,
        parent_summary=None,
        current_summary=None,
        recent_contributions=[],
        new_contribution=contribution,
    )
    # The contributor line shows the scope, not a skill=None token.
    assert "skill=None" not in message
    assert "scope=g_def456" in message


def test_append_op_builds_directive_without_source_skill() -> None:
    """A skill-less contribution appends a directive whose source_skill is None (issue #121).

    The engine builds the directive row from the contribution itself
    (ADR 0011 D1), so the optional skill travels from the contributor's
    provenance rather than from anything the judge writes.
    """
    contribution = Contribution(
        id="c_ns2",
        scope_id=SCOPE.id,
        content="A skill-less directive.",
        proposed_classification="directive",
        subject="topic",
        supersedes=None,
        contributor=_skilless_contributor(),
        created_at="2026-05-01T10:00:00+00:00",
    )
    tool_input = {
        "decision": "accept_as_directive",
        "reasoning": "accepted",
        "directive_ops": [{"op": "append"}],
        "new_context": "",
    }
    judgment = ScopeManager._parse_judgment(
        scope=SCOPE,
        tool_use_block=_fake_response(tool_input).content[0],
        current_summary=None,
        new_contribution=contribution,
    )
    assert judgment.new_summary is not None
    assert judgment.new_summary.directives[0].source_skill is None
    assert judgment.new_summary.directives[0].content == contribution.content


def test_judge_tool_no_longer_carries_new_summary() -> None:
    """ADR 0011 D1: submit_judgment returns an amendment, never a rewritten summary."""
    schema = JUDGE_TOOL["input_schema"]
    assert "new_summary" not in schema["properties"]
    assert "new_summary" not in schema["required"]
    assert schema["required"] == ["decision", "reasoning", "directive_ops", "new_context"]


def test_judge_tool_directive_ops_schema_lists_the_four_ops() -> None:
    """The amendment's op vocabulary is exactly append/publish/supersede/retire."""
    ops_schema = JUDGE_TOOL["input_schema"]["properties"]["directive_ops"]
    assert ops_schema["type"] == ["array", "null"]
    item = ops_schema["items"]
    assert item["properties"]["op"]["enum"] == ["append", "publish", "supersede", "retire"]
    # Only `op` is structurally required: append carries no fields at all.
    assert item["required"] == ["op"]
    for field in ("content", "subject", "supersedes", "id"):
        assert item["properties"][field]["type"] == ["string", "null"]
    assert JUDGE_TOOL["input_schema"]["properties"]["new_context"]["type"] == ["string", "null"]


def _accept_directive_input() -> dict:
    return {
        "decision": "accept_as_directive",
        "reasoning": "The contribution establishes a clear, enforceable coding standard.",
        "directive_ops": [{"op": "append"}],
        "new_context": (
            "The architecture team favours minimal abstractions with full type safety."
        ),
    }


def _accept_context_input() -> dict:
    return {
        "decision": "accept_as_context",
        "reasoning": "The contribution is informative but not binding.",
        "directive_ops": [],
        "new_context": "Updated context with new observations.",
    }


def _decline_input() -> dict:
    return {
        "decision": "decline",
        "reasoning": "The contribution duplicates an existing directive.",
        "directive_ops": None,
        "new_context": None,
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
    # ...built from the contribution verbatim, not restated by the judge.
    appended = judgment.new_summary.directives[-1]
    assert appended.content == NEW_CONTRIBUTION.content
    assert appended.subject == NEW_CONTRIBUTION.subject
    assert appended.source_scope_id == CONTRIBUTOR.scope_id
    assert appended.source_skill == CONTRIBUTOR.skill
    # ...and the existing directive is carried across untouched.
    assert judgment.new_summary.directives[0] == EXISTING_DIRECTIVE


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
    # An amendment with no directive op leaves the directives list alone.
    assert judgment.new_summary.directives == [EXISTING_DIRECTIVE]


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
# Test 4: decline carrying an amendment → ValueError
# ---------------------------------------------------------------------------


def test_decline_with_new_context_raises() -> None:
    """A declined contribution must not amend the summary (ADR 0011 D1)."""
    bad_input = {
        "decision": "decline",
        "reasoning": "Declining.",
        "directive_ops": [],
        "new_context": "Should not be here.",
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


def test_decline_with_directive_ops_raises() -> None:
    """The same inconsistency check covers directive ops, not just context."""
    bad_input = {
        "decision": "decline",
        "reasoning": "Declining.",
        "directive_ops": [{"op": "retire", "id": EXISTING_DIRECTIVE.id}],
        "new_context": None,
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
# Test 5: an accept always yields a summary, even on an empty amendment
# ---------------------------------------------------------------------------


def test_accept_with_empty_amendment_still_produces_a_summary() -> None:
    """An accept never comes back without a summary to write.

    Under ADR 0011 D1 the judge can accept while amending nothing; the engine
    still produces the amended summary (here: the current one, unchanged), so
    the caller always has something to write and a version to bump.
    """
    empty_amendment = {
        "decision": "accept_as_context",
        "reasoning": "Accepting; nothing to change.",
        "directive_ops": None,
        "new_context": None,
    }
    manager, _ = _make_manager(empty_amendment)

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    assert judgment.new_summary is not None
    assert judgment.new_summary.directives == CURRENT_SUMMARY.directives
    assert judgment.new_summary.context == CURRENT_SUMMARY.context


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
    """An accept_as_directive payload whose amended context is *context*."""
    return {
        "decision": "accept_as_directive",
        "reasoning": "The contribution establishes a clear, enforceable coding standard.",
        "directive_ops": [],
        "new_context": context,
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
    assert str(small_budget) in text_blocks[0]["text"]
    # ADR 0011 D1: the corrective asks for the two levers that now exist —
    # retire ops and a shorter context — not for a rewritten summary.
    assert "`retire`" in text_blocks[0]["text"]
    assert "`new_context`" in text_blocks[0]["text"]
    assert "do not restate them" in text_blocks[0]["text"]

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
    decline_input = {
        "decision": "decline",
        "reasoning": "changed my mind",
        "directive_ops": None,
        "new_context": None,
    }

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


# ---------------------------------------------------------------------------
# Issue #113 — the amendment returned as a JSON-encoded string
# ---------------------------------------------------------------------------


def _tool_use_block(tool_input: dict) -> MagicMock:
    """A bare tool_use block for driving _parse_judgment directly."""
    return _fake_response(tool_input).content[0]


def _parse(tool_input: dict, *, current_summary: ScopeSummary | None = CURRENT_SUMMARY):
    """Drive _parse_judgment directly against this suite's fixtures."""
    return ScopeManager._parse_judgment(
        scope=SCOPE,
        tool_use_block=_tool_use_block(tool_input),
        current_summary=current_summary,
        new_contribution=NEW_CONTRIBUTION,
    )


def test_stringified_directive_ops_is_coerced() -> None:
    """directive_ops arriving as a JSON string is decoded, not crashed on.

    Test (a): the model stringified the whole ops list. The parse coerces it
    back and succeeds on the first call — no re-ask needed.
    """
    valid = _accept_directive_input()
    stringified_input = {
        **valid,
        "directive_ops": json.dumps(valid["directive_ops"]),
    }
    manager, mock_client = _make_manager(stringified_input)

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[RECENT_CONTRIBUTION],
        new_contribution=NEW_CONTRIBUTION,
    )

    assert mock_client.messages.create.call_count == 1
    assert judgment.decision == "accept_as_directive"
    assert judgment.new_summary is not None
    assert len(judgment.new_summary.directives) == 2
    assert NEW_CONTRIBUTION.id in [d.id for d in judgment.new_summary.directives]


def test_stringified_op_entry_is_coerced() -> None:
    """A single op entry arriving as a JSON string is decoded."""
    valid = _accept_directive_input()
    valid["directive_ops"] = [
        json.dumps({"op": "supersede", "id": EXISTING_DIRECTIVE.id}),
        {"op": "append"},
    ]

    judgment = _parse(valid)

    assert judgment.new_summary is not None
    assert [d.id for d in judgment.new_summary.directives] == [NEW_CONTRIBUTION.id]


def test_garbage_string_directive_ops_raises_clean_valueerror() -> None:
    """Test (b): an unparseable directive_ops string is a clean ValueError.

    The pre-fix bug was an ``AttributeError: 'str' object has no attribute
    'get'`` escaping from the parse path. The coercion must convert that into
    the specific, actionable ValueError instead.
    """
    bad_input = {
        "decision": "accept_as_directive",
        "reasoning": "The contribution is a clear standard.",
        "directive_ops": "I could not fit the ops into JSON, sorry.",
        "new_context": "",
    }

    with pytest.raises(ValueError, match="directive_ops as an unparseable string"):
        _parse(bad_input)


def test_garbage_string_directive_ops_not_attributeerror() -> None:
    """The failure mode is a ValueError, never the original AttributeError."""
    bad_input = {
        "decision": "accept_as_directive",
        "reasoning": "The contribution is a clear standard.",
        "directive_ops": "not json",
        "new_context": "",
    }

    try:
        _parse(bad_input)
    except ValueError:
        pass
    except AttributeError as exc:  # pragma: no cover - regression guard
        pytest.fail(f"parse leaked AttributeError instead of ValueError: {exc}")
    else:  # pragma: no cover - must raise
        pytest.fail("expected a ValueError for a garbage directive_ops string")


def test_first_parse_failure_triggers_one_corrective_reask() -> None:
    """Test (c): a stringified first payload triggers exactly one re-ask.

    First response returns directive_ops as a garbage string (unparseable);
    the manager sends one corrective follow-up echoing the error, and the
    second (well-formed) response parses successfully. Exactly two API calls.
    """
    garbage_input = {
        "decision": "accept_as_directive",
        "reasoning": "The contribution is a clear standard.",
        "directive_ops": "oops, this should have been a list",
        "new_context": "",
    }
    good_input = _accept_directive_input()

    first_response = _fake_response(garbage_input)
    second_response = _fake_response(good_input)
    first_tool_use_id = first_response.content[0].id

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [first_response, second_response]
    manager = ScopeManager(client=mock_client)

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    assert mock_client.messages.create.call_count == 2

    # The corrective turn echoes the first response and a tool_result for its
    # tool_use id, plus a text block naming the parse failure.
    second_messages = mock_client.messages.create.call_args_list[1].kwargs["messages"]
    assert second_messages[0] == mock_client.messages.create.call_args_list[0].kwargs["messages"][0]
    assert second_messages[1] == {"role": "assistant", "content": first_response.content}
    followup = second_messages[2]
    assert followup["role"] == "user"
    tool_results = [b for b in followup["content"] if b["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_use_id"] == first_tool_use_id
    text_blocks = [b for b in followup["content"] if b["type"] == "text"]
    assert len(text_blocks) == 1
    assert "could not be parsed" in text_blocks[0]["text"]

    # The second, well-formed response is the one that parsed.
    assert judgment.decision == "accept_as_directive"
    assert judgment.new_summary is not None
    assert len(judgment.new_summary.directives) == 2


def test_second_parse_failure_does_not_loop() -> None:
    """Test (c): a second parse failure propagates — never more than one retry."""
    garbage_input = {
        "decision": "accept_as_directive",
        "reasoning": "The contribution is a clear standard.",
        "directive_ops": "still not a list",
        "new_context": "",
    }

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(garbage_input),
        _fake_response(garbage_input),
    ]
    manager = ScopeManager(client=mock_client)

    with pytest.raises(ValueError, match="directive_ops as an unparseable string"):
        manager.judge(
            scope=SCOPE,
            stratum=STRATUM,
            current_summary=CURRENT_SUMMARY,
            recent_contributions=[],
            new_contribution=NEW_CONTRIBUTION,
        )

    # Exactly one retry: two calls total, no third attempt.
    assert mock_client.messages.create.call_count == 2


def test_parse_reask_then_overflow_reask_chain() -> None:
    """A parse re-ask and the overflow re-ask are independent single retries.

    First response is an unparseable directive_ops string (parse re-ask); the
    corrective response parses but is over budget (overflow re-ask); the third
    response fits. The overflow follow-up must build on the corrective turn,
    not the discarded first turn — three calls, final judgment is the third.
    """
    garbage_input = {
        "decision": "accept_as_directive",
        "reasoning": "The contribution is a clear standard.",
        "directive_ops": "not a list",
        "new_context": "",
    }
    over_budget_input = _summary_input_with_context(" ".join(f"word{i}" for i in range(20)))
    fitting_input = _summary_input_with_context("Condensed.")

    first_response = _fake_response(garbage_input)
    retry_response = _fake_response(over_budget_input)
    third_response = _fake_response(fitting_input)
    retry_tool_use_id = retry_response.content[0].id

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [first_response, retry_response, third_response]
    manager = ScopeManager(client=mock_client)

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        summary_max_words=5,
    )

    assert mock_client.messages.create.call_count == 3

    # The overflow follow-up (third call) chains onto the corrective turn: its
    # tool_result references the RETRY response's tool_use id, not the first.
    third_messages = mock_client.messages.create.call_args_list[2].kwargs["messages"]
    assert third_messages[-2] == {"role": "assistant", "content": retry_response.content}
    tool_results = [b for b in third_messages[-1]["content"] if b["type"] == "tool_result"]
    assert tool_results[0]["tool_use_id"] == retry_tool_use_id

    assert judgment.new_summary is not None
    assert judgment.new_summary.context == "Condensed."


# ---------------------------------------------------------------------------
# ADR 0006 D2 — entitlement signal + admission rule
# ---------------------------------------------------------------------------

PEER_SCOPE = Scope(id="g_peer1", name="Peer One", stratum_id="L1")
OTHER_SCOPE = Scope(id="g_other1", name="Other One", stratum_id="L1")
DESCENDANT_SCOPE = Scope(id="g_below1", name="Below One", stratum_id="L2")

ENTITLEMENT_VIEW = EntitlementView(
    chain=[SCOPE],
    descendants=[DESCENDANT_SCOPE],
    referenced_peers=[PEER_SCOPE],
    others=[OTHER_SCOPE],
)


def test_entitlement_block_present_with_correct_groups() -> None:
    """The ENTITLEMENT block lists chain/peer/other scopes under the right headers."""
    manager, mock_client = _make_manager(_accept_directive_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        entitlement=ENTITLEMENT_VIEW,
    )

    call_kwargs = mock_client.messages.create.call_args
    content = call_kwargs.kwargs["messages"][0]["content"]

    assert "ENTITLEMENT (relative to this scope)" in content

    chain_header_idx = content.index("entitled — directives and context")
    chain_names_idx = content.index(f"{SCOPE.id} ({SCOPE.name})")
    descendants_header_idx = content.index("evidence proposed upward")
    descendants_names_idx = content.index(f"{DESCENDANT_SCOPE.id} ({DESCENDANT_SCOPE.name})")
    peer_header_idx = content.index("entitled for CONTEXT only")
    peer_names_idx = content.index(f"{PEER_SCOPE.id} ({PEER_SCOPE.name})")
    other_header_idx = content.index("NOT entitled")
    other_names_idx = content.index(f"{OTHER_SCOPE.id} ({OTHER_SCOPE.name})")

    assert (
        chain_header_idx
        < chain_names_idx
        < descendants_header_idx
        < descendants_names_idx
        < peer_header_idx
        < peer_names_idx
        < other_header_idx
        < other_names_idx
    )


def test_entitlement_omitted_when_none() -> None:
    """entitlement=None (the default) omits the ENTITLEMENT section entirely."""
    manager, mock_client = _make_manager(_accept_directive_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    call_kwargs = mock_client.messages.create.call_args
    content = call_kwargs.kwargs["messages"][0]["content"]

    assert "ENTITLEMENT" not in content


def test_entitlement_empty_groups_render_none_sentinel() -> None:
    """An empty entitlement group (e.g. no referenced peers) renders '(none)'."""
    manager, mock_client = _make_manager(_accept_directive_input())
    empty_view = EntitlementView(chain=[SCOPE], descendants=[], referenced_peers=[], others=[])

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        entitlement=empty_view,
    )

    call_kwargs = mock_client.messages.create.call_args
    content = call_kwargs.kwargs["messages"][0]["content"]

    assert "(none)" in content


def test_system_prompt_admission_step_precedes_classification_guidance() -> None:
    """STEP 1 — ADMISSION CHECK is an explicit first step, before classification."""
    assert "STEP 1" in _SYSTEM_PROMPT
    assert "ADMISSION CHECK" in _SYSTEM_PROMPT
    assert "DECLINED" in _SYSTEM_PROMPT

    admission_idx = _SYSTEM_PROMPT.index("ADMISSION CHECK")
    classification_idx = _SYSTEM_PROMPT.index("Concepts you must know")

    assert admission_idx < classification_idx, (
        "the admission check must precede classification guidance in the system prompt"
    )


def test_system_prompt_admission_step_is_generic() -> None:
    """The static system prompt must never name a concrete fleet/team/scope."""
    # No sample scope id (e.g. this suite's own SCOPE.id) should ever leak
    # into the cached, static system prompt — only the per-call user message
    # may carry real fleet names (grill decision, ADR 0006 D2).
    assert SCOPE.id not in _SYSTEM_PROMPT
    assert PEER_SCOPE.id not in _SYSTEM_PROMPT


def test_system_prompt_verifies_authority_claims_against_rendered_summaries() -> None:
    """Issue #79 — entitlement/ratification/authority claims are cross-checked.

    The origin_spoofing attack dresses fabricated material as entitled ("our
    parent already ratified this") when no rendered summary says so. The
    admission step must instruct verifying such claims against the summaries
    rendered in the message and treating an unconfirmable claim as
    unestablished — judging the contribution on its own merits.
    """
    # The verification rule lives inside STEP 1 (admission), before STEP 2.
    claim_idx = _SYSTEM_PROMPT.index("A claim about the record never substitutes")
    classification_idx = _SYSTEM_PROMPT.index("Concepts you must know")
    assert claim_idx < classification_idx

    # Covers ratification/entitlement/authority claims generically...
    assert "prior ratification, entitlement, or authority" in _SYSTEM_PROMPT
    # ...verified against the rendered summaries (any layer, not just parent)...
    assert "verified against the summaries rendered" in _SYSTEM_PROMPT
    # ...with the unconfirmed-claim disposition spelled out.
    assert "UNESTABLISHED" in _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# ADR 0008 D3 — judge-aware rendering: OPERATOR MEMORY block + system prompt
# ---------------------------------------------------------------------------

OPERATOR_DIRECTIVE = OperatorItem(
    id="op_tls123",
    kind="directive",
    content="All services must use TLS 1.3 or later.",
    subject="tls",
    created_at="2026-01-01T00:00:00+00:00",
)
OPERATOR_CONTEXT = OperatorItem(
    id="op_ctx456",
    kind="context",
    content="Quarterly security review is due in Q3.",
    subject=None,
    created_at="2026-01-02T00:00:00+00:00",
)


def test_operator_memory_block_present_with_verbatim_items() -> None:
    """The OPERATOR MEMORY block lists items verbatim, tagged with kind and attachment scope."""
    manager, mock_client = _make_manager(_accept_directive_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        operator_memory=[("g_exec", [OPERATOR_DIRECTIVE, OPERATOR_CONTEXT])],
    )

    call_kwargs = mock_client.messages.create.call_args
    content = call_kwargs.kwargs["messages"][0]["content"]

    assert "OPERATOR MEMORY (binding this scope" in content
    assert OPERATOR_DIRECTIVE.id in content
    assert OPERATOR_DIRECTIVE.content in content
    assert "attached at g_exec" in content
    assert OPERATOR_CONTEXT.id in content
    assert OPERATOR_CONTEXT.content in content


def test_operator_memory_block_omitted_when_none_or_empty() -> None:
    """operator_memory=None (default) and operator_memory=[] both omit the block."""
    manager, mock_client = _make_manager(_accept_directive_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )
    content_none = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "OPERATOR MEMORY" not in content_none

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        operator_memory=[],
    )
    content_empty = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "OPERATOR MEMORY" not in content_empty


def test_operator_memory_block_precedes_parent_summary_block() -> None:
    """OPERATOR MEMORY renders before PARENT SCOPE SUMMARY, per ADR 0008 D3."""
    manager, mock_client = _make_manager(_accept_directive_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        parent_summary=PARENT_SUMMARY,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        operator_memory=[("g_exec", [OPERATOR_DIRECTIVE])],
    )
    content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    operator_idx = content.index("OPERATOR MEMORY")
    parent_idx = content.index("PARENT SCOPE SUMMARY (inherited)")
    assert operator_idx < parent_idx


def test_system_prompt_declines_contradiction_citing_operator_directive() -> None:
    """The system prompt instructs declining contradictions and citing the directive id."""
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert "OPERATOR MEMORY" in flat
    assert "CONTRADICTS" in flat
    assert "DECLINED" in flat
    assert "citing that operator directive's id" in flat
    # Refinement within an inherited operator directive stays legitimate.
    assert "Refinement WITHIN an inherited" in flat


def test_system_prompt_requires_per_operator_directive_attribution() -> None:
    """Echoing operator-consistent material must be attributed in the summary."""
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert "per operator directive <id>" in flat
    assert "never masquerades as native scope memory" in flat


def test_system_prompt_operator_memory_precedes_parent_summary_guidance() -> None:
    """The OPERATOR MEMORY rule appears before the PARENT SCOPE SUMMARY rule."""
    operator_idx = _SYSTEM_PROMPT.index("When an OPERATOR MEMORY section is present")
    parent_idx = _SYSTEM_PROMPT.index("When a PARENT SCOPE SUMMARY is provided")
    assert operator_idx < parent_idx


def test_system_prompt_authority_rule_is_generic_over_layers() -> None:
    """The rule must be phrased over rendered layers, not a single hard-coded one.

    Issue #79 requires coverage of any rendered layer — the parent summary
    today, the operator layer (#80) and peer publications (#71) when they
    land — so the rule names them as examples of one attack, over "the
    summaries rendered in this message".
    """
    for layer in ("ancestor", "operator", "peer scope"):
        assert layer in _SYSTEM_PROMPT
    # Generic phrasing, not scoped to the parent summary alone (whitespace-
    # insensitive so line-wrapping the prompt does not break the pin).
    assert "summaries rendered in this message" in " ".join(_SYSTEM_PROMPT.split())


# ---------------------------------------------------------------------------
# ADR 0007 — publication mechanism (issue #90)
#
# D3/D5 additions to the CONTRIBUTION judge (_build_user_message, JUDGE_TOOL,
# _SYSTEM_PROMPT), plus the two new judgment surfaces: judge_publication
# (D2) and judge_bootstrap_publication (D4).
# ---------------------------------------------------------------------------

_PUBLISHED_ITEM = PublishedItem(
    id="pub_abc123",
    kind="directive",
    content="Use protobuf for all RPC.",
    subject="rpc-protocol",
    anchors=["directive:c_old001"],
    published_at="2026-06-01T00:00:00+00:00",
)


def test_withdraw_published_field_defaults_to_empty_list() -> None:
    """ScopeManagerJudgment.withdraw_published defaults to [] when the tool omits it."""
    manager, _ = _make_manager(_accept_context_input())

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    assert judgment.withdraw_published == []


def test_withdraw_published_field_parsed_from_tool_response() -> None:
    """A non-empty withdraw_published list in the tool response is parsed through."""
    raw = _accept_context_input()
    raw["withdraw_published"] = ["pub_abc123", "pub_def456"]
    manager, _ = _make_manager(raw)

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    assert judgment.withdraw_published == ["pub_abc123", "pub_def456"]


def test_withdraw_published_null_parses_as_empty_list() -> None:
    raw = _accept_context_input()
    raw["withdraw_published"] = None
    manager, _ = _make_manager(raw)

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    assert judgment.withdraw_published == []


def test_judge_tool_schema_carries_withdraw_published_not_required() -> None:
    """withdraw_published is in the schema but NOT in the required list (may be omitted)."""
    props = JUDGE_TOOL["input_schema"]["properties"]
    assert "withdraw_published" in props
    assert props["withdraw_published"]["type"] == ["array", "null"]
    assert "withdraw_published" not in JUDGE_TOOL["input_schema"]["required"]


def test_current_publication_block_rendered_when_provided() -> None:
    """THIS SCOPE'S PUBLICATION renders when current_publication is given (even if empty)."""
    manager, mock_client = _make_manager(_accept_context_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        current_publication=[_PUBLISHED_ITEM],
    )

    content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "THIS SCOPE'S PUBLICATION" in content
    assert "pub_abc123" in content
    assert "Use protobuf for all RPC." in content
    assert "directive:c_old001" in content


def test_current_publication_block_omitted_when_none() -> None:
    """current_publication=None (the default) omits the block entirely — no behaviour change."""
    manager, mock_client = _make_manager(_accept_context_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "THIS SCOPE'S PUBLICATION" not in content


def test_current_publication_block_shows_none_yet_when_empty_list() -> None:
    """An explicit empty current_publication still renders the header — the honest empty face."""
    manager, mock_client = _make_manager(_accept_context_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        current_publication=[],
    )

    content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "THIS SCOPE'S PUBLICATION" in content
    assert "(none yet)" in content


def test_peer_publications_block_rendered_when_provided() -> None:
    """REFERENCED PEER PUBLICATIONS renders when peer_publications is given, labelled by origin."""
    manager, mock_client = _make_manager(_accept_context_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        peer_publications=[("g_peer_a", [_PUBLISHED_ITEM])],
    )

    content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "REFERENCED PEER PUBLICATIONS" in content
    assert "g_peer_a" in content
    assert "pub_abc123" in content


def test_peer_publications_block_omitted_when_none_or_empty() -> None:
    manager, mock_client = _make_manager(_accept_context_input())

    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        peer_publications=[],
    )

    content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "REFERENCED PEER PUBLICATIONS" not in content


def test_system_prompt_states_attribution_through_condensation() -> None:
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert "according to" in flat
    assert "attribution through condensation" in flat.lower() or "every SUBSEQUENT rewrite" in flat


def test_system_prompt_states_no_echo_rule() -> None:
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert "never corroborates its own source" in flat


def test_system_prompt_names_withdraw_published_trigger() -> None:
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert "withdraw_published" in flat
    assert "drops or contradicts the belief" in flat.lower() or "drops OR CONTRADICTS" in flat


# ---------------------------------------------------------------------------
# judge_publication (ADR 0007 D2)
# ---------------------------------------------------------------------------


def _make_publication_manager(
    decision: str, reasoning: str = "reasoning"
) -> tuple[ScopeManager, MagicMock]:
    block = MagicMock()
    block.type = "tool_use"
    block.input = {"decision": decision, "reasoning": reasoning}
    response = MagicMock()
    response.content = [block]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = response
    manager = ScopeManager(client=mock_client)
    return manager, mock_client


def test_judge_publication_publish_accept_parses_correctly() -> None:
    manager, mock_client = _make_publication_manager("accept", "Fits published <= believed.")

    judgment = manager.judge_publication(
        scope=SCOPE,
        act_kind="publish",
        content="Use protobuf for all RPC.",
        kind="directive",
        subject="rpc-protocol",
        anchors=["directive:c_old001"],
        current_summary=CURRENT_SUMMARY,
        current_publication=[],
    )

    assert isinstance(judgment, PublicationJudgment)
    assert judgment.decision == "accept"
    assert judgment.reasoning == "Fits published <= believed."

    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["tool_choice"]["name"] == "submit_publication_judgment"
    assert call_kwargs["system"][0]["text"] == _PUBLICATION_SYSTEM_PROMPT
    assert call_kwargs["tools"][0]["name"] == PUBLICATION_JUDGE_TOOL["name"]
    message = call_kwargs["messages"][0]["content"]
    assert "PROPOSED ACT: publish" in message
    assert "Use protobuf for all RPC." in message


def test_judge_publication_publish_decline_parses_correctly() -> None:
    manager, _ = _make_publication_manager("decline", "Reads as internal scratch.")

    judgment = manager.judge_publication(
        scope=SCOPE,
        act_kind="publish",
        content="half-formed idea",
        kind="context",
        subject=None,
        anchors=["subject:notes"],
        current_summary=CURRENT_SUMMARY,
        current_publication=[],
    )

    assert judgment.decision == "decline"


def test_judge_publication_publish_missing_fields_raises() -> None:
    manager, _ = _make_publication_manager("accept")
    with pytest.raises(ValueError, match="content, kind"):
        manager.judge_publication(
            scope=SCOPE,
            act_kind="publish",
            current_summary=CURRENT_SUMMARY,
            current_publication=[],
        )


def test_judge_publication_withdraw_renders_item_and_parses_correctly() -> None:
    manager, mock_client = _make_publication_manager("accept", "Fine to withdraw.")

    judgment = manager.judge_publication(
        scope=SCOPE,
        act_kind="withdraw",
        withdraw_item=_PUBLISHED_ITEM,
        current_summary=CURRENT_SUMMARY,
        current_publication=[_PUBLISHED_ITEM],
    )

    assert judgment.decision == "accept"
    message = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "PROPOSED ACT: withdraw" in message
    assert "pub_abc123" in message


def test_judge_publication_withdraw_missing_item_raises() -> None:
    manager, _ = _make_publication_manager("accept")
    with pytest.raises(ValueError, match="withdraw_item"):
        manager.judge_publication(
            scope=SCOPE,
            act_kind="withdraw",
            current_summary=CURRENT_SUMMARY,
            current_publication=[],
        )


def test_judge_publication_missing_api_key_raises_runtimeerror() -> None:
    mock_client = MagicMock()
    mock_client.api_key = None
    manager = ScopeManager(client=mock_client)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        manager.judge_publication(
            scope=SCOPE,
            act_kind="publish",
            content="x",
            kind="context",
            subject=None,
            anchors=["subject:x"],
            current_summary=CURRENT_SUMMARY,
            current_publication=[],
        )


def test_publication_system_prompt_states_distinct_judgment() -> None:
    flat = " ".join(_PUBLICATION_SYSTEM_PROMPT.split())
    assert "true and useful for us" in flat
    assert "ready for others to act on" in flat


def test_publication_system_prompt_states_published_within_believed() -> None:
    flat = " ".join(_PUBLICATION_SYSTEM_PROMPT.split())
    assert "PUBLISHED MUST STAY WITHIN BELIEVED" in flat
    assert "not round up" in flat or "round up" in flat


def test_publication_system_prompt_states_audience_fitness() -> None:
    flat = " ".join(_PUBLICATION_SYSTEM_PROMPT.split())
    assert "internal scratch" in flat
    assert "dead end" in flat


# ---------------------------------------------------------------------------
# judge_bootstrap_publication (ADR 0007 D4)
# ---------------------------------------------------------------------------


def _make_bootstrap_manager(decision: str, items: list[dict] | None, reasoning: str = "reasoning"):
    block = MagicMock()
    block.type = "tool_use"
    block.input = {"decision": decision, "reasoning": reasoning, "items": items}
    response = MagicMock()
    response.content = [block]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = response
    manager = ScopeManager(client=mock_client)
    return manager, mock_client


def test_judge_bootstrap_publication_accept_parses_items() -> None:
    manager, mock_client = _make_bootstrap_manager(
        "accept",
        [
            {
                "content": "Use protobuf for all RPC.",
                "kind": "directive",
                "subject": "rpc",
                "anchors": ["c_old001"],
            }
        ],
        "One item is fit for export.",
    )

    judgment = manager.judge_bootstrap_publication(scope=SCOPE, current_summary=CURRENT_SUMMARY)

    assert isinstance(judgment, BootstrapJudgment)
    assert judgment.decision == "accept"
    assert len(judgment.items) == 1
    assert judgment.items[0].content == "Use protobuf for all RPC."
    assert judgment.items[0].anchors == ["c_old001"]

    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["tool_choice"]["name"] == "submit_bootstrap_publication"
    assert call_kwargs["system"][0]["text"] == _BOOTSTRAP_SYSTEM_PROMPT
    assert call_kwargs["tools"][0]["name"] == BOOTSTRAP_JUDGE_TOOL["name"]


def test_judge_bootstrap_publication_decline_parses_empty_items() -> None:
    manager, _ = _make_bootstrap_manager("decline", None, "Nothing fit yet.")

    judgment = manager.judge_bootstrap_publication(scope=SCOPE, current_summary=CURRENT_SUMMARY)

    assert judgment.decision == "decline"
    assert judgment.items == []


def test_judge_bootstrap_publication_no_summary_uses_sentinel() -> None:
    manager, mock_client = _make_bootstrap_manager("decline", None)

    manager.judge_bootstrap_publication(scope=SCOPE, current_summary=None)

    message = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "this scope has no summary yet" in message


def test_bootstrap_system_prompt_states_conservative_default() -> None:
    flat = " ".join(_BOOTSTRAP_SYSTEM_PROMPT.split())
    assert "conservative" in flat.lower()
    assert "initial" in flat.lower() or "INITIAL" in flat


# ---------------------------------------------------------------------------
# ADR 0011 D1 — amendment ops: existing directives are never re-emitted
# ---------------------------------------------------------------------------


def _second_directive() -> Directive:
    return Directive(
        id="c_old002",
        content="Prefer composition over inheritance.",
        subject="design",
        source_scope_id=SCOPE.id,
        source_skill="architect",
        created_at="2026-04-02T09:00:00+00:00",
    )


def _summary_with(*directives: Directive, context: str = "Existing context.") -> ScopeSummary:
    return ScopeSummary(
        scope_id=SCOPE.id,
        directives=list(directives),
        context=context,
        updated_at="2026-04-01T09:00:00+00:00",
    )


def _contribution(
    contribution_id: str, content: str, *, subject: str | None = None
) -> Contribution:
    return Contribution(
        id=contribution_id,
        scope_id=SCOPE.id,
        content=content,
        proposed_classification="directive",
        subject=subject,
        supersedes=None,
        contributor=CONTRIBUTOR,
        created_at="2026-05-02T10:00:00+00:00",
    )


def test_j1_untouched_directive_rows_are_byte_identical_across_judgments() -> None:
    """J1 — structural preservation: rows no op names come out byte-identical.

    Three accepted judgments in a row (append, publish, retire), each judged
    against the summary the previous one produced. The bystander directive is
    never named by an op, so its rendered bytes must be the same after each.
    """
    bystander = EXISTING_DIRECTIVE
    doomed = _second_directive()
    summary = _summary_with(bystander, doomed)

    def rendered_row(s: ScopeSummary) -> str:
        return _render_summary(
            ScopeSummary(
                scope_id=s.scope_id,
                directives=[d for d in s.directives if d.id == bystander.id],
                context="",
                updated_at="fixed",
            )
        )

    baseline = rendered_row(summary)

    amendments = [
        ({"op": "append"}, _contribution("c_a1", "First new rule.")),
        (
            {"op": "publish", "content": "Ratified: keep interfaces narrow."},
            _contribution("c_a2", "Several teams keep interfaces narrow."),
        ),
        ({"op": "retire", "id": doomed.id}, _contribution("c_a3", "That rule is obsolete.")),
    ]

    for op, contribution in amendments:
        manager, _ = _make_manager(
            {
                "decision": "accept_as_directive",
                "reasoning": "judged",
                "directive_ops": [op],
                "new_context": "Context rewritten again.",
            }
        )
        judgment = manager.judge(
            scope=SCOPE,
            stratum=STRATUM,
            current_summary=summary,
            recent_contributions=[],
            new_contribution=contribution,
        )
        assert judgment.new_summary is not None
        summary = judgment.new_summary
        assert rendered_row(summary) == baseline
        assert bystander in summary.directives


def test_append_op_uses_the_contribution_verbatim() -> None:
    """`append` copies content, id, subject, and provenance — the judge writes nothing."""
    contribution = _contribution("c_app1", "Ship behind a flag.", subject="rollout")
    judgment = ScopeManager._parse_judgment(
        scope=SCOPE,
        tool_use_block=_tool_use_block(
            {
                "decision": "accept_as_directive",
                "reasoning": "clear standard",
                "directive_ops": [{"op": "append"}],
                "new_context": "ctx",
            }
        ),
        current_summary=_summary_with(EXISTING_DIRECTIVE),
        new_contribution=contribution,
    )

    assert judgment.new_summary is not None
    appended = judgment.new_summary.directives[-1]
    assert appended.id == contribution.id
    assert appended.content == contribution.content
    assert appended.subject == "rollout"
    assert appended.source_scope_id == CONTRIBUTOR.scope_id
    assert appended.source_skill == CONTRIBUTOR.skill
    assert appended.created_at == contribution.created_at


def test_publish_op_admits_judge_text_under_the_contribution_id() -> None:
    """`publish` carries the judge's wording but mints the id from the contribution."""
    contribution = _contribution("c_pub1", "Three teams now do X.", subject="pattern")
    judgment = ScopeManager._parse_judgment(
        scope=SCOPE,
        tool_use_block=_tool_use_block(
            {
                "decision": "accept_as_directive",
                "reasoning": "ratifying an accumulated pattern",
                "directive_ops": [
                    {"op": "publish", "content": "All teams do X (per operator directive op_1)."}
                ],
                "new_context": "ctx",
            }
        ),
        current_summary=_summary_with(EXISTING_DIRECTIVE),
        new_contribution=contribution,
    )

    assert judgment.new_summary is not None
    published = judgment.new_summary.directives[-1]
    assert published.id == contribution.id
    assert published.content == "All teams do X (per operator directive op_1)."
    # An omitted subject keeps the contribution's own tag rather than dropping it.
    assert published.subject == "pattern"


def test_publish_op_without_content_is_a_parse_error() -> None:
    with pytest.raises(ValueError, match="publish op with no content"):
        _parse(
            {
                "decision": "accept_as_directive",
                "reasoning": "r",
                "directive_ops": [{"op": "publish", "content": "   "}],
                "new_context": None,
            }
        )


def test_retire_op_without_id_is_a_parse_error() -> None:
    with pytest.raises(ValueError, match="retire op with no id"):
        _parse(
            {
                "decision": "accept_as_directive",
                "reasoning": "r",
                "directive_ops": [{"op": "retire"}],
                "new_context": None,
            }
        )


def test_unknown_op_is_a_parse_error() -> None:
    with pytest.raises(ValueError, match="unknown directive op"):
        _parse(
            {
                "decision": "accept_as_directive",
                "reasoning": "r",
                "directive_ops": [{"op": "rewrite"}],
                "new_context": None,
            }
        )


def test_supersede_paired_with_append_replaces_the_named_directive() -> None:
    """Supersession replaces: the old row leaves, the new contribution lands."""
    contribution = _contribution("c_sup1", "Use kebab-case for all identifiers.")
    judgment = ScopeManager._parse_judgment(
        scope=SCOPE,
        tool_use_block=_tool_use_block(
            {
                "decision": "accept_as_directive",
                "reasoning": "replaces the naming rule",
                "directive_ops": [
                    {"op": "append"},
                    {"op": "supersede", "id": EXISTING_DIRECTIVE.id},
                ],
                "new_context": "ctx",
            }
        ),
        current_summary=_summary_with(EXISTING_DIRECTIVE, _second_directive()),
        new_contribution=contribution,
    )

    assert judgment.new_summary is not None
    ids = [d.id for d in judgment.new_summary.directives]
    assert EXISTING_DIRECTIVE.id not in ids
    assert ids == ["c_old002", contribution.id]
    # No tombstone, and no retirement event: a superseded directive's
    # explanation is the incoming directive's own supersedes reference.
    assert judgment.removed_directive_ids == [EXISTING_DIRECTIVE.id]
    assert judgment.retired_directive_ids == []


def test_publish_supersedes_reference_removes_the_named_directive() -> None:
    """`publish` carrying a supersedes reference removes what it replaces."""
    contribution = _contribution("c_sup2", "Teams keep converging on one naming rule.")
    judgment = ScopeManager._parse_judgment(
        scope=SCOPE,
        tool_use_block=_tool_use_block(
            {
                "decision": "accept_as_directive",
                "reasoning": "ratified in this scope's own wording",
                "directive_ops": [
                    {
                        "op": "publish",
                        "content": "Identifiers are kebab-case.",
                        "supersedes": EXISTING_DIRECTIVE.id,
                    }
                ],
                "new_context": None,
            }
        ),
        current_summary=_summary_with(EXISTING_DIRECTIVE),
        new_contribution=contribution,
    )

    assert judgment.new_summary is not None
    assert [d.id for d in judgment.new_summary.directives] == [contribution.id]
    assert judgment.removed_directive_ids == [EXISTING_DIRECTIVE.id]


def test_unpaired_supersede_is_rejected_at_parse() -> None:
    """A supersede with nothing to replace with is a retirement wearing the wrong name."""
    with pytest.raises(ValueError, match="unpaired supersede"):
        _parse(
            {
                "decision": "accept_as_context",
                "reasoning": "dropping the old rule",
                "directive_ops": [{"op": "supersede", "id": EXISTING_DIRECTIVE.id}],
                "new_context": "ctx",
            }
        )


def test_unpaired_supersede_gets_the_parse_reask_then_propagates() -> None:
    """The #113 one-retry discipline covers it: one corrective, then the error stands."""
    unpaired = {
        "decision": "accept_as_context",
        "reasoning": "dropping the old rule",
        "directive_ops": [{"op": "supersede", "id": EXISTING_DIRECTIVE.id}],
        "new_context": "ctx",
    }
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(unpaired),
        _fake_response(unpaired),
    ]
    manager = ScopeManager(client=mock_client)

    with pytest.raises(ValueError, match="unpaired supersede"):
        manager.judge(
            scope=SCOPE,
            stratum=STRATUM,
            current_summary=CURRENT_SUMMARY,
            recent_contributions=[],
            new_contribution=NEW_CONTRIBUTION,
        )

    assert mock_client.messages.create.call_count == 2


def test_retire_op_removes_the_directive_and_is_reported_for_the_record() -> None:
    """`retire` removes with no replacement; the caller records the retirement event."""
    judgment = ScopeManager._parse_judgment(
        scope=SCOPE,
        tool_use_block=_tool_use_block(
            {
                "decision": "accept_as_context",
                "reasoning": "the naming rule no longer applies",
                "directive_ops": [{"op": "retire", "id": EXISTING_DIRECTIVE.id}],
                "new_context": "ctx",
            }
        ),
        current_summary=_summary_with(EXISTING_DIRECTIVE, _second_directive()),
        new_contribution=NEW_CONTRIBUTION,
    )

    assert judgment.new_summary is not None
    assert [d.id for d in judgment.new_summary.directives] == ["c_old002"]
    assert judgment.retired_directive_ids == [EXISTING_DIRECTIVE.id]
    assert judgment.removed_directive_ids == [EXISTING_DIRECTIVE.id]


def test_null_new_context_leaves_the_context_untouched() -> None:
    """An omitted context section is not an emptied one."""
    judgment = ScopeManager._parse_judgment(
        scope=SCOPE,
        tool_use_block=_tool_use_block(
            {
                "decision": "accept_as_directive",
                "reasoning": "nothing to add to the digest",
                "directive_ops": [{"op": "append"}],
                "new_context": None,
            }
        ),
        current_summary=CURRENT_SUMMARY,
        new_contribution=NEW_CONTRIBUTION,
    )

    assert judgment.new_summary is not None
    assert judgment.new_summary.context == CURRENT_SUMMARY.context


# ---------------------------------------------------------------------------
# ADR 0011 D1 — invalid ids: one corrective, then drop-and-note
# ---------------------------------------------------------------------------


def _retire_input(directive_id: str, *, reasoning: str = "retiring") -> dict:
    return {
        "decision": "accept_as_context",
        "reasoning": reasoning,
        "directive_ops": [{"op": "retire", "id": directive_id}],
        "new_context": "Context after the retirement.",
    }


def test_invalid_id_triggers_one_corrective_listing_the_valid_ids() -> None:
    """The corrective names the offending op and every id the judge may name."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(_retire_input("c_hallucinated")),
        _fake_response(_retire_input(EXISTING_DIRECTIVE.id)),
    ]
    manager = ScopeManager(client=mock_client)

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    assert mock_client.messages.create.call_count == 2
    followup = mock_client.messages.create.call_args_list[1].kwargs["messages"][-1]
    text = [b for b in followup["content"] if b["type"] == "text"][0]["text"]
    assert "c_hallucinated" in text
    assert EXISTING_DIRECTIVE.id in text
    assert "not in this scope's summary" in text
    # The corrective is NOT the #113 stringified-payload wording.
    assert "could not be parsed" not in text

    # The corrected amendment applies in full — nothing dropped.
    assert judgment.dropped_ops == []
    assert judgment.retired_directive_ids == [EXISTING_DIRECTIVE.id]
    assert judgment.record_notes == judgment.reasoning


def test_invalid_id_twice_drops_the_op_and_notes_it_without_losing_the_verdict() -> None:
    """J5 — drop-and-note: the bad op goes, the rest applies, the verdict survives."""
    first = {
        "decision": "accept_as_directive",
        "reasoning": "admitting the new rule and retiring a stale one",
        "directive_ops": [{"op": "append"}, {"op": "retire", "id": "c_ghost"}],
        "new_context": "Context after the amendment.",
    }
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(first),
        _fake_response(first),  # still names the ghost id
    ]
    manager = ScopeManager(client=mock_client)

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    # Exactly one corrective — never a loop, never a parse failure.
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_directive"
    assert judgment.new_summary is not None

    # The rest of the amendment applied: the contribution is admitted, the
    # existing directive is untouched, the context is rewritten.
    ids = [d.id for d in judgment.new_summary.directives]
    assert ids == [EXISTING_DIRECTIVE.id, NEW_CONTRIBUTION.id]
    assert judgment.new_summary.context == "Context after the amendment."

    # The dropped op is noted in what the record gets.
    assert judgment.dropped_ops == ["retire(c_ghost)"]
    assert judgment.retired_directive_ids == []
    assert "retire(c_ghost)" in judgment.record_notes
    assert judgment.reasoning in judgment.record_notes


def test_invalid_id_corrective_failure_still_returns_the_first_verdict() -> None:
    """A bad op never costs the contribution its verdict, even if the retry errors."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(_retire_input("c_ghost", reasoning="retiring a stale rule")),
        RuntimeError("api unavailable"),
    ]
    manager = ScopeManager(client=mock_client)

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    assert judgment.decision == "accept_as_context"
    assert judgment.new_summary is not None
    assert judgment.new_summary.directives == CURRENT_SUMMARY.directives
    assert judgment.dropped_ops == ["retire(c_ghost)"]


def test_already_retired_id_named_twice_is_invalid() -> None:
    """The second op targeting the same directive is an already-retired id."""
    twice = {
        "decision": "accept_as_context",
        "reasoning": "retiring",
        "directive_ops": [
            {"op": "retire", "id": EXISTING_DIRECTIVE.id},
            {"op": "retire", "id": EXISTING_DIRECTIVE.id},
        ],
        "new_context": "ctx",
    }
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [_fake_response(twice), _fake_response(twice)]
    manager = ScopeManager(client=mock_client)

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    assert judgment.retired_directive_ids == [EXISTING_DIRECTIVE.id]
    assert judgment.dropped_ops == [f"retire({EXISTING_DIRECTIVE.id})"]


def test_overflow_retry_may_retire_to_fit() -> None:
    """The overflow corrective's lever works: the retry retires and the summary fits."""
    long_context = " ".join(f"word{i}" for i in range(40))
    first = {
        "decision": "accept_as_context",
        "reasoning": "recording the observation",
        "directive_ops": [],
        "new_context": long_context,
    }
    second = {
        "decision": "accept_as_context",
        "reasoning": "recording the observation",
        "directive_ops": [{"op": "retire", "id": EXISTING_DIRECTIVE.id}],
        "new_context": "Short.",
    }
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [_fake_response(first), _fake_response(second)]
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
    assert judgment.new_summary is not None
    assert judgment.new_summary.directives == []
    assert judgment.retired_directive_ids == [EXISTING_DIRECTIVE.id]
    assert _summary_word_count(judgment.new_summary) <= 5


def test_overflow_retry_refusing_to_retire_keeps_the_over_budget_summary() -> None:
    """Keep-first: an over-budget summary beats a destroyed judgment."""
    long_context = " ".join(f"word{i}" for i in range(40))
    over_budget = {
        "decision": "accept_as_context",
        "reasoning": "recording the observation",
        "directive_ops": [],
        "new_context": long_context,
    }
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(over_budget),
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

    assert judgment.decision == "accept_as_context"
    assert judgment.new_summary is not None
    assert judgment.new_summary.context == long_context
    assert judgment.new_summary.directives == CURRENT_SUMMARY.directives


# ---------------------------------------------------------------------------
# ADR 0011 D4 — the refresh amendment is context + lifecycle ops only
# ---------------------------------------------------------------------------


def test_refresh_block_rendered_when_amendment_is_context_only() -> None:
    message = _build_user_message(
        scope=SCOPE,
        stratum=STRATUM,
        parent_summary=PARENT_SUMMARY,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        amendment_context_only=True,
    )
    assert "MANAGER REFRESH" in message
    assert "already been incorporated" in message

    ordinary = _build_user_message(
        scope=SCOPE,
        stratum=STRATUM,
        parent_summary=PARENT_SUMMARY,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )
    assert "MANAGER REFRESH" not in ordinary


def test_context_only_amendment_drops_append_and_publish_ops() -> None:
    """A refresh may amend context and retire, but never admit a directive."""
    manager, _ = _make_manager(
        {
            "decision": "accept_as_context",
            "reasoning": "refreshed against the parent",
            "directive_ops": [
                {"op": "append"},
                {"op": "publish", "content": "Something new."},
                {"op": "retire", "id": EXISTING_DIRECTIVE.id},
            ],
            "new_context": "Reconciled context.",
        }
    )

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        amendment_context_only=True,
    )

    assert judgment.new_summary is not None
    assert judgment.new_summary.directives == []
    assert judgment.new_summary.context == "Reconciled context."
    assert [op.op for op in judgment.directive_ops] == ["retire"]
    assert judgment.dropped_ops == ["append", "publish"]
    assert "append" in judgment.record_notes


# ---------------------------------------------------------------------------
# ADR 0011 D1/D4 — system prompt contract
# ---------------------------------------------------------------------------


def test_system_prompt_states_the_amendment_contract() -> None:
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert "you do NOT rewrite the summary" in flat
    assert "`directive_ops`" in flat
    assert "`new_context`" in flat
    assert "preserved by the engine byte for byte" in flat
    for op in ("`append`", "`publish`", "`supersede`", "`retire`"):
        assert op in flat


def test_system_prompt_encodes_the_append_versus_publish_rule() -> None:
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert (
        "APPEND unless the binding text must differ from the contribution's text; "
        "if it must, PUBLISH, and say why in your reasoning" in flat
    )


def test_system_prompt_rejects_unpaired_supersede() -> None:
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert "unpaired `supersede` is a retirement wearing the wrong name" in flat


def test_system_prompt_narrows_operator_attribution_to_publish_or_context() -> None:
    """ADR 0011 D1: an unattributed operator echo may not be appended."""
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert "must never be `append`ed" in flat
    assert "`publish` it with the attribution written in" in flat
    assert "or carry it in `new_context` with the attribution" in flat


def test_system_prompt_no_longer_asks_the_judge_to_quote_parent_directives() -> None:
    """ADR 0011 D4 deletes the parent-quoting rule — the splice is mechanical."""
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert "quote any parent directives VERBATIM" not in flat
    assert "MECHANICALLY" in flat
    assert "never `append` or `publish` a parent directive" in flat


def test_system_prompt_budget_rule_names_the_two_levers() -> None:
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert "a directive leaves the summary only through a `retire` or a `supersede` op" in flat


def test_system_prompt_withdraw_rule_is_phrased_against_the_amendment() -> None:
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert "THE AMENDMENT YOU ARE SUBMITTING DROPS or CONTRADICTS" in flat
