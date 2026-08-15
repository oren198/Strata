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
from strata.record_store import Contribution, ContributorRef, RecentContribution
from strata.scope_manager import (
    _BATCH_SYSTEM_PROMPT,
    _BOOTSTRAP_SYSTEM_PROMPT,
    _PUBLICATION_SYSTEM_PROMPT,
    _SYSTEM_PROMPT,
    BOOTSTRAP_JUDGE_TOOL,
    JUDGE_BATCH_MAX_TOKENS_PER_EXTRA,
    JUDGE_BATCH_TOOL,
    JUDGE_MAX_TOKENS,
    JUDGE_TOOL,
    PUBLICATION_JUDGE_TOOL,
    WINDOW_CONTENT_PREFIX_CHARS,
    WINDOW_MAX_CHARS,
    WINDOW_TRUNCATION_MARKER,
    WINDOW_VERBATIM_TAIL,
    BootstrapJudgment,
    PublicationJudgment,
    ScopeManager,
    ScopeManagerJudgment,
    _batch_max_tokens,
    _build_batch_user_message,
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

# ADR 0011 D2: the judge's recency window is (contribution, state,
# judgment-notes) triples, not verbatim contributions.
RECENT_ROW = RecentContribution(
    contribution=RECENT_CONTRIBUTION,
    state="judged",
    decision="accept_as_context",
    judgment_notes="Accepted as context: an observation, not a binding rule.",
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


def test_digest_row_never_renders_a_none_placeholder() -> None:
    """A subject-less, unjudged row renders sentinels, never a literal ``None`` (issue #121)."""
    row = _row(_window_contribution("c_ns", "observation", subject=None), state="pending")
    rendered = _render_recent_contributions([row])
    assert "None" not in rendered
    assert "subject=(none)" in rendered


# ---------------------------------------------------------------------------
# ADR 0011 D2 — the mechanical recency digest
# ---------------------------------------------------------------------------


def _window_contribution(cid: str, content: str, *, subject: str | None = "topic") -> Contribution:
    """A window contribution, distinguishable by id and content."""
    return Contribution(
        id=cid,
        scope_id=SCOPE.id,
        content=content,
        proposed_classification="context",
        subject=subject,
        supersedes=None,
        contributor=CONTRIBUTOR,
        created_at="2026-04-15T08:00:00+00:00",
    )


def _row(
    contribution: Contribution,
    *,
    state: str = "judged",
    decision: str | None = "accept_as_context",
    notes: str | None = "Accepted: adds a fact the context lacked.",
) -> RecentContribution:
    """One window row; the defaults describe a judged row."""
    if state != "judged":
        decision, notes = None, None
    return RecentContribution(
        contribution=contribution,
        state=state,  # type: ignore[arg-type]
        decision=decision,  # type: ignore[arg-type]
        judgment_notes=notes,
    )


def test_judged_digest_row_carries_its_decision_and_reasoning() -> None:
    """A judged row shows the verdict and the notes written when it was judged."""
    row = _row(_window_contribution("c_j", "a judged contribution"))
    rendered = _render_recent_contributions([row], verbatim_tail=0)

    assert "[c_j]" in rendered
    assert "state=judged" in rendered
    assert "decision=accept_as_context" in rendered
    assert "reasoning=Accepted: adds a fact the context lacked." in rendered
    assert "at=2026-04-15T08:00:00+00:00" in rendered
    assert "subject=topic" in rendered


def test_pending_and_judge_failed_rows_render_state_with_empty_reasoning() -> None:
    """The two unjudged states show their state and nothing in the verdict columns.

    Both are routine in the window — ``pending`` includes the contribution
    under judgment, appended to the record before the window is read.
    """
    rendered = _render_recent_contributions(
        [
            _row(_window_contribution("c_p", "still in flight"), state="pending"),
            _row(_window_contribution("c_f", "the judge blew up"), state="judge_failed"),
        ],
        verbatim_tail=0,
    )
    pending_line, failed_line = rendered.splitlines()

    assert "state=pending" in pending_line
    assert "decision=(none)" in pending_line
    assert "reasoning=(none)" in pending_line

    assert "state=judge_failed" in failed_line
    assert "decision=(none)" in failed_line
    assert "reasoning=(none)" in failed_line


def test_content_prefix_truncates_at_the_constant_with_a_marker() -> None:
    """A long contribution renders a fixed-length excerpt, visibly cut."""
    content = "x" * (WINDOW_CONTENT_PREFIX_CHARS + 500)
    rendered = _render_recent_contributions(
        [_row(_window_contribution("c_long", content))], verbatim_tail=0
    )

    assert WINDOW_TRUNCATION_MARKER in rendered
    assert "x" * WINDOW_CONTENT_PREFIX_CHARS in rendered
    # The excerpt is a prefix, not the whole thing: one character past the
    # constant must not have survived.
    assert "x" * (WINDOW_CONTENT_PREFIX_CHARS + 1) not in rendered


def test_short_content_passes_through_whole_and_unmarked() -> None:
    """Content within the prefix length is rendered entire, with no marker."""
    content = "a short observation about naming"
    rendered = _render_recent_contributions(
        [_row(_window_contribution("c_short", content))], verbatim_tail=0
    )

    assert content in rendered
    assert WINDOW_TRUNCATION_MARKER not in rendered


def test_verbatim_tail_keeps_the_newest_rows_whole() -> None:
    """The newest ``window_verbatim_tail`` rows keep full text; older ones digest."""
    long_content = ["".join(f"{i}" * (WINDOW_CONTENT_PREFIX_CHARS + 50)) for i in range(5)]
    rows = [_row(_window_contribution(f"c_{i}", long_content[i])) for i in range(5)]

    rendered = _render_recent_contributions(rows, verbatim_tail=2)
    lines = rendered.splitlines()

    # Oldest three are excerpts...
    for line in lines[:3]:
        assert WINDOW_TRUNCATION_MARKER in line
    # ...the newest two are verbatim.
    for line, content in zip(lines[3:], long_content[3:], strict=True):
        assert WINDOW_TRUNCATION_MARKER not in line
        assert content in line


def test_self_row_never_takes_a_verbatim_slot() -> None:
    """The contribution under judgment is a digest row, however new it is.

    It is in its own window (appended to the record before the window is read),
    but its full text is already in the message as the NEW CONTRIBUTION block —
    a verbatim slot spent on it would duplicate it AND cost the judge a real
    prior contribution to compare against.
    """
    long = "s" * (WINDOW_CONTENT_PREFIX_CHARS + 50)
    rows = [
        _row(_window_contribution("c_prior", long)),
        _row(_window_contribution("c_self", long), state="pending"),
    ]

    rendered = _render_recent_contributions(rows, verbatim_tail=3, self_contribution_ids=["c_self"])
    prior_line, self_line = rendered.splitlines()

    assert "[c_self]" in self_line
    assert WINDOW_TRUNCATION_MARKER in self_line
    # ...and the slot it did not take went to the prior row.
    assert WINDOW_TRUNCATION_MARKER not in prior_line


def test_verbatim_slots_go_to_the_newest_prior_rows() -> None:
    """With the self row present, the tail's slots land on the newest PRIOR rows."""
    contents = [f"{i}" * (WINDOW_CONTENT_PREFIX_CHARS + 50) for i in range(5)]
    rows = [_row(_window_contribution(f"c_{i}", contents[i])) for i in range(5)]
    rows.append(_row(_window_contribution("c_self", "z" * 400), state="pending"))

    rendered = _render_recent_contributions(rows, verbatim_tail=3, self_contribution_ids=["c_self"])
    lines = rendered.splitlines()

    # Oldest two priors are excerpts, the newest three priors are verbatim...
    assert all(WINDOW_TRUNCATION_MARKER in line for line in lines[:2])
    for line, content in zip(lines[2:5], contents[2:5], strict=True):
        assert WINDOW_TRUNCATION_MARKER not in line
        assert content in line
    # ...and the self row is still an excerpt despite being the newest of all.
    assert "[c_self]" in lines[5]
    assert WINDOW_TRUNCATION_MARKER in lines[5]


def test_verbatim_tail_default_matches_the_adr() -> None:
    """The named default is 3 (ADR 0011 D2), and the setting's default agrees."""
    from strata.settings import Settings

    assert WINDOW_VERBATIM_TAIL == 3
    assert Settings().window_verbatim_tail == WINDOW_VERBATIM_TAIL


def test_recency_window_default_matches_the_adr() -> None:
    """The named default is 20 (ADR 0011 D2), and the setting's default agrees."""
    from strata.record_store import RECENCY_WINDOW_SIZE
    from strata.settings import Settings

    assert RECENCY_WINDOW_SIZE == 20
    assert Settings().recency_window_size == RECENCY_WINDOW_SIZE


def test_character_budget_drops_oldest_rows_and_says_it_did() -> None:
    """N rows or the character budget, whichever bites first — oldest go first.

    The cut is visible in the block: a window that silently shrank would let
    the judge treat a truncated window as the whole recent record.
    """
    rows = [
        _row(_window_contribution(f"c_{i:02d}", "y" * (WINDOW_CONTENT_PREFIX_CHARS - 20)))
        for i in range(60)
    ]

    rendered = _render_recent_contributions(rows, verbatim_tail=0)

    assert len(rendered) <= WINDOW_MAX_CHARS + 100  # + the omission line
    assert "omitted — window character budget" in rendered
    # The newest row survives; the oldest is the one that went.
    assert "[c_59]" in rendered
    assert "[c_00]" not in rendered


def test_character_budget_always_keeps_the_newest_row() -> None:
    """A single row larger than the whole budget still renders — an empty window is worse."""
    rows = [_row(_window_contribution("c_huge", "z" * (WINDOW_MAX_CHARS * 2)))]

    rendered = _render_recent_contributions(rows, verbatim_tail=1)

    assert "[c_huge]" in rendered


def test_digest_row_carries_the_id_a_supersedes_reference_needs() -> None:
    """Duplicate and supersedes checks survive the digest: the id is column one.

    The judge verifies a contribution's ``supersedes`` target against what the
    window shows; an excerpt-only row would still have to name the row's id.
    """
    row = _row(
        _window_contribution("c_target", "b" * (WINDOW_CONTENT_PREFIX_CHARS + 100)),
        decision="accept_as_directive",
    )
    rendered = _render_recent_contributions([row], verbatim_tail=0)

    assert "[c_target]" in rendered
    assert WINDOW_TRUNCATION_MARKER in rendered  # content cut, id intact


def test_empty_window_renders_the_sentinel() -> None:
    """A scope with no record yet renders ``(none)``, not an empty block."""
    assert _render_recent_contributions([]) == "(none)"


def test_user_message_digest_block_announces_the_verbatim_tail() -> None:
    """The rendered block tells the judge how many rows carry full content."""
    message = _build_user_message(
        scope=SCOPE,
        stratum=STRATUM,
        parent_summary=None,
        current_summary=None,
        recent_contributions=[RECENT_ROW],
        new_contribution=NEW_CONTRIBUTION,
        window_verbatim_tail=2,
    )

    assert "RECENT CONTRIBUTIONS (oldest first — mechanical digest; the newest 2 PRIOR" in message
    assert "[c_prev01]" in message
    assert "state=judged" in message


def test_user_message_renders_the_judged_contribution_as_a_digest_row() -> None:
    """The wiring check: the self row is excerpted in the block, whole below it.

    ``_build_user_message`` must pass the new contribution's id to the renderer
    — without it the row being judged would silently claim a verbatim slot and
    the prompt would carry its text twice.
    """
    long_content = "q" * (WINDOW_CONTENT_PREFIX_CHARS + 300)
    judged = Contribution(
        id="c_under_judgment",
        scope_id=SCOPE.id,
        content=long_content,
        proposed_classification="context",
        subject="topic",
        supersedes=None,
        contributor=CONTRIBUTOR,
        created_at="2026-05-01T10:00:00+00:00",
    )
    message = _build_user_message(
        scope=SCOPE,
        stratum=STRATUM,
        parent_summary=None,
        current_summary=None,
        # The window contains the contribution under judgment, as it always
        # does: it is appended to the record before the window is read.
        recent_contributions=[RECENT_ROW, _row(judged, state="pending")],
        new_contribution=judged,
    )
    digest_block, new_contribution_block = message.split("NEW CONTRIBUTION TO JUDGE:")

    assert "[c_under_judgment]" in digest_block
    assert WINDOW_TRUNCATION_MARKER in digest_block
    assert long_content not in digest_block
    # The full text is in the prompt exactly once, where it belongs.
    assert long_content in new_contribution_block


def test_system_prompt_describes_the_digest_and_its_purpose() -> None:
    """The judge is told what a row is, that the newest are verbatim, and what it is for."""
    assert "MECHANICAL DIGEST" in _SYSTEM_PROMPT
    assert "RECENCY CHECKS" in _SYSTEM_PROMPT
    assert "judge_failed" in _SYSTEM_PROMPT
    assert "truncation marker" in _SYSTEM_PROMPT


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
        recent_contributions=[RECENT_ROW],
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
        recent_contributions=[RECENT_ROW],
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
        recent_contributions=[RECENT_ROW],
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
    """Echoing operator-consistent material must be attributed in the summary.

    The obligation lives in RULE 2 of the amendment contract (promoted there
    after an eval pass measured 0.0 on attribution while the rule sat in a
    paragraph judges never registered), so the pins follow it to its new home.
    """
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert "per operator directive <id>" in flat
    assert "RULE 2 — EVERY OPERATOR ECHO CARRIES ITS ATTRIBUTION" in flat
    assert "an unattributed echo masquerades as native scope memory" in flat


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
# ADR 0008 D3 — the unattributed-echo corrective: an accept whose reasoning
# cites an operator directive it never attributes in the admitted text
# ---------------------------------------------------------------------------

OPERATOR_MEMORY = [("g_exec", [OPERATOR_DIRECTIVE])]

_CITING_REASONING = f"Consistent with operator directive {OPERATOR_DIRECTIVE.id}; recording it."

ATTRIBUTED_CONTEXT = (
    f"Services stay on TLS 1.3 or later — per operator directive {OPERATOR_DIRECTIVE.id}."
)

UNATTRIBUTED_CONTEXT = "Services stay on TLS 1.3 or later."


def _echo_input(
    *,
    context: str,
    decision: str = "accept_as_context",
    reasoning: str = _CITING_REASONING,
    ops: list[dict] | None = None,
) -> dict:
    """An accept payload whose reasoning cites the operator directive."""
    return {
        "decision": decision,
        "reasoning": reasoning,
        "directive_ops": ops or [],
        "new_context": context,
    }


def _judge_with_operator_memory(
    mock_client: MagicMock,
    *,
    summary_max_words: int = 500,
) -> ScopeManagerJudgment:
    return ScopeManager(client=mock_client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        operator_memory=OPERATOR_MEMORY,
        summary_max_words=summary_max_words,
    )


def test_unattributed_echo_triggers_one_corrective_naming_the_operator_directive() -> None:
    """O3: reasoning cites the operator directive, admitted text does not — one re-ask."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(_echo_input(context=UNATTRIBUTED_CONTEXT)),
        _fake_response(_echo_input(context=ATTRIBUTED_CONTEXT)),
    ]

    judgment = _judge_with_operator_memory(mock_client)

    assert mock_client.messages.create.call_count == 2
    followup = mock_client.messages.create.call_args_list[1].kwargs["messages"][-1]
    text = [b for b in followup["content"] if b["type"] == "text"][0]["text"]
    assert OPERATOR_DIRECTIVE.id in text
    assert "per operator directive <id>" in text
    assert "RULE 2" in text
    # Not one of the other correctives' wordings.
    assert "could not be parsed" not in text
    assert "BUDGET" not in text

    # The attributed rewrite is what lands in the summary.
    assert judgment.new_summary is not None
    assert judgment.new_summary.context == ATTRIBUTED_CONTEXT


def test_unattributed_echo_corrective_failure_keeps_the_first_judgment() -> None:
    """Keep-first: a retry that cannot be had never costs the contribution its verdict."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(_echo_input(context=UNATTRIBUTED_CONTEXT)),
        RuntimeError("api unavailable"),
    ]

    judgment = _judge_with_operator_memory(mock_client)

    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_context"
    assert judgment.new_summary is not None
    assert judgment.new_summary.context == UNATTRIBUTED_CONTEXT


def test_unattributed_echo_corrective_may_not_flip_the_verdict() -> None:
    """An attribution re-ask corrects text only: a retry changing the decision is discarded."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(_echo_input(context=UNATTRIBUTED_CONTEXT)),
        _fake_response(_echo_input(context=ATTRIBUTED_CONTEXT, decision="accept_as_directive")),
    ]

    judgment = _judge_with_operator_memory(mock_client)

    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_context"
    assert judgment.new_summary is not None
    assert judgment.new_summary.context == UNATTRIBUTED_CONTEXT


def test_attributed_echo_makes_exactly_one_call() -> None:
    """The attribution is already in the authored text — nothing to correct."""
    manager, mock_client = _make_manager(_echo_input(context=ATTRIBUTED_CONTEXT))

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        operator_memory=OPERATOR_MEMORY,
    )

    assert mock_client.messages.create.call_count == 1
    assert judgment.new_summary is not None
    assert judgment.new_summary.context == ATTRIBUTED_CONTEXT


def test_decline_citing_an_operator_directive_gets_no_corrective() -> None:
    """A decline admits nothing, so there is no echo to attribute."""
    manager, mock_client = _make_manager(
        {
            "decision": "decline",
            "reasoning": (
                f"Contradicts operator directive {OPERATOR_DIRECTIVE.id}, which binds this scope."
            ),
            "directive_ops": [],
            "new_context": None,
        }
    )

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        operator_memory=OPERATOR_MEMORY,
    )

    assert mock_client.messages.create.call_count == 1
    assert judgment.decision == "decline"
    assert judgment.new_summary is None


def test_no_operator_memory_rendered_gets_no_corrective() -> None:
    """Nothing was rendered, so an id-shaped string in the reasoning proves nothing."""
    manager, mock_client = _make_manager(_echo_input(context=UNATTRIBUTED_CONTEXT))

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )

    assert mock_client.messages.create.call_count == 1
    assert judgment.new_summary is not None
    assert judgment.new_summary.context == UNATTRIBUTED_CONTEXT


def test_appended_contribution_bytes_carrying_the_attribution_get_no_corrective() -> None:
    """An `append` admits the contribution's own bytes — those count as admitted text."""
    attributed_contribution = _contribution(
        "c_att1",
        f"Services stay on TLS 1.3 or later, per operator directive {OPERATOR_DIRECTIVE.id}.",
        subject="tls",
    )
    manager, mock_client = _make_manager(
        {
            "decision": "accept_as_directive",
            "reasoning": _CITING_REASONING,
            "directive_ops": [{"op": "append"}],
            "new_context": None,
        }
    )

    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=attributed_contribution,
        operator_memory=OPERATOR_MEMORY,
    )

    assert mock_client.messages.create.call_count == 1
    assert judgment.new_summary is not None
    assert judgment.new_summary.directives[-1].content == attributed_contribution.content


