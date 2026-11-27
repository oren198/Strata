"""#210 (removals): what ships. A scope is judged for relevance against ITS OWN description
only. An ancestor's description is never used, there is no never-decline sentence and no
outcome-report exemption (the block is the 1.21 text), and a scope's own memory is never an
implied purpose to decline: a scope with no description of its own gets no relevance block,
whatever its memory or its ancestors say.
"""

from __future__ import annotations

import dataclasses

from strata import scope_manager as sm
from strata.fleet_config import EntitlementView, Scope, Stratum
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import ActedOnTarget, _build_user_message
from strata.summary_store import ScopeSummary

_STRATUM = Stratum(id="L2", name="team", ordinal=2)
ROOT = Scope(id="g_root", name="Root", stratum_id="L0", description="The whole fleet: payments.")
LEAF = Scope(id="g_leaf", name="Leaf", stratum_id="L2")
LEAF_OWN = Scope(id="g_leaf", name="Leaf", stratum_id="L2", description="Leaf: refunds only.")
ROOT_BLANK = Scope(id="g_root", name="Root", stratum_id="L0")
_MEMORY = (
    "The billing service issues invoices, retries failed card payments three times and "
    "reconciles payouts nightly. Invoices are stored in the ledger database and exported "
    "to the finance team as CSV every month. Refunds are approved by support leads and "
    "recorded against the original invoice. Tax rates come from the pricing service and "
    "are cached for a day. Payment provider webhooks arrive out of order, so handlers "
    "must be idempotent."
)


def _contribution() -> Contribution:
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
    )


def _message(scope, above, memory="", *, outcome=False) -> str:
    summary = ScopeSummary(
        scope_id="g_leaf",
        directives=[],
        context=memory,
        updated_at="2026-11-23T00:00:00+00:00",
        version=1,
    )
    target = (
        ActedOnTarget(
            contribution=dataclasses.replace(_contribution(), id="c_target"),
            decision="accept_as_context",
        )
        if outcome
        else None
    )
    text = _build_user_message(
        scope=scope,
        stratum=_STRATUM,
        ancestor_directives=None,
        current_summary=summary,
        recent_contributions=[],
        new_contribution=_contribution(),
        entitlement=EntitlementView(
            chain=[*above, scope], descendants=[], referenced_peers=[], others=[]
        ),
        acted_on_target=target,
    )
    return " ".join(text.split())


def test_an_own_description_renders_the_1_21_relevance_text() -> None:
    message = _message(LEAF_OWN, [ROOT])
    assert "SCOPE PURPOSE: Leaf: refunds only." in message
    assert "RELEVANCE (an additional decline ground for this scope)" in message
    assert "Never decline" not in message
    assert "stated by the ancestor" not in message


def test_an_inherited_description_is_not_used() -> None:
    message = _message(LEAF, [ROOT])
    assert "The whole fleet: payments." not in message
    assert "RELEVANCE" not in message and "SCOPE PURPOSE" not in message


def test_an_outcome_report_keeps_the_relevance_block_as_in_1_21() -> None:
    assert "RELEVANCE" in _message(LEAF_OWN, [ROOT], outcome=True)


def test_a_scope_with_little_memory_never_gets_an_implied_rule() -> None:
    assert "RELEVANCE" not in _message(LEAF, [ROOT_BLANK], "Short.")


def test_a_populated_scope_with_no_description_gets_no_relevance_block() -> None:
    message = _message(LEAF, [ROOT_BLANK], _MEMORY)
    assert "RELEVANCE" not in message
    assert "implied by this scope's existing memory" not in message
    message = _message(LEAF, [ROOT], _MEMORY)  # an ancestor description does not revive it
    assert "RELEVANCE" not in message and "payments." not in message.split("CURRENT SUMMARY")[0]


def test_the_relevance_switches_are_gone() -> None:
    for name in ("_RELEVANCE_ANCESTOR_FALLBACK", "_RELEVANCE_DROP_IMPLIED", "_RELEVANCE_CHAIN"):
        assert not hasattr(sm, name)
