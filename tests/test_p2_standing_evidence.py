"""v1.15 P2, ADR 0017 — derived standing: `standing_evidence`.

Every rule from the plan's P2 section, each its own test. No score, count, weight or
rank anywhere in the return type — a list of evidence, read fresh from the record
every time, nothing cached and nothing stored (see test_p2_no_migration.py for the
write-nothing proof).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.record_store import ContributorRef, RecordStore, StandingEvidence, standing_evidence
from strata.summary_store import ScopeSummary, SummaryStore

_FLEET = {
    "strata": [
        {"id": "L0", "name": "executive", "ordinal": 0},
        {"id": "L1", "name": "function", "ordinal": 1},
    ],
    "scopes": [
        {"id": "g_source", "name": "Source", "stratum_id": "L1"},
        {"id": "g_reporter", "name": "Reporter", "stratum_id": "L1"},
        {"id": "g_unrelated", "name": "Unrelated", "stratum_id": "L1"},
    ],
    "edges": [],
}


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
    # ADR 0017 P4: standing_evidence now reads the holding scope's CURRENT summary to
    # tell "replaced" from "correction reported, owner not yet acted". No scope in
    # this fixture ever has a summary written unless a test writes one itself, so
    # `.read()` returns None everywhere by default — read as "the content is gone"
    # (see `_claim_content_absent`), which is exactly what every existing test here
    # already assumed before P4. Only the tests that specifically exercise the new
    # pending/paraphrase behaviour write a real summary.
    return SummaryStore(str(tmp_path / "summaries"))


def _contributor(scope_id: str, *, session: str = "s1") -> ContributorRef:
    return ContributorRef(
        scope_id=scope_id, skill="engineer", session_id=session, ts="2026-01-01T00:00:00Z"
    )


def _seed_target(
    store: RecordStore, *, scope_id: str = "g_source", decision: str = "accept_as_context"
) -> object:
    target = store.append_contribution(
        scope_id=scope_id,
        content="Release tags use rel-, never v.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=_contributor(scope_id),
    )
    store.record_judgment(contribution_id=target.id, decision=decision, judged_by="scope-manager")
    return target


def _seed_outcome(
    store: RecordStore,
    target_id: str,
    *,
    scope_id: str = "g_reporter",
    decision: str | None = "accept_as_context",
    session: str = "s1",
) -> object:
    outcome = store.append_contribution(
        scope_id=scope_id,
        content="Acted on it; it held.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=_contributor(scope_id, session=session),
        acted_on=target_id,
    )
    if decision is not None:
        store.record_judgment(
            contribution_id=outcome.id, decision=decision, judged_by="scope-manager"
        )
    return outcome


# --- no number anywhere in the shape --------------------------------------------------


def test_evidence_shape_has_no_numeric_field() -> None:
    for f in dataclasses.fields(StandingEvidence):
        assert f.type not in ("int", "float"), (f.name, f.type)


def test_returns_a_list_never_a_number(store, fleet, summary_store) -> None:
    target = _seed_target(store)
    _seed_outcome(store, target.id)
    result = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert isinstance(result, list)
    assert all(isinstance(e, StandingEvidence) for e in result)


# --- basic inclusion, provenance and independence -------------------------------------


def test_one_accepted_outcome_is_one_entry_with_full_provenance(
    store, fleet, summary_store
) -> None:
    target = _seed_target(store)
    outcome = _seed_outcome(store, target.id, session="sess_42")
    result = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert len(result) == 1
    e = result[0]
    assert e.contribution_id == outcome.id
    assert e.reporter_scope_id == "g_reporter"
    assert e.reporter_skill == "engineer"
    assert e.reporter_session_id == "sess_42"
    assert e.reported_at == outcome.created_at
    assert e.independent_of_source is True


def test_a_scopes_own_outcome_on_its_own_item_counts_but_is_not_independent(
    store, fleet, summary_store
) -> None:
    target = _seed_target(store, scope_id="g_source")
    _seed_outcome(store, target.id, scope_id="g_source")
    result = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert len(result) == 1
    assert result[0].independent_of_source is False


# --- declined / pending / judge-failed never appear -----------------------------------


def test_a_declined_outcome_does_not_appear(store, fleet, summary_store) -> None:
    target = _seed_target(store)
    _seed_outcome(store, target.id, decision="decline")
    assert standing_evidence(store, fleet, target.id, summary_store=summary_store) == []


def test_a_pending_unjudged_outcome_does_not_appear(store, fleet, summary_store) -> None:
    target = _seed_target(store)
    _seed_outcome(store, target.id, decision=None)
    assert standing_evidence(store, fleet, target.id, summary_store=summary_store) == []


def test_a_judge_failed_outcome_does_not_appear(store, fleet, summary_store) -> None:
    target = _seed_target(store)
    outcome = _seed_outcome(store, target.id, decision=None)
    store.record_judgment_attempt(
        contribution_id=outcome.id,
        error_class="KeyError",
        message="'reasoning'",
        outcome="judge_failed",
    )
    assert standing_evidence(store, fleet, target.id, summary_store=summary_store) == []


# --- never on a directive (D6) ---------------------------------------------------------


def test_a_target_accepted_as_directive_returns_nothing(store, fleet, summary_store) -> None:
    """The proposed_classification was context, but the judge accepted it as a directive
    — D6 reads the target's OWN judgment, not what was proposed."""
    target = _seed_target(store, decision="accept_as_directive")
    _seed_outcome(store, target.id)
    assert standing_evidence(store, fleet, target.id, summary_store=summary_store) == []


