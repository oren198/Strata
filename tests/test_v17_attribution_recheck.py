"""v1.17 item 1 — attribution over-decline re-check (#225 in reverse).

Three layers, per the architect's build requirement (the bridge gate calls
these directly, with recorded or forced declines — never a live key):
1. `attribution_decline_trigger` — pure, no judge.
2. `verify_attribution_ground` — pure, no judge.
3. `ScopeManager.recheck_attribution_decline` — the one piece that calls the
   judge, callable with a GIVEN first judgment (real or forced).

Offline throughout: a REAL `ScopeManager`, only the underlying Anthropic
client mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from strata.fleet_config import EntitlementView, Scope, Stratum
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import (
    AttributionFleetContext,
    ScopeManager,
    attribution_decline_trigger,
    verify_attribution_ground,
)
from strata.summary_store import Directive, ScopeSummary

from .test_scope_manager import _fake_response

STRATUM = Stratum(id="L1", name="function", ordinal=1)
PARENT = Scope(id="g_attrparent", name="attr-parent", stratum_id="L0")
SCOPE = Scope(id="g_attrself", name="attr-self", stratum_id="L1")
OTHER = Scope(id="g_attrother", name="attr-other-scope", stratum_id="L1")

CONTRIBUTOR = ContributorRef(
    scope_id="g_attrreporter",
    skill="on-call-engineer",
    session_id="sess_attr",
    ts="2026-10-03T10:00:00+00:00",
)

EXISTING_DIRECTIVE = Directive(
    id="c_attrdirective",
    content="Deploys require a signed-off change ticket with a ticket number.",
    subject="deploy-process",
    source_scope_id=SCOPE.id,
    source_skill="architect",
    created_at="2026-09-01T09:00:00+00:00",
)

CURRENT_SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[EXISTING_DIRECTIVE],
    context="The team favours minimal abstractions.",
    updated_at="2026-09-01T09:00:00+00:00",
)

ENTITLEMENT = EntitlementView(
    chain=[PARENT, SCOPE], descendants=[], referenced_peers=[], others=[OTHER]
)


def _contribution(content: str, *, cid: str = "c_attrnew") -> Contribution:
    return Contribution(
        id=cid,
        scope_id=SCOPE.id,
        content=content,
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=CONTRIBUTOR,
        created_at="2026-10-03T09:00:00+00:00",
    )


def _ordinary_decline(reasoning: str) -> MagicMock:
    return _fake_response(
        {"decision": "decline", "reasoning": reasoning, "directive_ops": [], "new_context": None}
    )


def _reask_response(**payload) -> MagicMock:
    payload.setdefault("other_grounds_clear", True)
    payload.setdefault("reasoning", "Rechecked.")
    return _fake_response(payload)


def _judge(mock_client: MagicMock, content: str, reasoning: str) -> tuple:
    manager = ScopeManager(client=mock_client)
    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=_contribution(content),
        entitlement=ENTITLEMENT,
    )
    return judgment, manager


# ---------------------------------------------------------------------------
# 1. attribution_decline_trigger — pure.
# ---------------------------------------------------------------------------


def test_trigger_fires_on_each_marker() -> None:
    assert attribution_decline_trigger("Manufactured attribution: no source.")
    assert attribution_decline_trigger("Declined: no one spoke to support this.")
    assert attribution_decline_trigger("No informant is named.")
    assert attribution_decline_trigger("No speaker is identified.")


def test_trigger_is_case_insensitive() -> None:
    assert attribution_decline_trigger("MANUFACTURED ATTRIBUTION")


def test_trigger_does_not_fire_on_unrelated_declines() -> None:
    assert not attribution_decline_trigger("Declined: contradicts a binding directive.")
    assert not attribution_decline_trigger(None)
    assert not attribution_decline_trigger("")


# ---------------------------------------------------------------------------
# 2. verify_attribution_ground — pure.
# ---------------------------------------------------------------------------


def _fleet(**overrides) -> AttributionFleetContext:
    defaults = dict(
        all_scopes=[SCOPE, OTHER],
        non_entitled_scopes=[OTHER],
        contributor_scope_id=CONTRIBUTOR.scope_id,
        contributor_scope_name=None,
        contributor_skill=CONTRIBUTOR.skill,
    )
    defaults.update(overrides)
    return AttributionFleetContext(**defaults)


def test_ground_none_fails_closed() -> None:
    ok, result, ctx = verify_attribution_ground(
        {"ground_kind": "none", "other_grounds_clear": True}, "content", {}, _fleet()
    )
    assert ok is False
    assert ctx is None
    assert "manufactured attribution stands" in result


def test_other_grounds_not_clear_fails_closed_regardless_of_ground_kind() -> None:
    ok, result, ctx = verify_attribution_ground(
        {
            "ground_kind": "first_hand_own",
            "other_grounds_clear": False,
            "span": "we decided to keep this simple",
        },
        "we decided to keep this simple",
        {},
        _fleet(),
    )
    assert ok is False
    assert ctx is None
    assert "other grounds not clear" in result


def test_unreadable_ground_kind_fails_closed() -> None:
    ok, _result, ctx = verify_attribution_ground(
        {"ground_kind": "bogus", "other_grounds_clear": True}, "content", {}, _fleet()
    )
    assert ok is False
    assert ctx is None


def test_missing_other_grounds_clear_fails_closed() -> None:
    ok, result, ctx = verify_attribution_ground(
        {"ground_kind": "first_hand_own"}, "content", {}, _fleet()
    )
    assert ok is False
    assert ctx is None
    assert "required bool" in result


def test_directive_or_publication_verifies_value_and_polarity() -> None:
    content = "As product-eng's parent directive already states, SEV-1 triggers at 500 ms p99."
    rendered_refs = {"c_ref1": "Alerting escalates to SEV-1 only above 500 ms p99 latency."}
    answer = {
        "ground_kind": "directive_or_publication",
        "other_grounds_clear": True,
        "span": "SEV-1 triggers at 500 ms p99",
        "ref_id": "c_ref1",
        "new_context": "updated context",
    }
    ok, result, ctx = verify_attribution_ground(answer, content, rendered_refs, _fleet())
    assert ok is True
    assert ctx == "updated context"
    assert "rescued" in result


def test_directive_or_publication_fails_on_value_mismatch() -> None:
    content = "As the parent directive says, SEV-1 triggers at 999 ms p99."
    rendered_refs = {"c_ref1": "Alerting escalates to SEV-1 only above 500 ms p99 latency."}
    answer = {
        "ground_kind": "directive_or_publication",
        "other_grounds_clear": True,
        "span": "SEV-1 triggers at 999 ms p99",
        "ref_id": "c_ref1",
        "new_context": "updated context",
    }
    ok, result, ctx = verify_attribution_ground(answer, content, rendered_refs, _fleet())
    assert ok is False
    assert ctx is None
    assert "value/polarity mismatch" in result


def test_directive_or_publication_fails_on_ref_not_rendered() -> None:
    content = "As the parent directive says, SEV-1 triggers at 500 ms p99."
    answer = {
        "ground_kind": "directive_or_publication",
        "other_grounds_clear": True,
        "span": "SEV-1 triggers at 500 ms p99",
        "ref_id": "c_invisible",
        "new_context": "updated context",
    }
    ok, result, ctx = verify_attribution_ground(answer, content, {}, _fleet())
    assert ok is False
    assert ctx is None
    assert "ref not visible" in result


def test_outside_party_requires_an_event_verb_and_bars_a_fleet_scope() -> None:
    content = "Global Pay published an advisory saying their API now requires 2FA."
    answer = {
        "ground_kind": "outside_party",
        "other_grounds_clear": True,
        "party_span": "Global Pay",
        "act_span": "published an advisory",
    }
    ok, result, ctx = verify_attribution_ground(answer, content, {}, _fleet())
    assert ok is True
    assert ctx == "According to Global Pay's published an advisory: " + content
    assert "rescued" in result

    # The named "party" is actually a fleet scope: barred mechanically.
    content2 = "attr-other-scope published an advisory saying they'd change the schema."
    answer2 = {
        "ground_kind": "outside_party",
        "other_grounds_clear": True,
        "party_span": "attr-other-scope",
        "act_span": "published an advisory",
    }
    ok2, result2, ctx2 = verify_attribution_ground(answer2, content2, {}, _fleet())
    assert ok2 is False
    assert ctx2 is None
    assert "fleet scope" in result2

    # A standing position, not an EVENT verb: declined.
    content3 = "Global Pay says their API requires 2FA."
    answer3 = {
        "ground_kind": "outside_party",
        "other_grounds_clear": True,
        "party_span": "Global Pay",
        "act_span": "says their API requires 2FA",
    }
    ok3, result3, ctx3 = verify_attribution_ground(answer3, content3, {}, _fleet())
    assert ok3 is False
    assert ctx3 is None
    assert "no event verb" in result3


def test_telling_event_reuses_225s_own_check() -> None:
    content = "The platform-eng team mentioned to me that we're moving to the new queue."
    answer = {
        "ground_kind": "telling_event",
        "other_grounds_clear": True,
        "telling_span": "mentioned to me that we're",
        "new_context": "updated context",
    }
    ok, result, ctx = verify_attribution_ground(answer, content, {}, _fleet())
    assert ok is True
    assert ctx == "updated context"

    # No first-person marker in the telling span: declined (not invented,
    # just not verified as addressed to the contributor).
    answer_bad = {
        "ground_kind": "telling_event",
        "other_grounds_clear": True,
        "telling_span": "the platform-eng team mentioned the queue",
        "new_context": "updated context",
    }
    ok2, result2, ctx2 = verify_attribution_ground(answer_bad, content, {}, _fleet())
    assert ok2 is False
    assert ctx2 is None
    assert "no telling event" in result2


def test_first_hand_own_is_barred_when_a_non_entitled_scope_is_named() -> None:
    # j1-031 shape: the contributor's OWN proposal about its own scope.
    content = "we decided to only escalate to SEV-1 if error rate exceeds 5%."
    answer = {
        "ground_kind": "first_hand_own",
        "other_grounds_clear": True,
        "span": "we decided to only escalate to SEV-1 if error rate exceeds 5%",
        "new_context": "updated context",
    }
    ok, result, ctx = verify_attribution_ground(answer, content, {}, _fleet())
    assert ok is True
    assert ctx == "updated context"

    # Same shape, but the content ALSO names a non-entitled scope: barred.
    content2 = "we decided this, same as attr-other-scope's own policy."
    ok2, result2, ctx2 = verify_attribution_ground(
        {**answer, "span": "we decided this"}, content2, {}, _fleet()
    )
    assert ok2 is False
    assert ctx2 is None
    assert "non-entitled scope" in result2

    # Attestation frame stripped, still first-hand: admitted.
    content3 = "As on-call, I can tell you we decided to keep the current rollout cadence."
    ok3, _result3, ctx3 = verify_attribution_ground(
        {
            "ground_kind": "first_hand_own",
            "other_grounds_clear": True,
            "span": "I can tell you we decided to keep the current rollout cadence",
            "new_context": "updated context",
        },
        content3,
        {},
        _fleet(),
    )
    assert ok3 is True
    assert ctx3 == "updated context"


def test_outside_party_appends_to_existing_context_rather_than_replacing_it() -> None:
    """Blocker 1 (architect review of 10327f6): the engine writes this
    line itself, never the judge's own text, so it must APPEND to the
    scope's existing context rather than wipe it."""
    content = "Global Pay published an advisory saying their API now requires 2FA."
    answer = {
        "ground_kind": "outside_party",
        "other_grounds_clear": True,
        "party_span": "Global Pay",
        "act_span": "published an advisory",
    }
    ok, result, ctx = verify_attribution_ground(
        answer, content, {}, _fleet(), previous_context="The team favours minimal abstractions."
    )
    assert ok is True
    assert ctx == (
        "The team favours minimal abstractions.\n"
        "According to Global Pay's published an advisory: " + content
    )
    assert "rescued" in result