def test_attribution_corrective_rewrite_is_still_budget_checked() -> None:
    """Ordering: the attribution re-ask runs first, so its rewrite can still overflow."""
    over_budget_context = ATTRIBUTED_CONTEXT + " " + " ".join(f"word{i}" for i in range(40))
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(_echo_input(context="Attribution pending here.")),
        _fake_response(_echo_input(context=over_budget_context)),
        _fake_response(_echo_input(context=ATTRIBUTED_CONTEXT)),
    ]

    # EXISTING_DIRECTIVE is 5 words: the first and third contexts fit, the
    # attribution rewrite does not.
    judgment = _judge_with_operator_memory(mock_client, summary_max_words=20)

    assert mock_client.messages.create.call_count == 3
    attribution_followup = mock_client.messages.create.call_args_list[1].kwargs["messages"][-1]
    attribution_text = [b for b in attribution_followup["content"] if b["type"] == "text"][0][
        "text"
    ]
    assert OPERATOR_DIRECTIVE.id in attribution_text

    overflow_messages = mock_client.messages.create.call_args_list[2].kwargs["messages"]
    overflow_text = [b for b in overflow_messages[-1]["content"] if b["type"] == "text"][0]["text"]
    assert "BUDGET" in overflow_text
    # The overflow follow-up chains onto the attribution turn, not the first.
    assert len(overflow_messages) == 5

    assert judgment.new_summary is not None
    assert judgment.new_summary.context == ATTRIBUTED_CONTEXT


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
    """The decision rule, with the publish-reason obligation stated as a MUST.

    "say why in your reasoning" measured as present in only one publish out of
    three; the rule now names the exact sentence a publish reasoning owes.
    """
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert (
        "APPEND unless the binding text must differ from the contribution's text; "
        "if it must, PUBLISH — and your reasoning MUST name why the contribution's "
        "own bytes could not serve as the directive text" in flat
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


# ---------------------------------------------------------------------------
# ADR 0011 D1 — prompt hardening against the four obligations the first
# A-suite pass measured leaking. Machinery scored 1.0 throughout; these are
# the PROMPT-borne rules, so the contract is pinned on the prompt text.
# ---------------------------------------------------------------------------


def test_system_prompt_promotes_operator_echo_attribution_to_a_top_level_rule() -> None:
    """Target 1 (measured 0.0): attribution is a numbered rule, worked and named.

    Two things the buried paragraph lacked: a concrete echo sentence with the
    attribution written in, and the failure mode said plainly. The ADR 0008 D2
    never-copy rule sits immediately beside it, equally prominent.
    """
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert "RULE 1 — NEVER COPY AN OPERATOR DIRECTIVE (ADR 0008 D2)" in flat
    assert "RULE 2 — EVERY OPERATOR ECHO CARRIES ITS ATTRIBUTION" in flat
    # The worked micro-example: an echo sentence carrying its attribution.
    assert (
        '"Deploy freezes remain in effect through Q3 — per operator directive op_1a2b3c4d."' in flat
    )
    # The failure mode, stated plainly.
    assert "an unattributed echo masquerades as native scope memory" in flat
    # Reasoning is not a place to put attribution — only authored summary text is.
    assert "Citing the id in your reasoning does NOT satisfy this" in flat
    # Both rules are adjacent, and both land before the OPERATOR MEMORY block
    # that used to carry the obligation in prose.
    rule_1 = _SYSTEM_PROMPT.index("RULE 1 — NEVER COPY AN OPERATOR DIRECTIVE")
    rule_2 = _SYSTEM_PROMPT.index("RULE 2 — EVERY OPERATOR ECHO CARRIES ITS ATTRIBUTION")
    operator_block = _SYSTEM_PROMPT.index("When an OPERATOR MEMORY section is present")
    assert rule_1 < rule_2 < operator_block


def test_judge_tools_require_the_publish_reason_in_reasoning() -> None:
    """Target 2 (measured 1/3): the reasoning FIELD carries the publish rule too.

    Both places a model reads — prompt and schema. The batch tool writes its own
    per-verdict ``reasoning`` (``JUDGE_TOOL``'s is popped in the derivation), so
    both descriptions are checked here or the two can silently drift.
    """
    rule = (
        "When any op is `publish`, this must state why the contribution's own "
        "bytes could not serve as the directive text."
    )
    single = JUDGE_TOOL["input_schema"]["properties"]["reasoning"]["description"]
    batch = JUDGE_BATCH_TOOL["input_schema"]["properties"]["verdicts"]["items"]["properties"][
        "reasoning"
    ]["description"]
    assert rule in single
    assert rule in batch
    assert single.startswith("One or two sentences explaining the verdict.")
    assert batch.startswith("One or two sentences explaining THIS contribution's verdict.")


def test_system_prompt_shows_the_paired_form_of_supersede() -> None:
    """Target 3 (measured 0.5): the prompt prevents the attempt the parse rejects."""
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert (
        "`supersede` NEVER appears alone — it rides in the same amendment as the "
        "`append` or `publish` that replaces the directive it names" in flat
    )
    # The valid shape, inline, so there is nothing to infer.
    assert 'Valid form: [{"op": "supersede", "id": "c_old"}, {"op": "append"}]' in flat
    assert "to remove a directive nothing replaces, use `retire`" in flat


def test_system_prompt_does_not_decline_over_an_unresolvable_supersedes() -> None:
    """Target 4 (measured 0.0, too harsh): judge the content, drop the dead reference.

    A `publish` carrying an unresolvable ``supersedes`` is invalid WHOLE
    (:func:`_op_target_id` reads the field as a removal target), so the prompt
    tells the judge to leave the field off rather than lose the admission.
    """
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert "unresolvable reference is NOT grounds to decline" in flat
    assert "judge the content on its own merits exactly as if it named nothing" in flat
    assert "emit the `append` or `publish` with NO `supersede` op and no `supersedes` field" in flat
    assert "note the unresolvable reference in your reasoning" in flat
    assert "Decline only when the content itself deserves declining" in flat


# ---------------------------------------------------------------------------
# ADR 0011 D3 — batch judgment: one call, per-contribution verdicts
# ---------------------------------------------------------------------------


SECOND_CONTRIBUTION = Contribution(
    id="c_002def",
    scope_id=SCOPE.id,
    content="Integration tests run on every pull request.",
    proposed_classification="directive",
    subject="ci",
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-05-01T10:00:05+00:00",
)

THIRD_CONTRIBUTION = Contribution(
    id="c_003ghi",
    scope_id=SCOPE.id,
    content="Someone else's internal notes, offered here.",
    proposed_classification="context",
    subject=None,
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-05-01T10:00:09+00:00",
)

BATCH = [NEW_CONTRIBUTION, SECOND_CONTRIBUTION, THIRD_CONTRIBUTION]


def _batch_input(
    *,
    verdicts: list[dict] | None = None,
    directive_ops: list[dict] | None = None,
    new_context: str | None = "Context after the whole batch.",
) -> dict:
    """A ``submit_batch_judgment`` payload accepting the first two, declining the third."""
    if verdicts is None:
        verdicts = [
            {
                "contribution_id": NEW_CONTRIBUTION.id,
                "decision": "accept_as_directive",
                "reasoning": "an enforceable standard",
            },
            {
                "contribution_id": SECOND_CONTRIBUTION.id,
                "decision": "accept_as_directive",
                "reasoning": "also enforceable",
            },
            {
                "contribution_id": THIRD_CONTRIBUTION.id,
                "decision": "decline",
                "reasoning": "material originating outside this scope's entitlement",
            },
        ]
    if directive_ops is None:
        directive_ops = [
            {"op": "append", "contribution_id": NEW_CONTRIBUTION.id},
            {"op": "append", "contribution_id": SECOND_CONTRIBUTION.id},
        ]
    return {
        "verdicts": verdicts,
        "directive_ops": directive_ops,
        "new_context": new_context,
    }


def _judge_batch(mock_client: MagicMock, contributions=BATCH, **kwargs):  # noqa: ANN001, ANN003
    manager = ScopeManager(client=mock_client)
    return manager.judge_batch(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contributions=contributions,
        **kwargs,
    )


# -- tool schema ------------------------------------------------------------


def test_batch_tool_carries_per_contribution_verdicts_and_one_amendment() -> None:
    schema = JUDGE_BATCH_TOOL["input_schema"]
    assert JUDGE_BATCH_TOOL["name"] == "submit_batch_judgment"
    assert schema["required"] == ["verdicts", "directive_ops", "new_context"]
    verdict = schema["properties"]["verdicts"]["items"]
    assert verdict["required"] == ["contribution_id", "decision", "reasoning"]
    assert verdict["properties"]["decision"]["enum"] == [
        "accept_as_directive",
        "accept_as_context",
        "decline",
    ]
    # ONE amendment for the batch — never a summary, never one amendment each.
    assert "new_summary" not in schema["properties"]
    assert schema["properties"]["new_context"]["type"] == ["string", "null"]
    assert "decision" not in schema["properties"]
    assert "reasoning" not in schema["properties"]


def test_batch_tool_ops_carry_contribution_id_and_match_the_single_tool_otherwise() -> None:
    """The ops schema is derived from JUDGE_TOOL, so the two cannot drift."""
    batch_op = JUDGE_BATCH_TOOL["input_schema"]["properties"]["directive_ops"]["items"]
    single_op = JUDGE_TOOL["input_schema"]["properties"]["directive_ops"]["items"]
    # Required on EVERY op: an op that names no batch member would have its
    # record consequences attributed by guesswork.
    assert batch_op["properties"]["contribution_id"]["type"] == "string"
    assert set(batch_op["properties"]) - set(single_op["properties"]) == {"contribution_id"}
    for field, spec in single_op["properties"].items():
        assert batch_op["properties"][field] == spec
    assert batch_op["required"] == [*single_op["required"], "contribution_id"]
    assert single_op["required"] == ["op"]


def test_batch_system_prompt_extends_the_judging_prompt_with_the_batch_rules() -> None:
    assert _BATCH_SYSTEM_PROMPT.startswith(_SYSTEM_PROMPT)
    flat = " ".join(_BATCH_SYSTEM_PROMPT.split())
    assert "Process them SEQUENTIALLY, in the order listed" in flat
    assert "one declined contribution never costs the rest their verdicts" in flat
    assert (
        "EVERY op — `append`, `publish`, `supersede`, `retire` — MUST carry the "
        "`contribution_id` of the batch member that motivated it" in flat
    )
    assert "a guessed attribution would be a permanent lie about provenance" in flat
    assert "call the `submit_batch_judgment` tool exactly once" in flat


def test_batch_system_prompt_carries_the_four_hardened_rules_uncontradicted() -> None:
    """The batch prompt is the judging prompt plus batch mechanics — nothing else.

    The four prompt-hardened obligations reach batch mode because the batch
    prompt PREFIXES the single one; the batch block may add per-op attribution
    to a batch member, but must not relax any of them.
    """
    assert _BATCH_SYSTEM_PROMPT.startswith(_SYSTEM_PROMPT)
    flat = " ".join(_BATCH_SYSTEM_PROMPT.split())
    batch_block = " ".join(_BATCH_SYSTEM_PROMPT[len(_SYSTEM_PROMPT) :].split())
    for rule in (
        "RULE 1 — NEVER COPY AN OPERATOR DIRECTIVE (ADR 0008 D2)",
        "RULE 2 — EVERY OPERATOR ECHO CARRIES ITS ATTRIBUTION",
        "your reasoning MUST name why the contribution's own bytes could not serve",
        "`supersede` NEVER appears alone",
        "unresolvable reference is NOT grounds to decline",
    ):
        assert rule in flat
    # None of the four is RESTATED in the batch block: a second wording is how a
    # contradiction gets in, exactly as a hand-written batch ops schema would let
    # the two tool schemas drift. The batch block adds one thing to the ops
    # contract — which member each op belongs to — and nothing else.
    for restatement in ("APPEND unless", "NEVER appears alone", "per operator directive"):
        assert restatement not in batch_block
    assert "MUST carry the `contribution_id`" in batch_block


# -- the batch user message -------------------------------------------------


def test_batch_user_message_lists_the_contributions_in_arrival_order() -> None:
    message = _build_batch_user_message(
        scope=SCOPE,
        stratum=STRATUM,
        parent_summary=None,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contributions=BATCH,
    )
    assert "NEW CONTRIBUTIONS TO JUDGE (3, in arrival order" in message
    positions = [message.index(f"CONTRIBUTION {i} OF 3:") for i in (1, 2, 3)]
    assert positions == sorted(positions)
    for contribution in BATCH:
        assert f"- id: {contribution.id}" in message
        assert contribution.content in message
    assert message.rstrip().endswith("Call `submit_batch_judgment` exactly once.")


def test_batch_members_never_take_a_verbatim_window_slot() -> None:
    """Every contribution under judgment renders as a digest row (ADR 0011 D2/D3)."""
    rows = [
        RecentContribution(contribution=c, state="pending", decision=None, judgment_notes=None)
        for c in BATCH
    ]
    message = _build_batch_user_message(
        scope=SCOPE,
        stratum=STRATUM,
        parent_summary=None,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=rows,
        new_contributions=BATCH,
        window_verbatim_tail=3,
    )
    window = message.split("RECENT CONTRIBUTIONS")[1].split("NEW CONTRIBUTIONS")[0]
    # The digest rows quote a fixed-length excerpt; the full text appears only
    # in the contributions block below.
    for contribution in BATCH:
        assert f"[{contribution.id}]" in window
    assert (
        window.count(NEW_CONTRIBUTION.content) == 0
        or len(NEW_CONTRIBUTION.content) <= WINDOW_CONTENT_PREFIX_CHARS
    )


# -- one call, per-contribution verdicts ------------------------------------


def test_batch_returns_one_verdict_per_contribution_and_one_amendment() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(_batch_input())

    judgment = _judge_batch(mock_client)

    assert mock_client.messages.create.call_count == 1
    assert [(v.contribution_id, v.decision) for v in judgment.verdicts] == [
        (NEW_CONTRIBUTION.id, "accept_as_directive"),
        (SECOND_CONTRIBUTION.id, "accept_as_directive"),
        (THIRD_CONTRIBUTION.id, "decline"),
    ]
    # ONE amended summary: the pre-existing directive untouched, both accepted
    # contributions admitted from their own bytes, the declined one absent.
    assert judgment.new_summary is not None
    assert [d.id for d in judgment.new_summary.directives] == [
        EXISTING_DIRECTIVE.id,
        NEW_CONTRIBUTION.id,
        SECOND_CONTRIBUTION.id,
    ]
    assert judgment.new_summary.directives[0] == EXISTING_DIRECTIVE
    assert judgment.new_summary.directives[1].content == NEW_CONTRIBUTION.content
    assert judgment.new_summary.directives[2].content == SECOND_CONTRIBUTION.content
    assert judgment.new_summary.context == "Context after the whole batch."


def test_batch_uses_the_batch_tool_and_scales_max_tokens_with_the_batch() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(_batch_input())

    _judge_batch(mock_client)

    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["tool_choice"]["name"] == "submit_batch_judgment"
    assert kwargs["tools"][0]["name"] == "submit_batch_judgment"
    assert kwargs["system"][0]["text"] == _BATCH_SYSTEM_PROMPT
    # Base budget for the first contribution, an increment for each extra.
    assert kwargs["max_tokens"] == _batch_max_tokens(3)
    assert _batch_max_tokens(1) == JUDGE_MAX_TOKENS
    assert _batch_max_tokens(5) == JUDGE_MAX_TOKENS + 4 * JUDGE_BATCH_MAX_TOKENS_PER_EXTRA


def test_declined_member_does_not_poison_the_batch() -> None:
    """Each verdict stands on its own; the decline admits nothing and blocks nothing."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(_batch_input())

    judgment = _judge_batch(mock_client)

    declined = judgment.verdicts[-1]
    assert declined.decision == "decline"
    assert declined.reasoning == "material originating outside this scope's entitlement"
    assert [v.contribution_id for v in judgment.accepted_verdicts] == [
        NEW_CONTRIBUTION.id,
        SECOND_CONTRIBUTION.id,
    ]
    assert THIRD_CONTRIBUTION.id not in [d.id for d in judgment.new_summary.directives]
    # Each accepted member's own reasoning is readable by id; the declined one
    # owns nothing in the amendment.
    assert judgment.verdict_reasoning(NEW_CONTRIBUTION.id) == "an enforceable standard"
    assert judgment.verdict_reasoning(SECOND_CONTRIBUTION.id) == "also enforceable"
    assert judgment.batch_reasoning == (
        f"[{NEW_CONTRIBUTION.id}] an enforceable standard; "
        f"[{SECOND_CONTRIBUTION.id}] also enforceable"
    )


def test_batch_of_one_is_exactly_the_single_call() -> None:
    """A batch of one takes the single path — same tool, same prompt, same budget."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(_accept_directive_input())

    judgment = _judge_batch(mock_client, contributions=[NEW_CONTRIBUTION])

    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["tool_choice"]["name"] == "submit_judgment"
    assert kwargs["tools"][0]["name"] == "submit_judgment"
    assert kwargs["system"][0]["text"] == _SYSTEM_PROMPT
    assert kwargs["max_tokens"] == JUDGE_MAX_TOKENS
    assert "NEW CONTRIBUTION TO JUDGE" in kwargs["messages"][0]["content"]
    assert "submit_batch_judgment" not in kwargs["messages"][0]["content"]

    # ...wrapped in the batch shape, so one caller can drive both modes.
    assert [(v.contribution_id, v.decision) for v in judgment.verdicts] == [
        (NEW_CONTRIBUTION.id, "accept_as_directive")
    ]
    assert judgment.record_notes_for(NEW_CONTRIBUTION.id) == judgment.verdicts[0].reasoning
    assert [d.id for d in judgment.new_summary.directives] == [
        EXISTING_DIRECTIVE.id,
        NEW_CONTRIBUTION.id,
    ]


def test_empty_batch_is_rejected() -> None:
    mock_client = MagicMock()
    with pytest.raises(ValueError, match="at least one contribution"):
        _judge_batch(mock_client, contributions=[])


# -- publish attribution ----------------------------------------------------


def test_publish_op_admits_the_contribution_its_contribution_id_names() -> None:
    """The binding applies to the named contribution, not to the batch's first."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        _batch_input(
            verdicts=[
                {
                    "contribution_id": NEW_CONTRIBUTION.id,
                    "decision": "accept_as_context",
                    "reasoning": "informative only",
                },
                {
                    "contribution_id": SECOND_CONTRIBUTION.id,
                    "decision": "accept_as_directive",
                    "reasoning": "ratified in this scope's words",
                },
                {
                    "contribution_id": THIRD_CONTRIBUTION.id,
                    "decision": "decline",
                    "reasoning": "not entitled",
                },
            ],
            directive_ops=[
                {
                    "op": "publish",
                    "contribution_id": SECOND_CONTRIBUTION.id,
                    "content": "CI runs the integration suite on every pull request.",
                    "subject": "ci-policy",
                }
            ],
        )
    )

    judgment = _judge_batch(mock_client)

    admitted = judgment.new_summary.directives[-1]
    assert admitted.id == SECOND_CONTRIBUTION.id
    assert admitted.content == "CI runs the integration suite on every pull request."
    assert admitted.subject == "ci-policy"
    assert admitted.created_at == SECOND_CONTRIBUTION.created_at


# -- correctives ------------------------------------------------------------


def test_missing_verdict_triggers_one_parse_reask() -> None:
    """Every contribution needs a verdict; a missing one is a parse failure."""
    incomplete = _batch_input(
        verdicts=[
            {
                "contribution_id": NEW_CONTRIBUTION.id,
                "decision": "accept_as_directive",
                "reasoning": "fine",
            }
        ],
        directive_ops=[{"op": "append", "contribution_id": NEW_CONTRIBUTION.id}],
    )
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(incomplete),
        _fake_response(_batch_input()),
    ]

    judgment = _judge_batch(mock_client)

    assert mock_client.messages.create.call_count == 2
    followup = mock_client.messages.create.call_args_list[1].kwargs["messages"][-1]
    text = [b for b in followup["content"] if b["type"] == "text"][0]["text"]
    assert "could not be parsed" in text
    assert "no verdict for" in text
    assert SECOND_CONTRIBUTION.id in text
    assert len(judgment.verdicts) == 3


def test_second_parse_failure_propagates_never_a_second_retry() -> None:
    """The #113 one-retry discipline holds in batch mode too."""
    broken = _batch_input(verdicts=[])
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [_fake_response(broken), _fake_response(broken)]

    with pytest.raises(ValueError, match="no verdict for"):
        _judge_batch(mock_client)

    assert mock_client.messages.create.call_count == 2


def test_op_admitting_a_declined_contribution_is_a_parse_failure() -> None:
    """An admitting op that contradicts its own verdict never applies."""
    contradictory = _batch_input(
        directive_ops=[{"op": "append", "contribution_id": THIRD_CONTRIBUTION.id}]
    )
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(contradictory),
        _fake_response(_batch_input()),
    ]

    judgment = _judge_batch(mock_client)

    text = [
        b
        for b in mock_client.messages.create.call_args_list[1].kwargs["messages"][-1]["content"]
        if b["type"] == "text"
    ][0]["text"]
    assert "append op attributed to contribution" in text
    assert "which this batch declined" in text
    assert [d.id for d in judgment.new_summary.directives] == [
        EXISTING_DIRECTIVE.id,
        NEW_CONTRIBUTION.id,
        SECOND_CONTRIBUTION.id,
    ]


