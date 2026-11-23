"""#212 backstop 2 (ADR 0016 D4): an item the judge itself labelled "Hearsay:" may not
carry a directive op. The refusal is mechanical, with a protocol note.

Offline: a REAL ``ScopeManager`` with only the Anthropic client mocked. The contribution
names no other scope, so the #225 re-ask never fires: this is the judge's own label,
not the verified informant path (which already drops directive ops).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from strata.fleet_config import EntitlementView, Scope, Stratum
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import ScopeManager
from strata.summary_store import Directive, ScopeSummary

from .test_scope_manager import _fake_response

STRATUM = Stratum(id="L1", name="function", ordinal=1)
SCOPE = Scope(id="g_212self", name="212-self", stratum_id="L1")
CONTRIBUTOR = ContributorRef(
    scope_id="g_212self",
    skill="on-call-engineer",
    session_id="sess_212",
    ts="2026-11-23T10:00:00+00:00",
)
EXISTING = Directive(
    id="c_212old",
    content="Use the shared logging format.",
    subject="logging",
    source_scope_id=SCOPE.id,
    source_skill="architect",
    created_at="2026-09-01T09:00:00+00:00",
)
SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[EXISTING],
    context="The team favours minimal abstractions.",
    updated_at="2026-09-01T09:00:00+00:00",
)
ENTITLEMENT = EntitlementView(chain=[SCOPE], descendants=[], referenced_peers=[], others=[])
TEXT = "Priya, a platform engineer, told me that rollbacks now need two approvals."
HEARSAY = "Hearsay: Priya, a platform engineer, told the agent this; context only."


def _contribution() -> Contribution:
    return Contribution(
        id="c_212new",
        scope_id=SCOPE.id,
        content=TEXT,
        proposed_classification="directive",
        subject=None,
        supersedes=None,
        contributor=CONTRIBUTOR,
        created_at="2026-11-23T09:00:00+00:00",
    )


def _judge(payload: dict):
    client = MagicMock()
    client.messages.create.return_value = _fake_response(payload)
    judgment = ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=SUMMARY,
        recent_contributions=[],
        new_contribution=_contribution(),
        entitlement=ENTITLEMENT,
    )
    return judgment, client


def _notes(judgment) -> list[str]:
    return [n for n in judgment.protocol_notes if "hearsay" in n.lower()]


def test_a_hearsay_labelled_append_is_refused_and_the_item_is_context() -> None:
    judgment, _ = _judge(
        {
            "decision": "accept_as_directive",
            "reasoning": HEARSAY,
            "directive_ops": [{"op": "append"}],
            "new_context": None,
        }
    )
    assert judgment.directive_ops == []
    assert judgment.decision == "accept_as_context"
    assert [d.id for d in judgment.new_summary.directives] == [EXISTING.id]
    assert len(_notes(judgment)) == 1


def test_a_hearsay_labelled_supersede_and_its_append_are_both_refused() -> None:
    judgment, _ = _judge(
        {
            "decision": "accept_as_directive",
            "reasoning": HEARSAY,
            "directive_ops": [{"op": "supersede", "id": EXISTING.id}, {"op": "append"}],
            "new_context": None,
        }
    )
    assert judgment.directive_ops == []
    assert [d.id for d in judgment.new_summary.directives] == [EXISTING.id]
    assert _notes(judgment)


def test_a_hearsay_labelled_retire_is_refused() -> None:
    judgment, _ = _judge(
        {
            "decision": "accept_as_directive",
            "reasoning": HEARSAY,
            "directive_ops": [{"op": "retire", "id": EXISTING.id}],
            "new_context": None,
        }
    )
    assert judgment.directive_ops == []
    assert [d.id for d in judgment.new_summary.directives] == [EXISTING.id]


def test_the_note_names_the_dropped_ops() -> None:
    judgment, _ = _judge(
        {
            "decision": "accept_as_directive",
            "reasoning": HEARSAY,
            "directive_ops": [{"op": "retire", "id": EXISTING.id}],
            "new_context": None,
        }
    )
    assert any(EXISTING.id in n for n in _notes(judgment))


def test_the_same_ops_without_the_hearsay_label_are_untouched() -> None:
    judgment, _ = _judge(
        {
            "decision": "accept_as_directive",
            "reasoning": "The contributor owns this scope and states the rule.",
            "directive_ops": [{"op": "append"}],
            "new_context": None,
        }
    )
    assert [op.op for op in judgment.directive_ops] == ["append"]
    assert judgment.decision == "accept_as_directive"
    assert not _notes(judgment)


def test_a_hearsay_context_item_with_no_ops_is_untouched() -> None:
    judgment, _ = _judge(
        {
            "decision": "accept_as_context",
            "reasoning": HEARSAY,
            "directive_ops": [],
            "new_context": f"{SUMMARY.context} Priya says rollbacks need two approvals.",
        }
    )
    assert judgment.decision == "accept_as_context"
    assert not _notes(judgment)


def test_a_wrong_hearsay_label_is_dropped_first_so_the_ops_stay() -> None:
    """The existing reason-honesty check removes a "Hearsay:" label on a reasoning that
    itself says first-hand; the refusal reads the label AFTER that check."""
    judgment, _ = _judge(
        {
            "decision": "accept_as_directive",
            "reasoning": "Hearsay: the contributor states this first-hand as the scope owner.",
            "directive_ops": [{"op": "append"}],
            "new_context": None,
        }
    )
    assert [op.op for op in judgment.directive_ops] == ["append"]
    assert not _notes(judgment)


def test_a_declined_hearsay_item_is_untouched() -> None:
    judgment, _ = _judge(
        {
            "decision": "decline",
            "reasoning": HEARSAY,
            "directive_ops": [],
            "new_context": None,
        }
    )
    assert judgment.decision == "decline"
    assert not _notes(judgment)
