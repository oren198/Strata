"""#210: relevance is measured against the scope's own ``description``, else NOT AT ALL.
The own-memory implied purpose is not a decline ground (it was circular: what a scope holds
decided what could enter it), and an ancestor's description is not used.
"""

from __future__ import annotations

from strata.fleet_config import EntitlementView, Scope, Stratum
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


# --- the judge prompt ---------------------------------------------------------------


def test_an_own_description_is_the_purpose() -> None:
    message = _message(LEAF_OWN, [ROOT, MID])
    assert "SCOPE PURPOSE: Leaf: refunds only." in message


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


def test_an_input_change_refresh_has_nothing_to_judge_for_relevance() -> None:
    message = _message(LEAF_OWN, [ROOT, MID], mode="input_change_refresh")
    assert "RELEVANCE" not in message
