"""#210 follow-up (CEO 2026-11-23): the relevance change is TWO internal switches, so a live
miss on one behaviour does not remove the other.

- ``_RELEVANCE_ANCESTOR_FALLBACK``: the ancestor-description fallback, the never-decline text,
  and the outcome-report exemption.
- ``_RELEVANCE_DROP_IMPLIED``: removes the own-memory implied-purpose decline.

Both default True (the 1.22 behaviour). All four positions are pinned here; (False, False)
reproduces 1.21 byte for byte.
"""

from __future__ import annotations

import dataclasses

import pytest

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
_POSITIONS = [(True, True), (True, False), (False, True), (False, False)]
_IDS = ["TT", "TF", "FT", "FF"]


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


@pytest.fixture
def position(request, monkeypatch):
    fallback, drop = request.param
    monkeypatch.setattr(sm, "_RELEVANCE_ANCESTOR_FALLBACK", fallback)
    monkeypatch.setattr(sm, "_RELEVANCE_DROP_IMPLIED", drop)
    return fallback, drop


def test_both_switches_default_to_the_1_22_behaviour() -> None:
    assert sm._RELEVANCE_ANCESTOR_FALLBACK is True
    assert sm._RELEVANCE_DROP_IMPLIED is True


# --- the ancestor fallback + never-decline text: only ANCESTOR_FALLBACK moves these ----


@pytest.mark.parametrize("position", _POSITIONS, ids=_IDS, indirect=True)
def test_an_inherited_description_follows_the_fallback_switch(position) -> None:
    fallback, _ = position
    message = _message(LEAF, [ROOT])
    assert ("The whole fleet: payments." in message) is fallback


@pytest.mark.parametrize("position", _POSITIONS, ids=_IDS, indirect=True)
def test_the_never_decline_text_follows_the_fallback_switch(position) -> None:
    fallback, _ = position
    message = _message(LEAF_OWN, [ROOT])
    assert "SCOPE PURPOSE: Leaf: refunds only." in message
    assert ("Never decline as irrelevant" in message) is fallback


@pytest.mark.parametrize("position", _POSITIONS, ids=_IDS, indirect=True)
def test_the_outcome_exemption_follows_the_fallback_switch(position) -> None:
    fallback, _ = position
    message = _message(LEAF_OWN, [ROOT], outcome=True)
    assert ("RELEVANCE" not in message) is fallback


# --- the implied purpose: only DROP_IMPLIED moves this ---------------------------------


@pytest.mark.parametrize("position", _POSITIONS, ids=_IDS, indirect=True)
def test_the_implied_purpose_follows_the_drop_switch(position) -> None:
    _, drop = position
    message = _message(LEAF, [ROOT_BLANK], _MEMORY)
    assert ("implied by this scope's existing memory" in message) is (not drop)


@pytest.mark.parametrize("position", _POSITIONS, ids=_IDS, indirect=True)
def test_a_scope_with_little_memory_never_gets_an_implied_rule(position) -> None:
    message = _message(LEAF, [ROOT_BLANK], "Short.")
    assert "RELEVANCE" not in message


@pytest.mark.parametrize("position", _POSITIONS, ids=_IDS, indirect=True)
def test_the_two_switches_do_not_leak_into_each_other(position) -> None:
    """An inherited description beats an implied purpose when the fallback is on; with the
    fallback off and the implied path kept, the own memory implies the purpose instead."""
    fallback, drop = position
    message = _message(LEAF, [ROOT], _MEMORY)
    assert ("The whole fleet: payments." in message) is fallback
    implied = "implied by this scope's existing memory" in message
    assert implied is (not fallback and not drop)


# --- FF reproduces 1.21 -----------------------------------------------------------------


def test_ff_is_the_1_21_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "_RELEVANCE_ANCESTOR_FALLBACK", False)
    monkeypatch.setattr(sm, "_RELEVANCE_DROP_IMPLIED", False)
    own = _message(LEAF_OWN, [ROOT])
    assert "SCOPE PURPOSE: Leaf: refunds only." in own
    assert "Never decline" not in own
    assert "stated by the ancestor" not in _message(LEAF, [ROOT])
    assert "implied by this scope's existing memory" in _message(LEAF, [ROOT], _MEMORY)
    assert "RELEVANCE" in _message(LEAF_OWN, [ROOT], outcome=True)
