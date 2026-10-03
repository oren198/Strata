"""Issue #238: the record opens with what the verdict actually did.

A judge's reasoning can describe an act its decision did not take ("Publishing
because ..." on a context accept). Every recorded verdict's notes now open with
an engine-written prefix naming the decision as recorded and the ops as
applied: ``[decline] `` or ``[<decision>; <ops or "no ops">] ``. The prefix is
for the record only: judge inputs strip it, and a judge failure (no verdict)
records exactly what it did before.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from strata.record_store import RecentContribution
from strata.scope_manager import (
    ScopeManager,
    _render_digest_row,
    _with_held_note,
    is_held_note,
    strip_record_only_notes,
)
from tests.test_same_scope_provenance import (
    CHILD,
    FOREIGN,
    _append,
    _contribute,
    client,  # noqa: F401 (fixture)
)
from tests.test_scope_manager import (
    BATCH,
    CURRENT_SUMMARY,
    EXISTING_DIRECTIVE,
    NEW_CONTRIBUTION,
    SCOPE,
    SECOND_CONTRIBUTION,
    STRATUM,
    _batch_input,
    _fake_response,
    _judge_batch,
    _make_manager,
)

_PUBLISHING = "Publishing because the rule is ready for outside readers."


def _judge(tool_input: dict, contribution=NEW_CONTRIBUTION, **kwargs):  # noqa: ANN001, ANN003
    manager, _ = _make_manager(tool_input)
    return manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=contribution,
        **kwargs,
    )


def test_a_context_accept_whose_reasoning_says_publishing_records_what_happened(
    client: TestClient,  # noqa: F811 — the imported fixture
) -> None:
    _cid, notes = _contribute(
        client,
        {
            "decision": "accept_as_context",
            "reasoning": _PUBLISHING,
            "directive_ops": [],
            "new_context": "Retries are bounded.",
        },
    )
    assert notes == f"[accept_as_context; no ops] {_PUBLISHING}"


def test_a_supersede_and_append_list_the_applied_ops() -> None:
    own = replace(NEW_CONTRIBUTION, supersedes=EXISTING_DIRECTIVE.id)
    judgment = _judge(
        {
            "decision": "accept_as_directive",
            "reasoning": "Replaces the naming rule.",
            "directive_ops": [{"op": "supersede", "id": EXISTING_DIRECTIVE.id}, {"op": "append"}],
            "new_context": None,
        },
        own,
    )
    assert judgment.record_notes.startswith(
        f"[accept_as_directive; supersede c_old001→{own.id}; append {own.id}] "
        "Replaces the naming rule. [Engine: same-scope change"
    )


def test_a_decline_opens_with_decline_only() -> None:
    judgment = _judge({"decision": "decline", "reasoning": "Out of scope.", "directive_ops": []})
    assert judgment.record_notes == "[decline] Out of scope."


def test_a_held_foreign_item_says_context_with_no_ops_and_the_held_note_stays_last(
    client: TestClient,  # noqa: F811 — the imported fixture
) -> None:
    _cid, notes = _contribute(client, _append(_PUBLISHING), as_scope=CHILD, skill="x")
    assert notes == "[accept_as_context; no ops] " + _with_held_note(_PUBLISHING, [])
    assert is_held_note(notes)


def test_a_refresh_carries_the_prefix_too() -> None:
    judgment = _judge(
        {
            "decision": "accept_as_context",
            "reasoning": "refreshed",
            "directive_ops": [
                {"op": "retire", "id": EXISTING_DIRECTIVE.id, "changed_circumstance": "gone"}
            ],
            "new_context": "Reconciled.",
        },
        mode="input_change_refresh",
    )
    assert judgment.record_notes == "[accept_as_context; retire c_old001] refreshed"


def test_batch_members_each_list_only_their_own_ops() -> None:
    foreign = replace(SECOND_CONTRIBUTION, contributor=FOREIGN)
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        _batch_input(
            directive_ops=[
                {"op": "append", "contribution_id": NEW_CONTRIBUTION.id},
                {"op": "append", "contribution_id": foreign.id},
            ]
        )
    )
    judgment = _judge_batch(mock_client, contributions=[NEW_CONTRIBUTION, foreign, BATCH[2]])

    assert judgment.record_notes_for(NEW_CONTRIBUTION.id).startswith(
        f"[accept_as_directive; append {NEW_CONTRIBUTION.id}] an enforceable standard [Engine:"
    )
    held = judgment.record_notes_for(foreign.id)
    assert held == "[accept_as_context; no ops] " + _with_held_note("also enforceable", [])
    assert is_held_note(held)
    assert judgment.record_notes_for(BATCH[2].id) == (
        "[decline] material originating outside this scope's entitlement"
    )


def test_a_batch_of_one_lists_its_unattributed_ops_as_its_own() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        {
            "decision": "accept_as_directive",
            "reasoning": "ok",
            "directive_ops": [{"op": "append"}],
            "new_context": None,
        }
    )
    judgment = ScopeManager(client=mock_client).judge_batch(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contributions=[NEW_CONTRIBUTION],
    )
    assert judgment.record_notes_for(NEW_CONTRIBUTION.id).startswith(
        f"[accept_as_directive; append {NEW_CONTRIBUTION.id}] ok [Engine:"
    )


# ---------------------------------------------------------------------------
# Judge failure: no verdict, so no prefix — notes byte-identical to 3180f1f
# ---------------------------------------------------------------------------

_SINGLE_FAILURE_NOTES = (
    "judge failure: the response was still malformed after the corrective re-ask "
    "(_DeclineWithAmendment); declined without a verdict on the merits [Second protocol "
    "slip on the corrective re-ask (Scope-manager returned decision='decline' with an "
    "amendment (directive_ops or new_context). A declined contribution must not amend the "
    "summary.); declined as a judge failure, not a missing-ground decline.]"
)
_BATCH_FAILURE_NOTES = (
    "judge failure: the response was still malformed after the corrective re-ask "
    "(_DeclineWithAmendment); declined without a verdict on the merits [Second protocol "
    "slip on the corrective re-ask (submit_batch_judgment declined every contribution in "
    "the batch but returned an amendment (directive_ops or new_context). Declined "
    "contributions must not amend the summary.); declined as a judge failure, not a "
    "missing-ground decline.]"
)
_DECLINE_WITH_AMENDMENT = {
    "decision": "decline",
    "reasoning": "Declining.",
    "directive_ops": [],
    "new_context": "Should not be here.",
}


def test_a_single_judge_failure_records_exactly_what_it_did_before() -> None:
    judgment = _judge(_DECLINE_WITH_AMENDMENT)
    assert judgment.judge_failure is True
    assert judgment.record_notes == _SINGLE_FAILURE_NOTES


def test_a_batch_judge_failure_records_exactly_what_it_did_before() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        _batch_input(
            verdicts=[
                {"contribution_id": c.id, "decision": "decline", "reasoning": "no"} for c in BATCH
            ],
            directive_ops=[{"op": "append", "contribution_id": BATCH[0].id}],
        )
    )
    judgment = _judge_batch(mock_client)
    for verdict in judgment.verdicts:
        assert verdict.judge_failure is True
        assert judgment.record_notes_for(verdict.contribution_id) == _BATCH_FAILURE_NOTES


def test_a_batch_of_one_judge_failure_records_no_prefix() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(_DECLINE_WITH_AMENDMENT)
    judgment = ScopeManager(client=mock_client).judge_batch(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contributions=[NEW_CONTRIBUTION],
    )
    assert judgment.verdicts[0].judge_failure is True
    assert judgment.record_notes_for(NEW_CONTRIBUTION.id).startswith("judge failure:")


# ---------------------------------------------------------------------------
# Judge inputs never carry the prefix (the window hash pins live in
# tests/test_same_scope_provenance.py and cover the prefix as well)
# ---------------------------------------------------------------------------


def test_the_digest_row_renders_the_notes_as_the_judge_wrote_them() -> None:
    held = _with_held_note(_PUBLISHING, [])
    recorded = f"[accept_as_context; no ops] {held}"
    before = RecentContribution(NEW_CONTRIBUTION, "judged", "accept_as_context", held)
    after = RecentContribution(NEW_CONTRIBUTION, "judged", "accept_as_context", recorded)
    assert _render_digest_row(after, verbatim=True) == _render_digest_row(before, verbatim=True)
    assert _render_digest_row(after, verbatim=False) == _render_digest_row(before, verbatim=False)


def test_strip_removes_both_record_only_parts_and_nothing_else() -> None:
    line = (
        "[Engine: same-scope change by lead (session s1), bound to g_x: append c_1. This line, "
        "not the reasoning above, is the record's account of who changed the directives.]"
    )
    assert strip_record_only_notes(f"[accept_as_directive; append c_1] Ok. {line}") == "Ok."
    assert strip_record_only_notes("[decline] No.") == "No."
    assert strip_record_only_notes("[accept_as_context; no ops] Ctx.") == "Ctx."
    for untouched in ("Plain.", "", "[Held: x] Not a prefix.", "[decline]No space."):
        assert strip_record_only_notes(untouched) == untouched