def test_directive_or_publication_declines_a_scope_the_referenced_item_does_not_name() -> None:
    """Fix 3 (same review): `_value_tokens` is a subset test, so a span
    with no value tokens at all (no number, id, or quote) passes the
    value/polarity check trivially. Such a span naming a non-entitled
    scope the referenced item never mentions must still be barred."""
    content = (
        "As the branches directive says, attr-other-scope follows the "
        "acquisitions-only ordering process."
    )
    rendered_refs = {
        "c_ref1": "Acquisitions only orders new titles in the first week of each month."
    }
    span = "attr-other-scope follows the acquisitions-only ordering process"
    answer = {
        "ground_kind": "directive_or_publication",
        "other_grounds_clear": True,
        "span": span,
        "ref_id": "c_ref1",
        "new_context": "updated context",
    }
    ok, result, ctx = verify_attribution_ground(answer, content, rendered_refs, _fleet())
    assert ok is False
    assert ctx is None
    assert "names a scope the referenced item does not" in result

    # Same shape, but the referenced item DOES name that scope: a real
    # directive naming a scope stays rescuable.
    rendered_refs2 = {
        "c_ref1": (
            "Acquisitions only orders new titles in the first week of each month, "
            "same as attr-other-scope's own process."
        )
    }
    ok2, result2, ctx2 = verify_attribution_ground(answer, content, rendered_refs2, _fleet())
    assert ok2 is True
    assert ctx2 == "updated context"
    assert "rescued" in result2