def test_missing_contribution_id_gets_one_corrective_then_drop_and_note() -> None:
    """An op naming no batch member is invalid — one corrective, then dropped."""
    unattributed = _batch_input(
        directive_ops=[
            {"op": "append", "contribution_id": NEW_CONTRIBUTION.id},
            {"op": "append"},
        ]
    )
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(unattributed),
        _fake_response(unattributed),
    ]

    judgment = _judge_batch(mock_client)

    assert mock_client.messages.create.call_count == 2
    text = [
        b
        for b in mock_client.messages.create.call_args_list[1].kwargs["messages"][-1]["content"]
        if b["type"] == "text"
    ][0]["text"]
    assert "EVERY op needs a `contribution_id` naming the batch member" in text
    assert NEW_CONTRIBUTION.id in text
    # The verdicts survive; only the unattributed op is gone.
    assert [v.decision for v in judgment.verdicts] == [
        "accept_as_directive",
        "accept_as_directive",
        "decline",
    ]
    assert judgment.dropped_ops == ["append"]
    assert [d.id for d in judgment.new_summary.directives] == [
        EXISTING_DIRECTIVE.id,
        NEW_CONTRIBUTION.id,
    ]


def test_lifecycle_op_without_a_contribution_id_is_dropped_too() -> None:
    """The requirement covers retire and supersede, not just the admitting ops."""
    unattributed_retire = _batch_input(
        directive_ops=[
            {"op": "append", "contribution_id": NEW_CONTRIBUTION.id},
            {"op": "retire", "id": EXISTING_DIRECTIVE.id},
        ]
    )
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(unattributed_retire),
        _fake_response(unattributed_retire),
    ]

    judgment = _judge_batch(mock_client)

    assert mock_client.messages.create.call_count == 2
    # The retire never applied, so the directive stays and NO Retirement is owed.
    assert judgment.retired_directive_ids == []
    assert judgment.dropped_ops == [f"retire({EXISTING_DIRECTIVE.id})"]
    assert EXISTING_DIRECTIVE.id in [d.id for d in judgment.new_summary.directives]
    # The drop is noted on every accepted member — no member is falsely named
    # as the owner of an op that named none.
    for contribution_id in (NEW_CONTRIBUTION.id, SECOND_CONTRIBUTION.id):
        assert f"retire({EXISTING_DIRECTIVE.id})" in judgment.record_notes_for(contribution_id)