def test_a_target_declined_or_pending_returns_nothing(store, fleet, summary_store) -> None:
    target = store.append_contribution(
        scope_id="g_source",
        content="Never admitted.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=_contributor("g_source"),
    )
    assert standing_evidence(store, fleet, target.id, summary_store=summary_store) == []


# --- target later replaced: outcome stays, marked as history --------------------------


def test_an_outcome_whose_target_was_later_superseded_still_appears_marked(
    store, fleet, summary_store
) -> None:
    target = _seed_target(store)
    outcome = _seed_outcome(store, target.id)
    replacement = store.append_contribution(
        scope_id="g_source",
        content="Actually the tags are rel/, with a slash.",
        proposed_classification="context",
        subject=None,
        supersedes=target.id,
        contributor=_contributor("g_source"),
    )
    store.record_judgment(
        contribution_id=replacement.id, decision="accept_as_context", judged_by="scope-manager"
    )
    result = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert len(result) == 1
    assert result[0].contribution_id == outcome.id
    assert result[0].target_replaced is True


def test_an_outcome_whose_target_was_never_replaced_is_not_marked(
    store, fleet, summary_store
) -> None:
    target = _seed_target(store)
    _seed_outcome(store, target.id)
    result = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert result[0].target_replaced is False


def test_a_declined_supersession_attempt_does_not_mark_the_target_replaced(
    store, fleet, summary_store
) -> None:
    target = _seed_target(store)
    _seed_outcome(store, target.id)
    rejected_replacement = store.append_contribution(
        scope_id="g_source",
        content="A rejected correction.",
        proposed_classification="context",
        subject=None,
        supersedes=target.id,
        contributor=_contributor("g_source"),
    )
    store.record_judgment(
        contribution_id=rejected_replacement.id, decision="decline", judged_by="scope-manager"
    )
    result = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert result[0].target_replaced is False


# --- the outcome ITSELF superseded: drops out entirely (distinct from the above) -------


def test_an_outcome_that_was_itself_superseded_drops_out(store, fleet, summary_store) -> None:
    target = _seed_target(store)
    outcome = _seed_outcome(store, target.id)
    correction_of_outcome = store.append_contribution(
        scope_id="g_reporter",
        content="Correcting my own outcome report — it didn't actually hold.",
        proposed_classification="context",
        subject=None,
        supersedes=outcome.id,
        contributor=_contributor("g_reporter"),
    )
    store.record_judgment(
        contribution_id=correction_of_outcome.id,
        decision="accept_as_context",
        judged_by="scope-manager",
    )
    assert standing_evidence(store, fleet, target.id, summary_store=summary_store) == []


def test_an_outcome_superseded_by_a_declined_contribution_still_counts(
    store, fleet, summary_store
) -> None:
    target = _seed_target(store)
    outcome = _seed_outcome(store, target.id)
    rejected = store.append_contribution(
        scope_id="g_reporter",
        content="A rejected attempt to retract the outcome.",
        proposed_classification="context",
        subject=None,
        supersedes=outcome.id,
        contributor=_contributor("g_reporter"),
    )
    store.record_judgment(
        contribution_id=rejected.id, decision="decline", judged_by="scope-manager"
    )
    result = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert len(result) == 1
    assert result[0].contribution_id == outcome.id


# --- entitlement re-checked live, marked not excluded ----------------------------------


def test_entitlement_current_by_default_when_the_reporter_can_still_read_the_item(
    store, fleet, summary_store
) -> None:
    target = _seed_target(store, scope_id="g_source")
    _seed_outcome(store, target.id, scope_id="g_source")  # own-scope: always entitled
    result = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert result[0].reporter_entitlement_current is True