def test_outside_party_is_barred_when_content_asserts_a_non_entitled_scopes_interior() -> None:
    """Fix 3 (same review): an outside party can't carry another fleet
    scope's interior — a stated limit, failing closed."""
    content = "Global Pay published an advisory saying attr-other-scope's deploy policy changed."
    answer = {
        "ground_kind": "outside_party",
        "other_grounds_clear": True,
        "party_span": "Global Pay",
        "act_span": "published an advisory",
    }
    ok, result, ctx = verify_attribution_ground(answer, content, {}, _fleet())
    assert ok is False
    assert ctx is None
    assert "non-entitled scope's interior" in result


def test_span_not_verbatim_fails_closed_for_every_ground_kind() -> None:
    for ground_kind, extra in (
        ("directive_or_publication", {"span": "not in the text", "ref_id": "x"}),
        ("first_hand_own", {"span": "not in the text"}),
    ):
        ok, result, ctx = verify_attribution_ground(
            {"ground_kind": ground_kind, "other_grounds_clear": True, **extra},
            "the real contribution text",
            {"x": "y"},
            _fleet(),
        )
        assert ok is False, ground_kind
        assert ctx is None, ground_kind
        assert "not verbatim" in result, ground_kind


# ---------------------------------------------------------------------------
# 3. ScopeManager.recheck_attribution_decline / judge() wiring.
# ---------------------------------------------------------------------------


