"""1.18.3: the engine's own decline wording never reaches the next judge.

The recency digest renders each earlier verdict's notes. The engine writes suffixes onto those
notes ("[Declined: ...]", "[inherited relation: ...]"); a judge that reads them on a same-subject
note a moment earlier copies them into its own reasoning, and the false decline cascades. The
digest now shows the judge's own reasoning only. The API's idempotent re-contribute still returns
the engine's account.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from strata.record_store import RecentContribution
from strata.scope_manager import (
    ScopeManager,
    strip_engine_notes,
    strip_notes_for_judge,
    strip_record_only_notes,
)
from tests.test_scope_manager import (
    CURRENT_SUMMARY,
    NEW_CONTRIBUTION,
    RECENT_CONTRIBUTION,
    SCOPE,
    STRATUM,
    _fake_response,
)

JUDGE_SENTENCE = (
    "The contribution is a first-hand observation of the contributor's own work on backend "
    "development. It directly extends the scope's existing memory."
)
DECLINED = (
    "[Declined: contrary to inherited directive op_rule (operator, g_arch). A practice that "
    "departs from an inherited rule can't be recorded as this scope's context. Report a specific "
    "occurrence of following the rule and what happened (admitted, and raised to operator, g_arch "
    "with acted_on), or propose the exception to operator, g_arch.]"
)
TRACE = (
    "[inherited relation: decline, answered unrelated; covers an inherited subject and "
    "(normative marker 'exception')]"
)
NOTES_1 = f"[decline] {JUDGE_SENTENCE} {DECLINED} {TRACE}"
PROVENANCE = (
    " [Engine: same-scope change by lead (session sess_1), bound to g_abc123: append c_1. This "
    "line, not the reasoning above, is the record's account of who changed the directives.]"
)


def test_the_engine_decline_and_trace_are_stripped_and_the_judges_sentence_is_kept():
    assert strip_notes_for_judge(NOTES_1) == JUDGE_SENTENCE


@pytest.mark.parametrize(
    "suffix",
    [
        "[A report of following op_rule (g_arch). To raise it to g_arch, resubmit with acted_on.]",
        "[A departure from op_rule (g_arch), not a licence: only g_arch decides whether the rule "
        "is slack. To raise it to g_arch, resubmit with acted_on = op_rule.]",
        "[Recorded as a departure from op_rule (this scope): not a licence; to change the rule, "
        "supersede or retire it.]",
        "[Declined: a report of following or departing from op_rule (g_arch) must name the "
        "specific occurrence (when, which); resubmit with it, or propose the exception to g_arch.]",
        "[directive relation: hold, answered tightens; no engine evidence]",
        "[Engine: a context note can't change op_rule; supersede or retire it explicitly. Ops "
        "dropped: supersede op_rule.]",
    ],
)
def test_each_engine_written_opener_is_stripped(suffix):
    assert strip_notes_for_judge(f"[decline] {JUDGE_SENTENCE} {suffix}") == JUDGE_SENTENCE


def test_the_inherited_hold_note_the_engine_writes_whole_is_stripped_to_nothing():
    note = (
        "[decline] Contradicts inherited op_rule (g_arch). An exception to it is g_arch's to "
        "make; propose it there."
    )
    assert strip_notes_for_judge(note) == ""
    held = (
        "[accept_as_context; no ops] Held as context: the judge reads this as an exception to "
        "op_rule (g_arch); the engine could not confirm a conflict. Context never binds."
    )
    assert strip_notes_for_judge(held) == ""


@pytest.mark.parametrize(
    "reasoning",
    [
        'the earlier note said "[Declined: contrary to inherited x" but this one differs.',
        "I saw [inherited relation: decline] in the log; this note is different.",
        "Quoting the engine: [Declined: contrary to inherited directive x (g_arch).] That was "
        "about another note.",
        "See [the design doc] for the reasoning.",
    ],
)
def test_a_judge_reasoning_that_merely_contains_an_opener_is_kept_byte_for_byte(reasoning):
    assert strip_notes_for_judge(f"[decline] {reasoning}") == reasoning
    assert strip_engine_notes(reasoning) == reasoning


def test_the_held_note_and_other_trailing_groups_stay():
    held = (
        " [Held: would change directive c_x, which stands. The contributor is not bound to this "
        "scope, so this is admitted as an attributed proposal; a session bound to this scope may "
        "adopt it.]"
    )
    notes = f"[accept_as_context; no ops] {JUDGE_SENTENCE} {TRACE}{held}"
    out = strip_notes_for_judge(notes)
    assert TRACE not in out
    assert out.startswith(JUDGE_SENTENCE)
    assert "[Held: would change directive c_x, which stands." in out


def test_the_decision_prefix_and_the_provenance_line_are_still_stripped():
    notes = f"[accept_as_directive; append c_1] {JUDGE_SENTENCE}{PROVENANCE}"
    assert strip_notes_for_judge(notes) == JUDGE_SENTENCE
    assert strip_record_only_notes(notes) == JUDGE_SENTENCE


def test_the_idempotent_api_account_keeps_the_engines_suffixes():
    kept = strip_record_only_notes(NOTES_1)
    assert DECLINED in kept
    assert TRACE in kept


def test_the_next_judge_is_not_shown_the_engines_decline_wording():
    declined = RecentContribution(
        contribution=replace(RECENT_CONTRIBUTION, id="c_first", content="Backend state note."),
        state="judged",
        decision="decline",
        judgment_notes=NOTES_1,
    )
    client = MagicMock()
    client.messages.create.return_value = _fake_response(
        {
            "decision": "accept_as_context",
            "reasoning": "A plain status.",
            "directive_ops": [],
            "new_context": "Status.",
        }
    )
    ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[declined],
        new_contribution=replace(NEW_CONTRIBUTION, proposed_classification="context"),
    )
    # An accept may make further calls (the own-directive relation call); none
    # of them may show the engine's wording, and exactly one carries the window.
    messages = [c.kwargs["messages"][0]["content"] for c in client.messages.create.call_args_list]
    for message in messages:
        assert "contrary to inherited directive" not in message.lower()
        assert "[inherited relation:" not in message
        assert "[Declined:" not in message
    windowed = [m for m in messages if "RECENT CONTRIBUTIONS" in m]
    assert len(windowed) == 1
    message = windowed[0]
    assert "decision=decline" in message  # the row still shows the verdict
    assert "first-hand observation" in message  # and the judge's own sentence
