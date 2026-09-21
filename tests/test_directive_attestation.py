"""v1.14 M1 — the judge attests which binding directives it weighed (ADR 0016 D5, #212).

Prose asking the judge to "check the directives" was measured and failed: it wrote "Directive
check: none restricts this class" with the restricting directive in front of it. So the check is a
field of the verdict, and a verdict that names none of the binding directives rendered is
malformed — mechanically, no LLM.

Rules pinned here:
- the binding set is the ANCESTOR DIRECTIVES plus OPERATOR MEMORY directives rendered to the call;
- rendered directives + a verdict naming none = one corrective re-ask naming the ids (the re-ask may
  change the verdict — that is the point);
- after that one re-ask, an unattested verdict is USED AS RETURNED and noted, never declined for
  the judge's failure (the contributor did nothing wrong);
- a decline grounded in a directive the scope does not hold is rejected the same way;
- the re-ask shares the protocol re-ask's single budget (no new call budget).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from strata.fleet_config import Scope, Stratum
from strata.operator import OperatorItem
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import (
    _SYSTEM_PROMPT,
    JUDGE_TOOL,
    ScopeManager,
    ScopeManagerJudgment,
    _rendered_binding_directive_ids,
)
from strata.summary_store import Directive, ScopeSummary

STRATUM = Stratum(id="L2", name="team", ordinal=2)
SCOPE = Scope(id="s_support_docs", name="support-docs", stratum_id="L2")
CONTRIBUTOR = ContributorRef(
    scope_id=SCOPE.id, skill="engineer", session_id="s1", ts="2026-07-07T09:00:00+00:00"
)
CONTRIBUTION = Contribution(
    id="c_flag01",
    scope_id=SCOPE.id,
    content="The people who own the account records mentioned account #A-88213 was flagged.",
    proposed_classification="context",
    subject="flagged-account-context",
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-07-07T09:00:00+00:00",
)
SUMMARY = ScopeSummary(
    scope_id=SCOPE.id, directives=[], context="", updated_at="2026-07-01T00:00:00Z"
)

RESTRICTING = Directive(
    id="d_records_001",
    content="Customer account states are held only in billing.",
    subject="customer-account-state-scope",
    source_scope_id="s_product_eng",
    source_skill="eng-director",
    created_at="2026-05-01T09:00:00+00:00",
)
OTHER = Directive(
    id="d_style_002",
    content="Docs use plain language.",
    subject="style",
    source_scope_id="s_product_eng",
    source_skill="eng-director",
    created_at="2026-05-02T09:00:00+00:00",
)
WALK = [("s_product_eng", [RESTRICTING, OTHER])]
OP_DIRECTIVE = OperatorItem(
    id="op_tls1",
    kind="directive",
    content="All services use TLS 1.3.",
    subject="tls",
    created_at="2026-01-01T00:00:00+00:00",
)
OP_CONTEXT = OperatorItem(
    id="op_ctx1",
    kind="context",
    content="Review due Q3.",
    subject=None,
    created_at="2026-01-02T00:00:00+00:00",
)


def _resp(**payload) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.input = payload
    r = MagicMock()
    r.content = [block]
    return r


def _accept(**extra) -> dict:
    return {
        "decision": "accept_as_context",
        "reasoning": "A person told the agent; admitted as hearsay context.",
        "directive_ops": [],
        "new_context": "Billing's record owners report account #A-88213 was flagged.",
        **extra,
    }


def _decline(**extra) -> dict:
    return {
        "decision": "decline",
        "reasoning": "Declined by directive d_records_001.",
        "directive_ops": None,
        "new_context": None,
        **extra,
    }


def _judge(*payloads: dict, walk=WALK, operator_memory=None):
    client = MagicMock()
    client.messages.create.side_effect = [_resp(**p) for p in payloads]
    judgment = ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        ancestor_directives=walk,
        current_summary=SUMMARY,
        recent_contributions=[],
        new_contribution=CONTRIBUTION,
        operator_memory=operator_memory,
    )
    return judgment, client


def _followup_text(client: MagicMock, call: int = 1) -> str:
    content = client.messages.create.call_args_list[call].kwargs["messages"][-1]["content"]
    return " ".join(b["text"] for b in content if b.get("type") == "text")


# --- the schema and the prompt ------------------------------------------------------------


def test_the_tool_carries_the_two_attestation_fields() -> None:
    props = JUDGE_TOOL["input_schema"]["properties"]
    assert props["directives_weighed"]["type"] == ["array", "null"]
    assert props["declined_by_directive"]["type"] == ["string", "null"]
    assert "weighed" in props["directives_weighed"]["description"]
    assert "restriction" in props["declined_by_directive"]["description"]


def test_the_system_prompt_points_at_the_fields() -> None:
    flat = " ".join(_SYSTEM_PROMPT.split())
    assert "`directives_weighed`" in flat
    assert "`declined_by_directive`" in flat


# --- the binding set ----------------------------------------------------------------------


def test_the_binding_set_is_ancestor_directives_plus_operator_directives() -> None:
    ids = _rendered_binding_directive_ids(WALK, [("g_exec", [OP_DIRECTIVE, OP_CONTEXT])])
    assert ids == ["d_records_001", "d_style_002", "op_tls1"]  # operator CONTEXT does not bind


def test_the_binding_set_is_empty_with_nothing_rendered() -> None:
    assert _rendered_binding_directive_ids(None, None) == []
    assert _rendered_binding_directive_ids([("a", [])], []) == []


# --- attested on the first call -----------------------------------------------------------


def test_an_attesting_verdict_needs_no_reask_and_records_what_was_weighed() -> None:
    j, client = _judge(_accept(directives_weighed=["d_records_001", "d_style_002"]))
    assert client.messages.create.call_count == 1
    assert j.attestation == "attested"
    assert j.directives_weighed == ["d_records_001", "d_style_002"]
    assert "Directives weighed: d_records_001, d_style_002 (2 of 2 rendered)" in j.record_notes


def test_a_subset_is_visible_as_a_subset() -> None:
    j, _ = _judge(_accept(directives_weighed=["d_style_002"]))
    assert "Directives weighed: d_style_002 (1 of 2 rendered)" in j.record_notes


def test_a_decline_by_directive_names_its_ground_in_the_record() -> None:
    j, client = _judge(
        _decline(directives_weighed=["d_records_001"], declined_by_directive="d_records_001")
    )
    assert client.messages.create.call_count == 1
    assert j.decision == "decline"
    assert j.declined_by_directive == "d_records_001"
    assert "Declined by directive: d_records_001" in j.record_notes


def test_nothing_rendered_means_nothing_to_attest() -> None:
    j, client = _judge(_accept(), walk=None)
    assert client.messages.create.call_count == 1
    assert j.attestation == "not_required"
    assert "Directives weighed" not in j.record_notes


# --- the malformed verdict and its one re-ask ---------------------------------------------


def test_naming_none_of_the_rendered_directives_earns_one_reask_naming_the_ids() -> None:
    j, client = _judge(_accept(), _accept(directives_weighed=["d_records_001", "d_style_002"]))
    assert client.messages.create.call_count == 2
    text = _followup_text(client)
    assert "d_records_001" in text and "d_style_002" in text
    assert "directives_weighed" in text and "declined_by_directive" in text
    assert j.attestation == "attested"
    assert "Corrective re-ask" in j.record_notes


def test_the_reask_may_change_the_verdict_to_a_decline_by_directive() -> None:
    """The j4-822 case: weighing the directive is what changes the answer."""
    j, client = _judge(
        _accept(),
        _decline(directives_weighed=["d_records_001"], declined_by_directive="d_records_001"),
    )
    assert client.messages.create.call_count == 2
    assert j.decision == "decline"
    assert j.new_summary is None
    assert j.declined_by_directive == "d_records_001"
    assert "Declined by directive: d_records_001" in j.record_notes


def test_the_reask_is_neutral_it_does_not_ask_for_a_decline() -> None:
    _, client = _judge(_accept(), _accept(directives_weighed=["d_style_002"]))
    text = _followup_text(client)
    assert "if weighing them changes your verdict" in text.lower()
    assert "you must decline" not in text.lower()


def test_still_unattested_after_the_reask_the_verdict_is_used_as_returned_and_noted() -> None:
    """Criterion 3: never decline the contribution for the judge's failure."""
    j, client = _judge(_accept(), _accept())
    assert client.messages.create.call_count == 2  # one re-ask, never two
    assert j.decision == "accept_as_context"
    assert j.new_summary is not None
    assert j.new_context
    assert j.attestation == "not_attested"
    assert "Directive check not attested" in j.record_notes
    assert "Directives weighed: none (0 of 2 rendered)" in j.record_notes