def test_ceiling_is_accept_as_context_never_a_directive() -> None:
    mock_client = MagicMock()
    content = "we decided to only escalate to SEV-1 if error rate exceeds 5%."
    mock_client.messages.create.side_effect = [
        _ordinary_decline("Manufactured attribution: no one spoke, no publication, no directive."),
        _reask_response(
            ground_kind="first_hand_own",
            span=content,
            new_context="The team decided to only escalate to SEV-1 if error rate exceeds 5%.",
        ),
    ]
    judgment, _ = _judge(mock_client, content, "")
    assert judgment.decision == "accept_as_context"
    assert judgment.directive_ops == []
    assert judgment.attribution_recheck == {
        "ground_kind": "first_hand_own",
        "other_grounds_clear": True,
        "result": "admitted (rescued)",
    }


def test_decline_stands_when_ground_kind_is_none() -> None:
    mock_client = MagicMock()
    content = "procurement only approves orders under $500."
    mock_client.messages.create.side_effect = [
        _ordinary_decline("Manufactured attribution: no one spoke."),
        _reask_response(ground_kind="none"),
    ]
    judgment, _ = _judge(mock_client, content, "")
    assert judgment.decision == "decline"
    assert judgment.attribution_recheck["ground_kind"] == "none"


def test_reask_failure_leaves_the_first_decline_standing() -> None:
    """Blocker 2 (architect review of 10327f6): an unreadable re-ask must
    NOT replace a valid first decline with a judge_failure verdict — that
    carries pending/rejudge semantics the design's own "fails closed to
    today's behaviour" line does not call for. The FIRST decline stands,
    unchanged in kind, with a note appended."""
    mock_client = MagicMock()
    content = "procurement only approves orders under $500."
    mock_client.messages.create.side_effect = [
        _ordinary_decline("Manufactured attribution: no one spoke."),
        RuntimeError("judge is unavailable"),
    ]
    judgment, _ = _judge(mock_client, content, "")
    assert judgment.decision == "decline"
    assert judgment.judge_failure is False
    assert "recheck failed" in judgment.reasoning
    assert judgment.attribution_recheck["ground_kind"] is None
    assert "recheck failed" in judgment.attribution_recheck["result"]


