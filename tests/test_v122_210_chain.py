"""#210 re-ruling (CEO 2026-11-23, Philis c_e36aad252937eba4): relevance is measured against
the scope's own ``description``, else the nearest ancestor's, up to the root, else NOT AT ALL.
The own-memory implied purpose is no longer a decline ground (it was circular: what a scope
holds decided what could enter it).

The whole change sits behind ``_RELEVANCE_CHAIN`` (an internal switch, tested in both
positions): True is the 1.22 behaviour, False restores the shipped own-memory behaviour
byte-for-byte until the C5 verdict, after which the dead path is removed.
"""

from __future__ import annotations

import dataclasses

import pytest

from strata import scope_manager as sm
from strata.fleet_config import EntitlementView, FleetConfig, Scope, Stratum
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import (
    _BATCH_SYSTEM_PROMPT,
    _SYSTEM_PROMPT,
    ActedOnTarget,
    _build_user_message,
)
from strata.summary_store import ScopeSummary

_STRATUM = Stratum(id="L2", name="team", ordinal=2)
ROOT = Scope(id="g_root", name="Root", stratum_id="L0", description="The whole fleet: payments.")
MID = Scope(id="g_mid", name="Mid", stratum_id="L1", description="Billing: invoices and payouts.")
MID_BLANK = Scope(id="g_mid", name="Mid", stratum_id="L1")
LEAF = Scope(id="g_leaf", name="Leaf", stratum_id="L2")
LEAF_OWN = Scope(id="g_leaf", name="Leaf", stratum_id="L2", description="Leaf: refunds only.")
ROOT_BLANK = Scope(id="g_root", name="Root", stratum_id="L0")

_POPULATED = (
    "The billing service issues invoices, retries failed card payments three times and "
    "reconciles payouts nightly. Invoices are stored in the ledger database and exported "
    "to the finance team as CSV every month. Refunds are approved by support leads and "
    "recorded against the original invoice. Tax rates come from the pricing service and "
    "are cached for a day. Payment provider webhooks arrive out of order, so handlers "
    "must be idempotent."
)
_MARKERS = ("RELEVANCE", "SCOPE PURPOSE", "stated purpose", "implied by")


def _summary(context: str = "") -> ScopeSummary:
    return ScopeSummary(
        scope_id="g_leaf",
        directives=[],
        context=context,
        updated_at="2026-11-23T00:00:00+00:00",
        version=1,
    )


def _contribution(acted_on: str | None = None) -> Contribution:
    return Contribution(
        id="c_new",
        scope_id="g_leaf",
        content="The office coffee machine is broken.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_leaf", skill=None, session_id="s", ts="2026-11-23T00:00:00+00:00"
        ),
        created_at="2026-11-23 00:00:00",
        acted_on=acted_on,
    )


def _message(
    scope: Scope,
    chain_above: list[Scope],
    summary: ScopeSummary | None = None,
    *,
    mode: str = "ordinary",
    acted_on_target: ActedOnTarget | None = None,
) -> str:
    entitlement = EntitlementView(
        chain=[*chain_above, scope], descendants=[], referenced_peers=[], others=[]
    )
    text = _build_user_message(
        scope=scope,
        stratum=_STRATUM,
        ancestor_directives=None,
        current_summary=summary,
        recent_contributions=[],
        new_contribution=_contribution(),
        entitlement=entitlement,
        mode=mode,  # type: ignore[arg-type]
        acted_on_target=acted_on_target,
    )
    return " ".join(text.split())


# --- the fallback chain on the fleet ------------------------------------------------


def _fleet(*scopes: Scope) -> FleetConfig:
    return FleetConfig.model_validate(
        {
            "strata": [
                {"id": "L0", "name": "exec", "ordinal": 0},
                {"id": "L1", "name": "function", "ordinal": 1},
                {"id": "L2", "name": "team", "ordinal": 2},
            ],
            "scopes": [s.model_dump(exclude_none=True) for s in scopes],
            "edges": [
                {"from": "g_mid", "to": "g_root", "kind": "chain"},
                {"from": "g_leaf", "to": "g_mid", "kind": "chain"},
            ],
        }
    )


