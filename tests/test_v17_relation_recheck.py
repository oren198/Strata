"""v1.17 item 2 — refinement/tightening over-decline re-check (#237).

Three layers, mirroring item 1's own shape (the architect's build
requirement — the bridge gate calls these directly, with recorded or
forced declines — never a live key), plus item 1's own review lessons
applied from the start:
1. `relation_decline_trigger` — pure, no judge.
2. `verify_relation_ground` — pure, no judge.
3. `ScopeManager.recheck_relation_decline` — the one piece that calls the
   judge, callable with a GIVEN first judgment (real or forced); the re-ask
   renders current context and never replaces it with engine text, a
   re-ask failure leaves the first decline standing, and `reasoning` is a
   required field.

Offline throughout: a REAL `ScopeManager`, only the underlying Anthropic
client mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from strata.fleet_config import Scope, Stratum
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import (
    ScopeManager,
    relation_decline_trigger,
    verify_relation_ground,
)
from strata.summary_store import Directive, ScopeSummary

from .test_scope_manager import _fake_response

STRATUM = Stratum(id="L1", name="function", ordinal=1)
PARENT = Scope(id="g_relparent", name="rel-parent", stratum_id="L0")
SCOPE = Scope(id="g_relself", name="rel-self", stratum_id="L1")

OWN_CONTRIBUTOR = ContributorRef(
    scope_id=SCOPE.id,
    skill="on-call-engineer",
    session_id="sess_rel",
    ts="2026-10-03T10:00:00+00:00",
)
OTHER_CONTRIBUTOR = ContributorRef(
    scope_id="g_relotherscope",
    skill="on-call-engineer",
    session_id="sess_relother",
    ts="2026-10-03T10:00:00+00:00",
)

ANCESTOR_DIRECTIVE = Directive(
    id="c_relparentdirective",
    content="Alerting escalates to SEV-1 at or below 500 ms p99 latency.",
    subject="alerting",
    source_scope_id=PARENT.id,
    source_skill="architect",
    created_at="2026-09-01T09:00:00+00:00",
)

CURRENT_SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[],
    context="The team favours minimal abstractions.",
    updated_at="2026-09-01T09:00:00+00:00",
)

ANCESTOR_DIRECTIVES = [(PARENT.id, [ANCESTOR_DIRECTIVE])]


def _contribution(
    content: str,
    *,
    cid: str = "c_relnew",
    proposed_classification: str = "directive",
    contributor: ContributorRef = OWN_CONTRIBUTOR,
) -> Contribution:
    return Contribution(
        id=cid,
        scope_id=SCOPE.id,
        content=content,
        proposed_classification=proposed_classification,
        subject=None,
        supersedes=None,
        contributor=contributor,
        created_at="2026-10-03T09:00:00+00:00",
    )


def _ordinary_decline(reasoning: str) -> MagicMock:
    return _fake_response(
        {"decision": "decline", "reasoning": reasoning, "directive_ops": [], "new_context": None}
    )


def _reask_response(**payload) -> MagicMock:
    payload.setdefault("other_grounds_clear", True)
    payload.setdefault("parent_id", ANCESTOR_DIRECTIVE.id)
    payload.setdefault("reasoning", "Rechecked.")
    return _fake_response(payload)


def _judge(
    mock_client: MagicMock,
    content: str,
    *,
    proposed_classification: str = "directive",
    contributor: ContributorRef = OWN_CONTRIBUTOR,
) -> tuple:
    manager = ScopeManager(client=mock_client)
    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        ancestor_directives=ANCESTOR_DIRECTIVES,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=_contribution(
            content, proposed_classification=proposed_classification, contributor=contributor
        ),
    )
    return judgment, manager


def _decline_reasoning() -> str:
    return f"Contradicts the inherited directive {ANCESTOR_DIRECTIVE.id}."


# ---------------------------------------------------------------------------
# 1. relation_decline_trigger — pure.
# ---------------------------------------------------------------------------


def test_trigger_fires_and_returns_the_matched_ancestor_id() -> None:
    reasoning = f"Contradicts the inherited directive {ANCESTOR_DIRECTIVE.id}."
    assert relation_decline_trigger(reasoning, [ANCESTOR_DIRECTIVE.id]) == ANCESTOR_DIRECTIVE.id


def test_trigger_does_not_fire_when_no_ancestor_id_is_named() -> None:
    assert (
        relation_decline_trigger("Declined: not relevant to this scope.", [ANCESTOR_DIRECTIVE.id])
        is None
    )
    assert relation_decline_trigger(None, [ANCESTOR_DIRECTIVE.id]) is None
    assert relation_decline_trigger("", [ANCESTOR_DIRECTIVE.id]) is None


def test_trigger_requires_a_word_bounded_match() -> None:
    # A substring match that is NOT word-bounded must not fire.
    assert (
        relation_decline_trigger("cites c_relparentdirectiveX somehow", ["c_relparentdirective"])
        is None
    )


# ---------------------------------------------------------------------------
# 2. verify_relation_ground — pure.
# ---------------------------------------------------------------------------


def test_contradicts_and_exempts_stand() -> None:
    for relation in ("contradicts", "exempts"):
        ok, result, classification, ctx = verify_relation_ground(
            {"relation": relation, "other_grounds_clear": True, "parent_id": "p"},
            "parent text",
            "content",
            "directive",
        )
        assert ok is False, relation
        assert classification is None
        assert ctx is None
        assert relation in result


def test_other_grounds_not_clear_fails_closed() -> None:
    ok, result, classification, ctx = verify_relation_ground(
        {
            "relation": "refines",
            "other_grounds_clear": False,
            "parent_id": "p",
            "subject_span": "x",
        },
        "parent text",
        "x",
        "directive",
    )
    assert ok is False
    assert classification is None
    assert ctx is None
    assert "other grounds not clear" in result


def test_unreadable_relation_fails_closed() -> None:
    ok, _result, classification, ctx = verify_relation_ground(
        {"relation": "bogus", "other_grounds_clear": True, "parent_id": "p"},
        "parent text",
        "content",
        "directive",
    )
    assert ok is False
    assert classification is None
    assert ctx is None


def test_missing_parent_id_fails_closed() -> None:
    ok, result, classification, ctx = verify_relation_ground(
        {"relation": "refines", "other_grounds_clear": True},
        "parent text",
        "content",
        "directive",
    )
    assert ok is False
    assert classification is None
    assert ctx is None
    assert "no parent_id" in result


# --- refines ---------------------------------------------------------------


def test_refines_verifies_subject_span_and_falls_back_to_proposed_classification() -> None:
    parent_text = "Deploys require a signed-off change ticket with a ticket number."
    content = "For hotfixes only, deploys also require on-call sign-off."
    answer = {
        "relation": "refines",
        "other_grounds_clear": True,
        "parent_id": "p",
        "subject_span": "For hotfixes only, deploys also require on-call sign-off",
    }
    ok, result, classification, ctx = verify_relation_ground(
        answer, parent_text, content, "directive"
    )
    assert ok is True
    assert classification == "directive"
    assert ctx is None
    assert "rescued" in result


def test_refines_fails_when_subject_span_not_verbatim() -> None:
    answer = {
        "relation": "refines",
        "other_grounds_clear": True,
        "parent_id": "p",
        "subject_span": "not in the text",
    }
    ok, result, classification, ctx = verify_relation_ground(
        answer, "parent text", "the real contribution text", "directive"
    )
    assert ok is False
    assert classification is None
    assert ctx is None
    assert "not verbatim" in result


def test_refines_fails_on_polarity_flip_against_parent() -> None:
    parent_text = "The gate opens before 09:00."
    content = "For the weekend shift, the gate opens after 09:00."
    answer = {
        "relation": "refines",
        "other_grounds_clear": True,
        "parent_id": "p",
        "subject_span": "For the weekend shift, the gate opens after 09:00",
    }
    ok, result, classification, ctx = verify_relation_ground(
        answer, parent_text, content, "directive"
    )
    assert ok is False
    assert classification is None
    assert ctx is None
    assert "polarity flip" in result


def test_refines_reinstates_as_context_and_requires_new_context() -> None:
    parent_text = "Deploys require a signed-off change ticket with a ticket number."
    content = "For hotfixes only, deploys also require on-call sign-off."
    answer = {
        "relation": "refines",
        "other_grounds_clear": True,
        "parent_id": "p",
        "subject_span": "For hotfixes only, deploys also require on-call sign-off",
        "classification": "context",
    }
    ok, _result, classification, ctx = verify_relation_ground(
        answer, parent_text, content, "directive"
    )
    assert ok is False
    assert classification is None
    assert ctx is None

    ok2, _result2, classification2, ctx2 = verify_relation_ground(
        {**answer, "new_context": "updated context"}, parent_text, content, "directive"
    )
    assert ok2 is True
    assert classification2 == "context"
    assert ctx2 == "updated context"


# --- tightens: RULE mode -----------------------------------------------------


def test_tightens_rule_at_or_below_smaller_is_stricter() -> None:
    parent_text = "Alerting escalates to SEV-1 at or below 500 ms p99 latency."
    content = "Alerting now escalates to SEV-1 at or below 300 ms p99 latency."
    answer = {
        "relation": "tightens",
        "other_grounds_clear": True,
        "parent_id": "p",
        "tighten_kind": "rule",
        "kept_span": "escalates to SEV-1 at or below 300 ms p99 latency",
    }
    ok, result, classification, ctx = verify_relation_ground(
        answer, parent_text, content, "directive"
    )
    assert ok is True, result
    assert classification == "directive"
    assert ctx is None


def test_tightens_rule_at_or_below_fails_when_not_stricter() -> None:
    parent_text = "Alerting escalates to SEV-1 at or below 500 ms p99 latency."
    content = "Alerting now escalates to SEV-1 at or below 700 ms p99 latency."
    answer = {
        "relation": "tightens",
        "other_grounds_clear": True,
        "parent_id": "p",
        "tighten_kind": "rule",
        "kept_span": "escalates to SEV-1 at or below 700 ms p99 latency",
    }
    ok, result, _classification, ctx = verify_relation_ground(
        answer, parent_text, content, "directive"
    )
    assert ok is False
    assert ctx is None
    assert "not stricter" in result


def test_tightens_rule_every_smaller_is_stricter() -> None:
    parent_text = "Rotate the on-call key every 30 minutes."
    content = "Rotate the on-call key every 15 minutes."
    answer = {
        "relation": "tightens",
        "other_grounds_clear": True,
        "parent_id": "p",
        "tighten_kind": "rule",
        "kept_span": "Rotate the on-call key every 15 minutes",
    }
    ok, result, _c, _ctx = verify_relation_ground(answer, parent_text, content, "directive")
    assert ok is True, result

    content_bad = "Rotate the on-call key every 60 minutes."
    answer_bad = {**answer, "kept_span": "Rotate the on-call key every 60 minutes"}
    ok2, result2, _c2, _ctx2 = verify_relation_ground(
        answer_bad, parent_text, content_bad, "directive"
    )
    assert ok2 is False
    assert "not stricter" in result2


def test_tightens_rule_within_smaller_is_stricter() -> None:
    parent_text = "Acknowledge the page within 24 hours."
    content = "Acknowledge the page within 12 hours."
    answer = {
        "relation": "tightens",
        "other_grounds_clear": True,
        "parent_id": "p",
        "tighten_kind": "rule",
        "kept_span": "Acknowledge the page within 12 hours",
    }
    ok, result, _c, _ctx = verify_relation_ground(answer, parent_text, content, "directive")
    assert ok is True, result

    content_bad = "Acknowledge the page within 48 hours."
    answer_bad = {**answer, "kept_span": "Acknowledge the page within 48 hours"}
    ok2, result2, _c2, _ctx2 = verify_relation_ground(
        answer_bad, parent_text, content_bad, "directive"
    )
    assert ok2 is False
    assert "not stricter" in result2


def test_tightens_rule_at_least_larger_is_stricter() -> None:
    parent_text = "Retry at least 3 times before failing."
    content = "Retry at least 5 times before failing."
    answer = {
        "relation": "tightens",
        "other_grounds_clear": True,
        "parent_id": "p",
        "tighten_kind": "rule",
        "kept_span": "Retry at least 5 times before failing",
    }
    ok, result, _c, _ctx = verify_relation_ground(answer, parent_text, content, "directive")
    assert ok is True, result

    content_bad = "Retry at least 1 time before failing."
    answer_bad = {**answer, "kept_span": "Retry at least 1 time before failing"}
    ok2, result2, _c2, _ctx2 = verify_relation_ground(
        answer_bad, parent_text, content_bad, "directive"
    )
    assert ok2 is False
    assert "not stricter" in result2


# --- tightens: FACT mode -----------------------------------------------------


def test_tightens_fact_mode_requires_parent_value_kept_verbatim() -> None:
    parent_text = "The cutover happens at 06:30."
    content = "For house 3 specifically, the cutover happens at 06:30, same as before."
    answer = {
        "relation": "tightens",
        "other_grounds_clear": True,
        "parent_id": "p",
        "tighten_kind": "fact",
        "kept_span": "the cutover happens at 06:30",
    }
    ok, result, classification, ctx = verify_relation_ground(
        answer, parent_text, content, "directive"
    )
    assert ok is True, result
    assert classification == "directive"
    assert ctx is None

    content_bad = "For house 3 specifically, the cutover happens at 07:00 now."
    answer_bad = {**answer, "kept_span": "the cutover happens at 07:00 now"}
    ok2, result2, _c2, _ctx2 = verify_relation_ground(
        answer_bad, parent_text, content_bad, "directive"
    )
    assert ok2 is False
    assert "value not kept" in result2


def test_tightens_fact_mode_fails_on_relation_antonym_polarity_flip() -> None:
    parent_text = "The maintenance window opens before 09:00."
    content = "For house 3 only, the maintenance window opens after 09:00."
    answer = {
        "relation": "tightens",
        "other_grounds_clear": True,
        "parent_id": "p",
        "tighten_kind": "fact",
        "kept_span": "the maintenance window opens after 09:00",
    }
    ok, result, _c, ctx = verify_relation_ground(answer, parent_text, content, "directive")
    assert ok is False
    assert ctx is None
    assert "polarity flip" in result


def test_tightens_fails_on_quantifier_softening_exception() -> None:
    parent_text = "Every deploy requires a signed-off change ticket."
    content = "Most deploys require a signed-off change ticket, except small config tweaks."
    answer = {
        "relation": "tightens",
        "other_grounds_clear": True,
        "parent_id": "p",
        "tighten_kind": "fact",
        "kept_span": "Most deploys require a signed-off change ticket",
    }
    ok, result, _c, ctx = verify_relation_ground(answer, parent_text, content, "directive")
    assert ok is False
    assert ctx is None
    assert "quantifier softened" in result


def test_tightens_fails_when_kept_span_not_verbatim() -> None:
    answer = {
        "relation": "tightens",
        "other_grounds_clear": True,
        "parent_id": "p",
        "kept_span": "not in the text",
    }
    ok, result, _c, ctx = verify_relation_ground(
        answer, "parent text", "the real contribution text", "directive"
    )
    assert ok is False
    assert ctx is None
    assert "not verbatim" in result


# ---------------------------------------------------------------------------
# 3. ScopeManager.recheck_relation_decline / judge() wiring.
# ---------------------------------------------------------------------------


def test_reinstates_as_accept_as_directive_when_proposed_classification_is_directive() -> None:
    mock_client = MagicMock()
    content = "For hotfixes only, deploys also require on-call sign-off."
    mock_client.messages.create.side_effect = [
        _ordinary_decline(_decline_reasoning()),
        _reask_response(
            relation="refines",
            subject_span=content,
        ),
    ]
    judgment, _ = _judge(mock_client, content, proposed_classification="directive")
    assert judgment.decision == "accept_as_directive"
    assert judgment.directive_ops and judgment.directive_ops[0].op == "append"
    assert judgment.relation_recheck == {
        "relation": "refines",
        "parent_id": ANCESTOR_DIRECTIVE.id,
        "result": "admitted (rescued, refines parent)",
    }


def test_reinstates_as_accept_as_context_when_proposed_classification_is_context() -> None:
    mock_client = MagicMock()
    content = "For hotfixes only, deploys also require on-call sign-off."
    mock_client.messages.create.side_effect = [
        _ordinary_decline(_decline_reasoning()),
        _reask_response(
            relation="refines",
            subject_span=content,
            new_context="updated context",
        ),
    ]
    judgment, _ = _judge(mock_client, content, proposed_classification="context")
    assert judgment.decision == "accept_as_context"
    assert judgment.directive_ops == []
    assert judgment.new_context == "updated context"


def test_other_grounds_not_clear_leaves_decline_standing() -> None:
    mock_client = MagicMock()
    content = "whatever"
    mock_client.messages.create.side_effect = [
        _ordinary_decline(_decline_reasoning()),
        _reask_response(relation="refines", subject_span=content, other_grounds_clear=False),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert judgment.relation_recheck["relation"] == "refines"


def test_reask_failure_leaves_the_first_decline_standing() -> None:
    mock_client = MagicMock()
    content = "whatever"
    mock_client.messages.create.side_effect = [
        _ordinary_decline(_decline_reasoning()),
        RuntimeError("judge is unavailable"),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert judgment.judge_failure is False
    assert "recheck failed" in judgment.reasoning
    assert judgment.relation_recheck["relation"] is None
    assert "recheck failed" in judgment.relation_recheck["result"]


def test_a_decline_naming_no_ancestor_directive_never_triggers() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _ordinary_decline(
        "Declined: not relevant to this scope."
    )
    judgment, _ = _judge(mock_client, "whatever")
    assert mock_client.messages.create.call_count == 1
    assert judgment.decision == "decline"
    assert judgment.relation_recheck is None


def test_position_concept_never_fires_for_a_non_own_scope_contribution() -> None:
    """Only an OWN-SCOPE contribution's decline can be reinstated — the
    contributor must be bound to the scope being judged."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _ordinary_decline(_decline_reasoning())
    judgment, _ = _judge(mock_client, "whatever", contributor=OTHER_CONTRIBUTOR)
    assert mock_client.messages.create.call_count == 1
    assert judgment.decision == "decline"
    assert judgment.relation_recheck is None


