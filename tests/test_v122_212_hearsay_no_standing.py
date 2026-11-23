"""#212 backstop 3 (ADR 0016 D3, Philis's ruling (b)): hearsay about B's item counts for
NOTHING toward B's standing. No echo (Concept 8): trust comes from outcomes, not repetition.
A hearsay outcome never appears in ``standing_evidence``, and it does not revive when the
item it reported on is later replaced or withdrawn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.record_store import ContributorRef, RecordStore, standing_evidence
from strata.summary_store import SummaryStore

_FLEET = {
    "strata": [
        {"id": "L0", "name": "executive", "ordinal": 0},
        {"id": "L1", "name": "function", "ordinal": 1},
    ],
    "scopes": [
        {"id": "g_source", "name": "Source", "stratum_id": "L1"},
        {"id": "g_reporter", "name": "Reporter", "stratum_id": "L1"},
    ],
    "edges": [],
}
HEARSAY_NOTES = (
    "accept_as_context. Hearsay: Priya, a platform engineer, told the agent it held; context only."
)
INFORMANT_NOTES = (
    "accept_as_context. A named engineer relayed that it held. "
    "interior assertion: g_source (Source), informant, admitted (context replaced)"
)
FIRST_HAND_NOTES = "accept_as_context. The reporter acted on it and it held."


@pytest.fixture
def store(tmp_path: Path) -> RecordStore:
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    return RecordStore(db_path)


@pytest.fixture
def fleet() -> FleetConfig:
    return FleetConfig.model_validate(_FLEET)


@pytest.fixture
def summary_store(tmp_path: Path) -> SummaryStore:
    return SummaryStore(str(tmp_path / "summaries"))


def _who(scope_id: str, session: str = "s1") -> ContributorRef:
    return ContributorRef(
        scope_id=scope_id, skill="engineer", session_id=session, ts="2026-01-01T00:00:00Z"
    )


def _target(store: RecordStore):
    target = store.append_contribution(
        scope_id="g_source",
        content="Release tags use rel-, never v.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=_who("g_source"),
    )
    store.record_judgment(
        contribution_id=target.id, decision="accept_as_context", judged_by="scope-manager"
    )
    return target


def _outcome(store: RecordStore, target_id: str, notes: str, *, session: str = "s1"):
    outcome = store.append_contribution(
        scope_id="g_reporter",
        content="Acted on it; it held.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=_who("g_reporter", session),
        acted_on=target_id,
    )
    store.record_judgment(
        contribution_id=outcome.id,
        decision="accept_as_context",
        judged_by="scope-manager",
        notes=notes,
    )
    return outcome


def test_a_first_hand_outcome_still_counts(store, fleet, summary_store) -> None:
    target = _target(store)
    outcome = _outcome(store, target.id, FIRST_HAND_NOTES)
    got = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert [e.contribution_id for e in got] == [outcome.id]


def test_a_hearsay_labelled_outcome_counts_for_nothing(store, fleet, summary_store) -> None:
    target = _target(store)
    _outcome(store, target.id, HEARSAY_NOTES)
    assert standing_evidence(store, fleet, target.id, summary_store=summary_store) == []


def test_a_verified_informant_outcome_counts_for_nothing(store, fleet, summary_store) -> None:
    target = _target(store)
    _outcome(store, target.id, INFORMANT_NOTES)
    assert standing_evidence(store, fleet, target.id, summary_store=summary_store) == []


def test_repeating_the_hearsay_never_adds_up(store, fleet, summary_store) -> None:
    target = _target(store)
    for n in range(3):
        _outcome(store, target.id, HEARSAY_NOTES, session=f"s{n}")
    assert standing_evidence(store, fleet, target.id, summary_store=summary_store) == []


def test_hearsay_beside_a_first_hand_outcome_leaves_only_the_first_hand(
    store, fleet, summary_store
) -> None:
    target = _target(store)
    _outcome(store, target.id, HEARSAY_NOTES, session="s1")
    own = _outcome(store, target.id, FIRST_HAND_NOTES, session="s2")
    got = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert [e.contribution_id for e in got] == [own.id]


def test_hearsay_does_not_revive_when_the_item_is_replaced(store, fleet, summary_store) -> None:
    target = _target(store)
    hearsay = _outcome(store, target.id, HEARSAY_NOTES)
    own = _outcome(store, target.id, FIRST_HAND_NOTES, session="s2")
    replacement = store.append_contribution(
        scope_id="g_source",
        content="Release tags use v-, not rel-.",
        proposed_classification="context",
        subject=None,
        supersedes=target.id,
        contributor=_who("g_source"),
    )
    store.record_judgment(
        contribution_id=replacement.id, decision="accept_as_context", judged_by="scope-manager"
    )
    got = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    ids = [e.contribution_id for e in got]
    assert own.id in ids
    assert hearsay.id not in ids


def test_a_hearsay_word_without_the_label_is_not_the_label(store, fleet, summary_store) -> None:
    target = _target(store)
    outcome = _outcome(store, target.id, "accept_as_context. It is not hearsay: I ran it myself.")
    got = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert [e.contribution_id for e in got] == [outcome.id]