def test_a_scope_with_its_own_description_uses_it() -> None:
    found = _fleet(ROOT, MID, LEAF_OWN).effective_description("g_leaf")
    assert found == ("Leaf: refunds only.", "g_leaf")


def test_a_scope_without_one_inherits_the_nearest_ancestors() -> None:
    found = _fleet(ROOT, MID, LEAF).effective_description("g_leaf")
    assert found == ("Billing: invoices and payouts.", "g_mid")


def test_the_chain_runs_up_to_the_root() -> None:
    found = _fleet(ROOT, MID_BLANK, LEAF).effective_description("g_leaf")
    assert found == ("The whole fleet: payments.", "g_root")


def test_no_description_anywhere_is_none() -> None:
    assert _fleet(ROOT_BLANK, MID_BLANK, LEAF).effective_description("g_leaf") is None


# --- the judge prompt ---------------------------------------------------------------


def test_an_own_description_is_the_purpose() -> None:
    message = _message(LEAF_OWN, [ROOT, MID])
    assert "SCOPE PURPOSE: Leaf: refunds only." in message


def test_the_nearest_ancestors_description_is_used_and_named() -> None:
    message = _message(LEAF, [ROOT, MID])
    assert "Billing: invoices and payouts." in message
    assert "The whole fleet: payments." not in message
    assert "g_mid" in message.split("SCOPE PURPOSE")[1].split("RELEVANCE")[0]
    assert "begin \"Outside this scope's stated purpose" in message


def test_a_root_description_bounds_a_whole_fleet() -> None:
    message = _message(LEAF, [ROOT, MID_BLANK])
    assert "The whole fleet: payments." in message


def test_no_description_in_the_chain_means_no_relevance_rule_at_all() -> None:
    for summary in (None, _summary(), _summary(_POPULATED)):
        for prompt in (_message(LEAF, [ROOT_BLANK, MID_BLANK], summary), _SYSTEM_PROMPT):
            for marker in _MARKERS:
                assert marker not in prompt, marker
    assert _BATCH_SYSTEM_PROMPT


def test_a_populated_scope_with_no_description_is_no_longer_judged_by_its_memory() -> None:
    message = _message(LEAF, [ROOT_BLANK, MID_BLANK], _summary(_POPULATED))
    assert "implied" not in message
    assert "RELEVANCE" not in message


def test_the_three_never_decline_cases_are_in_the_rule() -> None:
    message = _message(LEAF_OWN, [ROOT, MID])
    assert "Never decline as irrelevant" in message
    assert "outcome report" in message
    assert "directive this scope holds" in message
    assert "withdrawal" in message


def test_an_outcome_report_gets_no_relevance_rule() -> None:
    target = ActedOnTarget(
        contribution=dataclasses.replace(_contribution(), id="c_target"),
        decision="accept_as_context",
    )
    message = _message(LEAF_OWN, [ROOT, MID], acted_on_target=target)
    for marker in ("RELEVANCE", "SCOPE PURPOSE"):
        assert marker not in message


def test_an_input_change_refresh_has_nothing_to_judge_for_relevance() -> None:
    message = _message(LEAF_OWN, [ROOT, MID], mode="input_change_refresh")
    assert "RELEVANCE" not in message


# --- the switch ---------------------------------------------------------------------


def test_the_switch_is_on_by_default() -> None:
    assert sm._RELEVANCE_CHAIN is True


def test_switch_off_restores_the_shipped_own_memory_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sm, "_RELEVANCE_CHAIN", False)
    message = _message(LEAF, [ROOT, MID], _summary(_POPULATED))
    # No own description: the ancestors' are NOT used; the own memory implies a purpose.
    assert "Billing: invoices and payouts." not in message
    assert "implied by this scope's existing memory" in message


def test_switch_off_without_memory_carries_no_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "_RELEVANCE_CHAIN", False)
    message = _message(LEAF, [ROOT, MID], _summary("Short."))
    for marker in _MARKERS:
        assert marker not in message


def test_switch_off_an_own_description_is_the_shipped_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sm, "_RELEVANCE_CHAIN", False)
    message = _message(LEAF_OWN, [ROOT, MID])
    assert "SCOPE PURPOSE: Leaf: refunds only." in message
    assert "Never decline as irrelevant" not in message