def test_reask_message_renders_the_current_context() -> None:
    """Item 1's own Blocker 1 lesson, applied from the start: the re-ask
    must render the scope's current context so a judge's full-replacement
    `new_context` can keep it."""
    mock_client = MagicMock()
    content = "For hotfixes only, deploys also require on-call sign-off."
    mock_client.messages.create.side_effect = [
        _ordinary_decline(_decline_reasoning()),
        _reask_response(relation="refines", subject_span=content),
    ]
    _judge(mock_client, content)
    reask_message = mock_client.messages.create.call_args_list[1].kwargs["messages"][0]["content"]
    assert CURRENT_SUMMARY.context in reask_message


# ---------------------------------------------------------------------------
# 4. Input identity: the FIRST call is unaffected by whether the recheck
#    fires.
# ---------------------------------------------------------------------------


def test_first_call_is_identical_whether_or_not_the_recheck_fires() -> None:
    mock_client_a = MagicMock()
    mock_client_a.messages.create.return_value = _ordinary_decline(_decline_reasoning())
    mock_client_b = MagicMock()
    mock_client_b.messages.create.return_value = _ordinary_decline(
        "Declined: not relevant to this scope."
    )
    content = "For hotfixes only, deploys also require on-call sign-off."
    _judge(mock_client_a, content)
    _judge(mock_client_b, content)

    first_call_a = mock_client_a.messages.create.call_args_list[0].kwargs
    first_call_b = mock_client_b.messages.create.call_args_list[0].kwargs
    assert first_call_a["messages"] == first_call_b["messages"]
    assert first_call_a["system"] == first_call_b["system"]
    assert first_call_a["tools"] == first_call_b["tools"]


