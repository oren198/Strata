"""v1.15 P3, ADR 0017 — the judge's four-disposition outcome judging.

The judgments.decision column stays CHECK-constrained to its original three values —
held/failed_corrected/failed_superseded/decline is a TOOL-level disposition, never
persisted directly (see ScopeManagerJudgment.outcome_disposition's docstring).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from strata.fleet_config import Scope, Stratum
from strata.record_store import Contribution, ContributorRef, RecentContribution
from strata.scope_manager import (
    JUDGE_TOOL,
    ActedOnTarget,
    ScopeManager,
    _build_batch_judge_tool,
    _judge_tool_for,
)
from strata.summary_store import ScopeSummary

STRATUM = Stratum(id="L1", name="team", ordinal=1)
SCOPE = Scope(id="g_team", name="team", stratum_id="L1")
CONTRIBUTOR = ContributorRef(
    scope_id=SCOPE.id, skill="engineer", session_id="s1", ts="2026-09-25T09:00:00+00:00"
)
TARGET_CONTEXT_CONTRIBUTION = Contribution(
    id="c_target01",
    scope_id=SCOPE.id,
    content="The service listens on port 8443.",
    proposed_classification="context",
    subject="service-port",
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-09-01T09:00:00+00:00",
)
TARGET_DIRECTIVE_CONTRIBUTION = Contribution(
    id="c_target02",
    scope_id=SCOPE.id,
    content="All services must use TLS 1.3 or later.",
    proposed_classification="directive",
    subject="tls",
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-09-01T09:00:00+00:00",
)
CONTEXT_TARGET = ActedOnTarget(
    contribution=TARGET_CONTEXT_CONTRIBUTION, decision="accept_as_context"
)
DIRECTIVE_TARGET = ActedOnTarget(
    contribution=TARGET_DIRECTIVE_CONTRIBUTION, decision="accept_as_directive"
)

OUTCOME_CONTRIBUTION = Contribution(
    id="c_outcome01",
    scope_id=SCOPE.id,
    content="Used port 8443, the service refused; the right port is unknown.",
    proposed_classification="context",
    subject="service-port-outcome",
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-09-25T09:00:00+00:00",
    acted_on=TARGET_CONTEXT_CONTRIBUTION.id,
)

SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[],
    context="The service listens on port 8443.",
    updated_at="2026-09-01T09:00:00+00:00",
)


def _resp(**payload) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.input = payload
    r = MagicMock()
    r.content = [block]
    return r


def _held(**extra) -> dict:
    return {
        "decision": "accept_as_context",
        "reasoning": 'Confirmed by observation: "the service refused on 8443" never happened.',
        "directive_ops": [],
        "new_context": "The service listens on port 8443.",
        "outcome_disposition": "held",
        **extra,
    }


def _failed_corrected(**extra) -> dict:
    return {
        "decision": "accept_as_context",
        "reasoning": "Used port 8443, the service refused; the claim was wrong.",
        "directive_ops": [],
        "new_context": "The service listens on an unknown port; 8443 refused the connection.",
        "outcome_disposition": "failed_corrected",
        **extra,
    }


def _decline(**extra) -> dict:
    return {
        "decision": "decline",
        "reasoning": "no outcome reported: the contribution only says it was reviewed.",
        "directive_ops": None,
        "new_context": None,
        "outcome_disposition": "decline",
        **extra,
    }


def _judge(
    *payloads: dict,
    target=CONTEXT_TARGET,
    contribution=OUTCOME_CONTRIBUTION,
    recent_contributions=(),
):
    client = MagicMock()
    client.messages.create.side_effect = [_resp(**p) for p in payloads]
    judgment = ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=SUMMARY,
        recent_contributions=list(recent_contributions),
        new_contribution=contribution,
        acted_on_target=target,
    )
    return judgment, client


#: The target's own row in the recency window — the #199 backstop can only see a
#: target's content in the CURRENT summary or this window (its documented bound).
TARGET_IN_WINDOW = RecentContribution(
    contribution=TARGET_CONTEXT_CONTRIBUTION,
    state="judged",
    decision="accept_as_context",
    judgment_notes="Accepted as context.",
)


def _followup_text(client: MagicMock, call: int = 1) -> str:
    content = client.messages.create.call_args_list[call].kwargs["messages"][-1]["content"]
    return " ".join(b["text"] for b in content if b.get("type") == "text")


# --- schema --------------------------------------------------------------------------


def test_the_tool_carries_outcome_disposition_only_for_an_acted_on_target() -> None:
    # ADR 0017 P3 rev2 (M1/#212 lesson): outcome_disposition is offered ONLY on the
    # variant derived for an acted_on contribution — never on the base JUDGE_TOOL every
    # ordinary judge call sees. See tests/test_p3_tool_schema_pins.py for the full pin.
    assert "outcome_disposition" not in JUDGE_TOOL["input_schema"]["properties"]

    props = _judge_tool_for(CONTEXT_TARGET)["input_schema"]["properties"]
    assert props["outcome_disposition"]["type"] == ["string", "null"]
    assert "held" in props["outcome_disposition"]["description"]


def test_the_batch_tool_does_not_offer_outcome_disposition() -> None:
    batch = _build_batch_judge_tool()
    assert "outcome_disposition" not in batch["input_schema"]["properties"]


# --- the four dispositions map to existing record facts -------------------------------


def test_held_persists_as_accept_as_context_no_event() -> None:
    j, client = _judge(_held())
    assert client.messages.create.call_count == 1
    assert j.decision == "accept_as_context"
    assert j.outcome_disposition == "held"
    assert j.new_summary is not None


def test_failed_corrected_persists_as_accept_as_context() -> None:
    j, _ = _judge(_failed_corrected())
    assert j.decision == "accept_as_context"
    assert j.outcome_disposition == "failed_corrected"


def test_failed_superseded_persists_as_accept_as_context() -> None:
    j, _ = _judge(_failed_corrected(outcome_disposition="failed_superseded"))
    assert j.decision == "accept_as_context"
    assert j.outcome_disposition == "failed_superseded"


def test_decline_persists_as_decline() -> None:
    j, _ = _judge(_decline())
    assert j.decision == "decline"
    assert j.outcome_disposition == "decline"
    assert j.new_summary is None


# --- disposition/decision must agree ---------------------------------------------------


def test_disposition_decision_mismatch_earns_the_one_reask() -> None:
    j, client = _judge(
        _held(decision="decline"),  # mismatch: held requires accept_as_context
        _held(),
    )
    assert client.messages.create.call_count == 2
    text = _followup_text(client)
    assert "outcome_disposition" in text
    assert j.outcome_disposition == "held"
    assert j.decision == "accept_as_context"


# --- missing/malformed/unknown/empty: four shapes, one re-ask, then fail-closed --------


@pytest.mark.parametrize(
    "bad_disposition",
    [None, "", "definitely_not_a_real_value", "confirmed"],
    ids=["missing", "empty", "unknown", "malformed"],
)
def test_four_shapes_of_malformed_disposition_earn_one_reask(bad_disposition) -> None:
    j, client = _judge(_held(outcome_disposition=bad_disposition), _held())
    assert client.messages.create.call_count == 2
    assert j.outcome_disposition == "held"
    assert j.disposition_unreadable is False


def test_still_malformed_after_the_reask_declines_as_a_judge_failure() -> None:
    j, client = _judge(
        _held(outcome_disposition="definitely_not_a_real_value"),
        _held(outcome_disposition="still_not_real"),
    )
    assert client.messages.create.call_count == 2  # one re-ask, never a third call
    assert j.decision == "decline"
    assert j.new_summary is None
    assert j.outcome_disposition == "decline"
    assert j.disposition_unreadable is True
    assert "judge failure" in j.record_notes
    assert "not a missing-ground decline" in j.record_notes


def test_judge_failure_decline_is_distinguishable_from_a_missing_ground_decline() -> None:
    judge_failure, _ = _judge(
        _held(outcome_disposition="bogus"), _held(outcome_disposition="still bogus")
    )
    missing_ground, _ = _judge(_decline())
    assert "judge failure" in judge_failure.record_notes
    assert "judge failure" not in missing_ground.record_notes


# --- default to failed_corrected when ambiguous (prompt instruction, not mechanical) ---


def test_prompt_instructs_default_to_failed_corrected_when_ambiguous() -> None:
    from strata.scope_manager import _render_outcome_block

    block = _render_outcome_block(CONTEXT_TARGET)
    assert "cannot tell failed_corrected from failed_superseded, choose failed_corrected" in block


# --- held quotes its observable ---------------------------------------------------------


def test_prompt_requires_held_to_quote_the_observable() -> None:
    from strata.scope_manager import _render_outcome_block

    block = _render_outcome_block(CONTEXT_TARGET)
    assert "QUOTE, in one clause, the observed result" in block


def test_prompt_defines_held_never_as_the_claim_is_true() -> None:
    from strata.scope_manager import _render_outcome_block

    block = _render_outcome_block(CONTEXT_TARGET)
    assert "an ACTION THAT COULD HAVE FAILED CONFIRMED THE ITEM" in block
    assert "never merely that the claim reads as true" in block


# --- no known-wrong state, no standing field --------------------------------------------


def test_prompt_states_no_known_wrong_state_no_lowered_standing() -> None:
    from strata.scope_manager import _render_outcome_block

    block = _render_outcome_block(CONTEXT_TARGET)
    assert "no known-wrong state and no lowered standing" in block


# --- the #199 backstop treats acted_on as the effective target for failed_* -----------


def test_failed_corrected_backstop_catches_a_rewrite_that_keeps_the_old_claim() -> None:
    """The #199 path, extended to acted_on: new_context still says "8443" verbatim.
    The target must be visible to the backstop — in the current summary or the
    recency window (its documented bound) — so it is seeded into the window here."""
    j, client = _judge(
        _failed_corrected(new_context="The service listens on port 8443."),  # resurrects it
        _failed_corrected(new_context="The service listens on an unknown port."),
        recent_contributions=[TARGET_IN_WINDOW],
    )
    assert client.messages.create.call_count == 2
    text = _followup_text(client)
    assert "SUPERSEDED OR RETRACTED CLAIM LEAVES THE CONTEXT ENTIRELY" in text
    assert j.new_summary.context == "The service listens on an unknown port."


def test_failed_corrected_backstop_is_silently_skipped_when_the_target_is_out_of_window() -> None:
    """MEASURED, not fixed (per the plan's own stated bound): with the target neither
    in the current summary's directives nor in the recency window, the #199 backstop
    has no content to compare against, so a rewrite that keeps the old claim verbatim
    is NOT caught — it passes through on the first call, no re-ask at all."""
    j, client = _judge(
        _failed_corrected(new_context="The service listens on port 8443."),
        recent_contributions=[],  # target genuinely out of window
    )
    assert client.messages.create.call_count == 1  # no backstop re-ask fired
    assert j.new_summary.context == "The service listens on port 8443."  # old claim survives


def test_held_never_triggers_the_199_backstop_against_its_own_target() -> None:
    """A held outcome does not replace anything — its own target's content staying in
    new_context is not a resurrection, it is simply unchanged."""
    j, client = _judge(_held(new_context="The service listens on port 8443."))
    assert client.messages.create.call_count == 1
    assert j.new_summary.context == "The service listens on port 8443."


# --- D6: a directive target is never replaced ------------------------------------------


def test_a_directive_target_is_never_replaced_by_a_failed_disposition() -> None:
    """CEO ruling: failed_* against a directive target -> accept_as_context only, no
    #199 replacement attempted (the directive isn't in `current_summary.context` to
    begin with, so this also proves the backstop wasn't even consulted for it)."""
    j, client = _judge(
        _failed_corrected(new_context="Something else entirely, no TLS mention."),
        target=DIRECTIVE_TARGET,
        contribution=Contribution(
            id="c_outcome02",
            scope_id=SCOPE.id,
            content="Tried TLS 1.3; the peer only supports 1.2.",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=CONTRIBUTOR,
            created_at="2026-09-25T09:00:00+00:00",
            acted_on=TARGET_DIRECTIVE_CONTRIBUTION.id,
        ),
    )
    assert client.messages.create.call_count == 1  # no #199 re-ask fired
    assert j.decision == "accept_as_context"
    assert j.outcome_disposition == "failed_corrected"


def test_prompt_tells_the_judge_the_target_is_a_directive() -> None:
    from strata.scope_manager import _render_outcome_block

    block = _render_outcome_block(DIRECTIVE_TARGET)
    assert "this item is a DIRECTIVE" in block
    assert "never replaced by an outcome" in block
    context_block = _render_outcome_block(CONTEXT_TARGET)
    assert "this item is a DIRECTIVE" not in context_block


# --- golden: the block appears only with acted_on_target, and never otherwise ---------


def test_no_acted_on_target_renders_no_outcome_block() -> None:
    from strata.scope_manager import _build_user_message

    ordinary = Contribution(
        id="c_ordinary",
        scope_id=SCOPE.id,
        content="An ordinary observation.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=CONTRIBUTOR,
        created_at="2026-09-25T09:00:00+00:00",
    )
    prompt = _build_user_message(
        scope=SCOPE,
        stratum=STRATUM,
        ancestor_directives=None,
        current_summary=SUMMARY,
        recent_contributions=[],
        new_contribution=ordinary,
    )
    assert "OUTCOME REPORT" not in prompt


def test_acted_on_target_renders_the_outcome_block() -> None:
    from strata.scope_manager import _build_user_message

    prompt = _build_user_message(
        scope=SCOPE,
        stratum=STRATUM,
        ancestor_directives=None,
        current_summary=SUMMARY,
        recent_contributions=[],
        new_contribution=OUTCOME_CONTRIBUTION,
        acted_on_target=CONTEXT_TARGET,
    )
    assert "OUTCOME REPORT" in prompt
    assert TARGET_CONTEXT_CONTRIBUTION.id in prompt
    assert TARGET_CONTEXT_CONTRIBUTION.content in prompt