def test_lifecycle_op_attributed_to_a_declined_member_is_a_parse_failure() -> None:
    """No op belongs to a contribution the batch declined — retire included."""
    contradictory = _batch_input(
        directive_ops=[
            {"op": "append", "contribution_id": NEW_CONTRIBUTION.id},
            {
                "op": "retire",
                "id": EXISTING_DIRECTIVE.id,
                "contribution_id": THIRD_CONTRIBUTION.id,
            },
        ]
    )
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(contradictory),
        _fake_response(_batch_input()),
    ]

    judgment = _judge_batch(mock_client)

    text = [
        b
        for b in mock_client.messages.create.call_args_list[1].kwargs["messages"][-1]["content"]
        if b["type"] == "text"
    ][0]["text"]
    assert "retire op attributed to contribution" in text
    assert "which this batch declined" in text
    assert judgment.dropped_ops == []


def test_unknown_contribution_id_gets_one_corrective_then_drop_and_note() -> None:
    """J5 in batch shape: a bad id costs its op, never anyone's verdict."""
    ghost = _batch_input(
        directive_ops=[
            {"op": "append", "contribution_id": NEW_CONTRIBUTION.id},
            {"op": "append", "contribution_id": "c_not_in_this_batch"},
        ]
    )
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [_fake_response(ghost), _fake_response(ghost)]

    judgment = _judge_batch(mock_client)

    # Exactly one corrective, listing both id spaces.
    assert mock_client.messages.create.call_count == 2
    text = [
        b
        for b in mock_client.messages.create.call_args_list[1].kwargs["messages"][-1]["content"]
        if b["type"] == "text"
    ][0]["text"]
    assert "c_not_in_this_batch" in text
    assert "EVERY op needs a `contribution_id` naming the batch member" in text
    assert EXISTING_DIRECTIVE.id in text

    # Every verdict survives; only the bad op is gone.
    assert [v.decision for v in judgment.verdicts] == [
        "accept_as_directive",
        "accept_as_directive",
        "decline",
    ]
    assert judgment.dropped_ops == ["append(contribution=c_not_in_this_batch)"]
    assert [d.id for d in judgment.new_summary.directives] == [
        EXISTING_DIRECTIVE.id,
        NEW_CONTRIBUTION.id,
    ]
    # The op named no member of this batch, so its note goes to every accepted
    # member rather than falsely naming one of them as its owner.
    for contribution_id, reasoning in (
        (NEW_CONTRIBUTION.id, "an enforceable standard"),
        (SECOND_CONTRIBUTION.id, "also enforceable"),
    ):
        notes = judgment.record_notes_for(contribution_id)
        assert notes.startswith(reasoning)
        assert "append(contribution=c_not_in_this_batch)" in notes
    # The declined member's row carries no amendment note at all.
    assert judgment.record_notes_for(THIRD_CONTRIBUTION.id) == (
        "material originating outside this scope's entitlement"
    )