# ---------------------------------------------------------------------------
# 7. Round 1 (architect ruling) — item 2's own adversarial attack against
#    verify_relation_ground, over every EXEMPT/CONTRADICT item in
#    j1_refinement and the gup in_child set. Shapes reproduced here with
#    self-authored fixtures (never copying the held-out sets' own text).
# ---------------------------------------------------------------------------


def test_tightens_rule_mode_requires_value_subset_when_no_pattern_recognised() -> None:
    """The judge-only free pass for an unrecognised comparator pattern was
    an unconditional admit on the PURE verifier — every such attack went
    through. Falls back to the value-subset check instead."""
    parent_text = "The safety curtain is lowered during every interval."
    content = "The safety curtain is lowered only at the end of the performance."
    answer = {
        "relation": "tightens",
        "other_grounds_clear": True,
        "parent_id": "p",
        "tighten_kind": "rule",
        "kept_span": content,
    }
    ok, result, _c, ctx = verify_relation_ground(answer, parent_text, content, "directive")
    assert ok is False
    assert ctx is None
    assert "quantifier softened" in result


def test_refines_declines_an_exemption_phrased_as_a_refinement() -> None:
    """An EXEMPT item — a conditional carve-out relaxing the parent's own
    limit — must not pass as a refinement."""
    parent_text = "The access door must be replaced every 40,000 openings."
    content = "The access door may run past 40,000 openings when only light use is logged."
    answer = {
        "relation": "refines",
        "other_grounds_clear": True,
        "parent_id": "p",
        "subject_span": content,
    }
    ok, result, _c, ctx = verify_relation_ground(answer, parent_text, content, "directive")
    assert ok is False
    assert ctx is None
    assert "exemption language" in result