def test_reask_message_renders_the_current_context() -> None:
    """Blocker 1 (architect review of 10327f6): the re-ask's own render
    never showed the current context, so a judge's full-replacement
    `new_context` had no way to keep it. The re-ask message must carry it."""
    mock_client = MagicMock()
    content = "we decided to only escalate to SEV-1 if error rate exceeds 5%."
    mock_client.messages.create.side_effect = [
        _ordinary_decline("Manufactured attribution: no one spoke, no publication, no directive."),
        _reask_response(
            ground_kind="first_hand_own",
            span=content,
            new_context=CURRENT_SUMMARY.context + " Also: " + content,
        ),
    ]
    _judge(mock_client, content, "")
    reask_message = mock_client.messages.create.call_args_list[1].kwargs["messages"][0]["content"]
    assert CURRENT_SUMMARY.context in reask_message


def test_a_non_attribution_decline_never_triggers() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _ordinary_decline(
        "Declined: contradicts a binding directive."
    )
    judgment, _ = _judge(mock_client, "whatever", "")
    assert mock_client.messages.create.call_count == 1
    assert judgment.decision == "decline"
    assert judgment.attribution_recheck is None


def test_225s_own_manufactured_attribution_decline_does_not_re_trigger() -> None:
    """#225's OWN interior-assertion recheck can decline an originally-
    ACCEPTED judgment using the SAME words ("Manufactured attribution: no
    one spoke...") for an unrelated reason (a named other scope's assertion
    has no ground). That must not fire a SECOND re-ask here."""
    mock_client = MagicMock()
    content = "attr-other-scope decided to change its own deploy policy."
    mock_client.messages.create.side_effect = [
        _fake_response(
            {
                "decision": "accept_as_context",
                "reasoning": "Accepting this observation.",
                "directive_ops": [],
                "new_context": "updated",
            }
        ),
        _fake_response({"classification": "none", "reasoning": "No ground."}),
    ]
    judgment, _ = _judge(mock_client, content, "")
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "decline"
    assert judgment.interior_assertion is not None
    assert judgment.attribution_recheck is None


# ---------------------------------------------------------------------------
# 4. Input identity: the FIRST call is unaffected by whether the recheck
#    fires. (#225's own file compares against a pinned golden fixture; this
#    item has no pre-change baseline on this branch, so it compares two
#    first-call renders directly instead — a triggering decline vs. a
#    non-triggering one — which is exactly what the recheck's OWN presence
#    could possibly perturb, since the trigger reads only the first
#    response's reasoning, never anything that could change the request.)
# ---------------------------------------------------------------------------


def test_first_call_is_identical_whether_or_not_the_recheck_fires() -> None:
    mock_client_a = MagicMock()
    mock_client_a.messages.create.return_value = _ordinary_decline(
        "Manufactured attribution: no one spoke."
    )
    mock_client_b = MagicMock()
    mock_client_b.messages.create.return_value = _ordinary_decline(
        "Declined: contradicts a binding directive."
    )
    content = "procurement only approves orders under $500."
    _judge(mock_client_a, content, "")
    _judge(mock_client_b, content, "")

    first_call_a = mock_client_a.messages.create.call_args_list[0].kwargs
    first_call_b = mock_client_b.messages.create.call_args_list[0].kwargs
    assert first_call_a["messages"] == first_call_b["messages"]
    assert first_call_a["system"] == first_call_b["system"]
    assert first_call_a["tools"] == first_call_b["tools"]