def test_a_reask_that_cannot_be_parsed_keeps_the_first_verdict() -> None:
    client = MagicMock()
    client.messages.create.side_effect = [_resp(**_accept()), RuntimeError("endpoint down")]
    j = ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        ancestor_directives=WALK,
        current_summary=SUMMARY,
        recent_contributions=[],
        new_contribution=CONTRIBUTION,
    )
    assert isinstance(j, ScopeManagerJudgment)
    assert j.decision == "accept_as_context"
    assert j.attestation == "not_attested"


# --- a decline grounded in a directive the scope does not hold ----------------------------


def test_a_decline_naming_a_directive_not_rendered_is_rejected_the_same_way() -> None:
    j, client = _judge(
        _decline(directives_weighed=["d_records_001"], declined_by_directive="d_invented_999"),
        _decline(directives_weighed=["d_records_001"], declined_by_directive="d_records_001"),
    )
    assert client.messages.create.call_count == 2
    text = _followup_text(client)
    assert "d_invented_999" in text
    assert j.declined_by_directive == "d_records_001"
    assert j.attestation == "attested"


def test_a_ground_still_not_held_after_the_reask_is_dropped_and_noted_verdict_stands() -> None:
    j, client = _judge(
        _decline(directives_weighed=["d_records_001"], declined_by_directive="d_invented_999"),
        _decline(directives_weighed=["d_records_001"], declined_by_directive="d_invented_999"),
    )
    assert client.messages.create.call_count == 2
    assert j.decision == "decline"  # the verdict is the judge's; only the bogus ground goes
    assert j.declined_by_directive is None
    assert "d_invented_999" in j.record_notes and "not held" in j.record_notes
    assert j.attestation == "not_attested"