def test_tightens_fact_mode_requires_value_subset_even_with_no_parent_numbers() -> None:
    """A parent with NO value tokens at all made the FACT-mode subset check
    vacuously true (empty set ⊆ anything), so a genuine CONTRADICTION
    (narrowing WHEN the rule applies, not tightening a value) slipped
    through. The quantifier-softening guard (extended to "only") now
    catches this shape directly."""
    parent_text = "The gate is checked during every patrol."
    content = "The gate is checked only at the start of the shift."
    answer = {
        "relation": "tightens",
        "other_grounds_clear": True,
        "parent_id": "p",
        "tighten_kind": "fact",
        "kept_span": content,
    }
    ok, result, _c, ctx = verify_relation_ground(answer, parent_text, content, "directive")
    assert ok is False
    assert ctx is None
    assert "quantifier softened" in result


def test_tightens_declines_a_kept_span_that_truncates_a_number() -> None:
    """A verbatim span that ends mid-number ("...every 65" where the real
    text continues ",000 units") passes the plain verbatim check but
    misrepresents the actual value."""
    parent_text = "The filter must be replaced every 40,000 cycles."
    content = "The filter must be replaced every 65,000 cycles."
    answer = {
        "relation": "tightens",
        "other_grounds_clear": True,
        "parent_id": "p",
        "tighten_kind": "rule",
        "kept_span": "The filter must be replaced every 65",
    }
    ok, result, _c, ctx = verify_relation_ground(answer, parent_text, content, "directive")
    assert ok is False
    assert ctx is None
    assert "truncates a number" in result