def test_a_reporter_no_longer_entitled_still_appears_but_marked(store, summary_store) -> None:
    """CEO add: P1 checked entitlement at WRITE time; the record is never rewritten.
    A later fleet change (the edge removed) must not make history disappear."""
    entitled_fleet = FleetConfig.model_validate(
        {
            "strata": [
                {"id": "L0", "name": "executive", "ordinal": 0},
                {"id": "L1", "name": "function", "ordinal": 1},
            ],
            "scopes": [
                {"id": "g_source", "name": "Source", "stratum_id": "L0"},
                {"id": "g_reporter", "name": "Reporter", "stratum_id": "L1"},
            ],
            # Chain convention: child (L1) -> parent (L0).
            "edges": [{"from": "g_reporter", "to": "g_source"}],
        }
    )
    target = _seed_target(store, scope_id="g_source")
    outcome = _seed_outcome(store, target.id, scope_id="g_reporter")

    # Confirm it reads as entitled under the fleet that had the edge...
    result_before = standing_evidence(store, entitled_fleet, target.id, summary_store=summary_store)
    assert result_before[0].reporter_entitlement_current is True

    # ...then the edge is removed (a fleet.yaml edit after the write).
    narrowed_fleet = FleetConfig.model_validate(
        {
            "strata": [
                {"id": "L0", "name": "executive", "ordinal": 0},
                {"id": "L1", "name": "function", "ordinal": 1},
            ],
            "scopes": [
                {"id": "g_source", "name": "Source", "stratum_id": "L0"},
                {"id": "g_reporter", "name": "Reporter", "stratum_id": "L1"},
            ],
            "edges": [],
        }
    )
    result_after = standing_evidence(store, narrowed_fleet, target.id, summary_store=summary_store)
    assert len(result_after) == 1, "the outcome must still appear — never silently dropped"
    assert result_after[0].contribution_id == outcome.id
    assert result_after[0].reporter_entitlement_current is False


# --- multiple outcomes, ordering ---------------------------------------------------------


def test_multiple_outcomes_all_appear_in_record_order(store, fleet, summary_store) -> None:
    target = _seed_target(store)
    first = _seed_outcome(store, target.id, session="s1")
    second = _seed_outcome(store, target.id, scope_id="g_unrelated", session="s2")
    result = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert [e.contribution_id for e in result] == [first.id, second.id]


# ---------------------------------------------------------------------------
# v1.15 P3 (ADR 0017) — the real disposition, read back through standing_evidence.
# ---------------------------------------------------------------------------


def _seed_outcome_with_disposition(
    store: RecordStore,
    *,
    target_id: str,
    decision: str,
    outcome_disposition: str,
    reporter_scope: str = "g_reporter",
) -> str:
    outcome = store.append_contribution(
        scope_id=reporter_scope,
        content="Used port 8443, the service refused; the right port is unknown.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=_contributor(reporter_scope),
        acted_on=target_id,
    )
    claim_event = None
    _kind_by_disposition = {
        "failed_corrected": "claim_corrected",
        "failed_superseded": "claim_superseded",
    }
    if outcome_disposition in _kind_by_disposition:
        from strata.record_store import ClaimEventInput

        claim_event = ClaimEventInput(
            change_id=f"chg_{outcome.id}",
            contribution_id=outcome.id,
            scope_id=reporter_scope,
            source_scope_id="g_source",
            item_id=target_id,
            kind=_kind_by_disposition[outcome_disposition],
            before="The service listens on port 8443.",
            after=outcome.content,
        )
    store.record_judgment(
        contribution_id=outcome.id,
        decision=decision,
        judged_by="scope-manager",
        claim_event=claim_event,
    )
    return outcome.id


def test_held_reads_back_as_no_replaced_kind(store, fleet, summary_store) -> None:
    target = _seed_target(store)
    outcome_id = _seed_outcome_with_disposition(
        store, target_id=target.id, decision="accept_as_context", outcome_disposition="held"
    )
    result = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert len(result) == 1
    assert result[0].contribution_id == outcome_id
    assert result[0].replaced_kind is None


def test_failed_corrected_reads_back_with_its_kind(store, fleet, summary_store) -> None:
    target = _seed_target(store)
    outcome_id = _seed_outcome_with_disposition(
        store,
        target_id=target.id,
        decision="accept_as_context",
        outcome_disposition="failed_corrected",
    )
    result = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert len(result) == 1
    assert result[0].contribution_id == outcome_id
    assert result[0].replaced_kind == "claim_corrected"