def test_a_ground_is_rejected_even_when_no_directive_was_rendered() -> None:
    j, client = _judge(
        _decline(declined_by_directive="d_invented_999"),
        _decline(declined_by_directive="d_invented_999"),
        walk=None,
    )
    assert client.messages.create.call_count == 2
    assert j.declined_by_directive is None
    assert "not held" in j.record_notes


def test_naming_a_directive_that_is_not_rendered_in_the_weighed_list_is_reasked_then_dropped() -> (
    None
):
    j, client = _judge(
        _accept(directives_weighed=["d_records_001", "d_ghost_404"]),
        _accept(directives_weighed=["d_records_001", "d_ghost_404"]),
    )
    assert client.messages.create.call_count == 2
    assert "d_ghost_404" in _followup_text(client)
    assert j.directives_weighed == ["d_records_001"]  # only ids the scope actually holds
    assert j.attestation == "not_attested"


def test_a_ground_on_an_accept_is_malformed() -> None:
    j, client = _judge(
        _accept(directives_weighed=["d_records_001"], declined_by_directive="d_records_001"),
        _accept(directives_weighed=["d_records_001"]),
    )
    assert client.messages.create.call_count == 2
    assert j.declined_by_directive is None
    assert j.attestation == "attested"


# --- one budget ---------------------------------------------------------------------------


def test_the_attestation_reask_shares_the_protocol_reask_budget() -> None:
    """A protocol slip already spent the one re-ask; the retry's missing attestation is noted."""
    slip = _decline()
    slip["new_context"] = "an amendment on a decline"  # decline carrying an amendment: a slip
    j, client = _judge(slip, _accept())
    assert client.messages.create.call_count == 2  # not 3
    assert j.attestation == "not_attested"
    assert "Directive check not attested" in j.record_notes


# --- operator directives bind too ---------------------------------------------------------


def test_operator_directives_are_part_of_the_binding_set() -> None:
    j, client = _judge(
        _accept(),
        _accept(directives_weighed=["op_tls1"]),
        walk=None,
        operator_memory=[("g_exec", [OP_DIRECTIVE, OP_CONTEXT])],
    )
    assert client.messages.create.call_count == 2
    assert "op_tls1" in _followup_text(client)
    assert "op_ctx1" not in _followup_text(client)
    assert j.directives_weighed == ["op_tls1"]


# --- stringified lists are tolerated, like the other list fields --------------------------


def test_a_stringified_weighed_list_is_coerced() -> None:
    j, client = _judge(_accept(directives_weighed='["d_records_001", "d_style_002"]'))
    assert client.messages.create.call_count == 1
    assert j.directives_weighed == ["d_records_001", "d_style_002"]


def test_stray_weighed_ids_with_nothing_rendered_cost_no_call() -> None:
    """Nothing to attest means nothing worth a re-ask; the strays are just dropped."""
    j, client = _judge(_accept(directives_weighed=["d_ghost_404"]), walk=None)
    assert client.messages.create.call_count == 1
    assert j.attestation == "not_required"
    assert j.directives_weighed == []
    assert "Directives weighed" not in j.record_notes
    assert "not attested" not in j.record_notes