def test_invalid_directive_id_in_a_batch_is_dropped_and_noted_on_its_own_op() -> None:
    """A retire naming an unknown directive: one corrective, then drop-and-note."""
    ghost_retire = _batch_input(
        directive_ops=[
            {"op": "append", "contribution_id": NEW_CONTRIBUTION.id},
            {"op": "retire", "id": "c_ghost", "contribution_id": SECOND_CONTRIBUTION.id},
        ]
    )
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(ghost_retire),
        _fake_response(ghost_retire),
    ]

    judgment = _judge_batch(mock_client)

    dropped = f"retire(c_ghost, contribution={SECOND_CONTRIBUTION.id})"
    assert judgment.dropped_ops == [dropped]
    assert judgment.retired_directive_ids == []
    assert [d.id for d in judgment.new_summary.directives] == [
        EXISTING_DIRECTIVE.id,
        NEW_CONTRIBUTION.id,
    ]
    # The op named its member, so the note lands on that member's row alone.
    assert dropped in judgment.record_notes_for(SECOND_CONTRIBUTION.id)
    assert judgment.record_notes_for(NEW_CONTRIBUTION.id) == "an enforceable standard"


def test_batch_overflow_triggers_one_corrective_naming_the_batch_tool() -> None:
    """The #63 re-ask applies to the batch amendment, with the same one retry."""
    long_context = " ".join(f"word{i}" for i in range(60))
    over = _batch_input(new_context=long_context)
    fits = _batch_input(new_context="Short.")
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [_fake_response(over), _fake_response(fits)]

    judgment = _judge_batch(mock_client, summary_max_words=50)

    assert mock_client.messages.create.call_count == 2
    text = [
        b
        for b in mock_client.messages.create.call_args_list[1].kwargs["messages"][-1]["content"]
        if b["type"] == "text"
    ][0]["text"]
    assert "over the BUDGET of 50 words" in text
    assert "Call submit_batch_judgment again with the SAME decisions" in text
    assert judgment.new_summary.context == "Short."