def test_refines_declines_a_same_subject_value_swap() -> None:
    """A flat value swap on the SAME subject (near-identical sentence,
    just the number changed) is a contradiction, not a refinement."""
    parent_text = "Seedlings must be watered at or before 07:00 every day."
    content = "Seedlings must be watered at or before 09:00 every day."
    answer = {
        "relation": "refines",
        "other_grounds_clear": True,
        "parent_id": "p",
        "subject_span": content,
    }
    ok, result, _c, ctx = verify_relation_ground(answer, parent_text, content, "directive")
    assert ok is False
    assert ctx is None
    assert "value conflict with parent" in result


def test_refines_admits_a_genuinely_different_subject_despite_shared_wording() -> None:
    """The flip side of the above: a DIFFERENT subject that happens to
    share most of the parent's sentence shape (different adjective, same
    noun and structure) is a genuine refine, not a conflict."""
    parent_text = "Frozen pallets must stay at or below -18 °C during dock transfer."
    content = "Chilled pallets must stay at or below 4 °C during dock transfer."
    answer = {
        "relation": "refines",
        "other_grounds_clear": True,
        "parent_id": "p",
        "subject_span": content,
    }
    ok, _result, _c, ctx = verify_relation_ground(answer, parent_text, content, "directive")
    assert ok is True
    assert ctx is None


def test_refines_admits_a_paraphrased_restatement_of_the_same_subject() -> None:
    """A compressed paraphrase ("Replace X every N" for "X must be
    replaced every M") still correctly flags a value swap — the
    subject-match proxy tolerates the paraphrase's own word order."""
    parent_text = "The guillotine blade must be replaced every 40,000 cuts."
    content = "Replace guillotine blade every 65,000 cuts."
    answer = {
        "relation": "refines",
        "other_grounds_clear": True,
        "parent_id": "p",
        "subject_span": content,
    }
    ok, result, _c, ctx = verify_relation_ground(answer, parent_text, content, "directive")
    assert ok is False
    assert ctx is None
    assert "value conflict with parent" in result