def test_failed_superseded_reads_back_with_its_kind(store, fleet, summary_store) -> None:
    target = _seed_target(store)
    outcome_id = _seed_outcome_with_disposition(
        store,
        target_id=target.id,
        decision="accept_as_context",
        outcome_disposition="failed_superseded",
    )
    result = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert len(result) == 1
    assert result[0].contribution_id == outcome_id
    assert result[0].replaced_kind == "claim_superseded"


def test_failed_corrected_reads_as_pending_while_the_owner_still_holds_the_claim(
    store, fleet, summary_store
) -> None:
    """ADR 0017 P4: an event alone is not enough — the holding scope ("g_source")
    still carries the target's exact content in its CURRENT summary, so nothing has
    actually been corrected yet from a reader's point of view. Never `replaced_kind`
    by assertion."""
    target = _seed_target(store, scope_id="g_source")
    summary_store.write(
        "g_source",
        ScopeSummary(
            scope_id="g_source",
            directives=[],
            # Verbatim, matching _seed_target's own content exactly.
            context="Release tags use rel-, never v.",
            updated_at="2026-01-01T00:00:00Z",
        ),
    )
    outcome_id = _seed_outcome_with_disposition(
        store,
        target_id=target.id,
        decision="accept_as_context",
        outcome_disposition="failed_corrected",
    )
    result = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert len(result) == 1
    assert result[0].contribution_id == outcome_id
    assert result[0].replaced_kind is None
    assert result[0].correction_pending_kind == "claim_corrected"


def test_failed_corrected_reads_as_replaced_once_the_owner_drops_the_claim(
    store, fleet, summary_store
) -> None:
    """The same setup, but the owner's summary has since moved on — the exact same
    scenario standing_evidence would see after the owner's own refresh withdraws or
    rewrites the claim (P4's fan-out). Now it reads as replaced."""
    target = _seed_target(store, scope_id="g_source")
    summary_store.write(
        "g_source",
        ScopeSummary(
            scope_id="g_source",
            directives=[],
            context="Release tag convention is under review.",
            updated_at="2026-01-01T00:01:00Z",
        ),
    )
    outcome_id = _seed_outcome_with_disposition(
        store,
        target_id=target.id,
        decision="accept_as_context",
        outcome_disposition="failed_corrected",
    )
    result = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert len(result) == 1
    assert result[0].contribution_id == outcome_id
    assert result[0].replaced_kind == "claim_corrected"
    assert result[0].correction_pending_kind is None


def test_a_paraphrase_before_any_correction_over_reads_as_replaced(
    store, fleet, summary_store
) -> None:
    """KNOWN over-approximation (same direction as #202), pinned rather than fixed
    (CEO/architect ruling): the verbatim substring test cannot tell a paraphrase from
    an actual drop. If the owner had already reworded the claim in its own words for
    an UNRELATED reason before this correction ever arrived, the exact bytes are gone
    and this reads `replaced_kind`, even though the owner never acted on THIS
    correction specifically. Over-disclosure, never under — the same trade #202
    already accepted."""
    target = _seed_target(store, scope_id="g_source")
    summary_store.write(
        "g_source",
        ScopeSummary(
            scope_id="g_source",
            directives=[],
            # Same fact, different words — not a real correction, just a rewrite.
            context="Release names should use the rel- prefix, not v.",
            updated_at="2026-01-01T00:01:00Z",
        ),
    )
    outcome_id = _seed_outcome_with_disposition(
        store,
        target_id=target.id,
        decision="accept_as_context",
        outcome_disposition="failed_corrected",
    )
    result = standing_evidence(store, fleet, target.id, summary_store=summary_store)
    assert len(result) == 1
    assert result[0].contribution_id == outcome_id
    assert result[0].replaced_kind == "claim_corrected"  # over-read; documented, not fixed


def test_decline_reads_back_as_absent_entirely(store, fleet, summary_store) -> None:
    target = _seed_target(store)
    _seed_outcome_with_disposition(
        store, target_id=target.id, decision="decline", outcome_disposition="decline"
    )
    assert standing_evidence(store, fleet, target.id, summary_store=summary_store) == []


def test_a_directive_target_never_gets_a_claim_event_and_standing_is_empty(
    store, fleet, summary_store
) -> None:
    """CEO ruling: an outcome against a directive target persists as accept_as_context
    with NO claim event (the engine never mints one for a directive) — and P2's
    standing_evidence on a directive target is empty regardless, per D6, which gates
    before any outcome is even read."""
    target = _seed_target(store, decision="accept_as_directive")
    _seed_outcome_with_disposition(
        store, target_id=target.id, decision="accept_as_context", outcome_disposition="held"
    )
    assert standing_evidence(store, fleet, target.id, summary_store=summary_store) == []