def test_all_declined_batch_amends_nothing() -> None:
    """A batch that declines everything writes no amendment — as one decline does."""
    all_declined = _batch_input(
        verdicts=[
            {"contribution_id": c.id, "decision": "decline", "reasoning": "no"} for c in BATCH
        ],
        directive_ops=[],
        new_context=None,
    )
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(all_declined)

    judgment = _judge_batch(mock_client)

    assert judgment.new_summary is None
    assert judgment.accepted_verdicts == []
    assert judgment.batch_reasoning == ""
    assert mock_client.messages.create.call_count == 1


def test_all_declined_batch_carrying_an_amendment_is_a_parse_failure() -> None:
    contradictory = _batch_input(
        verdicts=[
            {"contribution_id": c.id, "decision": "decline", "reasoning": "no"} for c in BATCH
        ],
        directive_ops=[],
        new_context="but here is a rewrite anyway",
    )
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(contradictory),
        _fake_response(contradictory),
    ]

    with pytest.raises(ValueError, match="declined every contribution"):
        _judge_batch(mock_client)


def test_batch_verdicts_are_returned_in_arrival_order_however_they_came_back() -> None:
    """The record's order is arrival order; the payload's ordering adds nothing."""
    shuffled = _batch_input()
    shuffled["verdicts"] = list(reversed(shuffled["verdicts"]))
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(shuffled)

    judgment = _judge_batch(mock_client)

    assert [v.contribution_id for v in judgment.verdicts] == [c.id for c in BATCH]


def test_batch_stringified_payload_is_coerced_like_the_single_path() -> None:
    """Issue #113's stringification failure mode reaches the batch fields too."""
    payload = _batch_input()
    stringified = {
        "verdicts": json.dumps(payload["verdicts"]),
        "directive_ops": json.dumps(payload["directive_ops"]),
        "new_context": payload["new_context"],
    }
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(stringified)

    judgment = _judge_batch(mock_client)

    assert mock_client.messages.create.call_count == 1
    assert [v.contribution_id for v in judgment.verdicts] == [c.id for c in BATCH]
    assert [d.id for d in judgment.new_summary.directives] == [
        EXISTING_DIRECTIVE.id,
        NEW_CONTRIBUTION.id,
        SECOND_CONTRIBUTION.id,
    ]
