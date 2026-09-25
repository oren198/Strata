"""Tests for the affected set and the emission of change events (ADR 0014).

Phase B of the ADR 0014 implementation: what a change to a composed input
touches (:func:`strata.change_events.affected_scopes`, D3) and what the
engine writes when one happens (:func:`strata.change_events.emit`, D4/D5).

Vocabulary follows CONTEXT.md: scope, contribution, record, publication,
directive, change event.
"""

from __future__ import annotations

import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

# Make strata importable when running from the repo root.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.change_events import (  # noqa: E402
    HOP_BUDGET,
    affected_scopes,
    emit,
)
from strata.fleet_config import FleetConfig  # noqa: E402
from strata.migrator import run_migrations  # noqa: E402
from strata.record_store import RecordStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# g_exec -> g_funcA -> {g_teamX, g_teamY}; g_funcB reads g_funcA and
# g_funcA reads g_funcB (a reference CYCLE, which ADR 0014 D4 exists for).
_FLEET_YAML = """
strata:
  - id: L0
    name: Executive
    ordinal: 0
  - id: L1
    name: Function
    ordinal: 1
  - id: L2
    name: Team
    ordinal: 2

scopes:
  - id: g_exec
    name: Executive
    stratum_id: L0
  - id: g_funcA
    name: Function A
    stratum_id: L1
  - id: g_funcB
    name: Function B
    stratum_id: L1
  - id: g_lonely
    name: Lonely
    stratum_id: L1
  - id: g_teamX
    name: Team X
    stratum_id: L2
  - id: g_teamY
    name: Team Y
    stratum_id: L2

edges:
  - from: g_funcA
    to: g_exec
    kind: chain
  - from: g_funcB
    to: g_exec
    kind: chain
  - from: g_lonely
    to: g_exec
    kind: chain
  - from: g_teamX
    to: g_funcA
    kind: chain
  - from: g_teamY
    to: g_funcA
    kind: chain
  - from: g_funcB
    to: g_funcA
    kind: reference
  - from: g_funcA
    to: g_funcB
    kind: reference
"""


@pytest.fixture
def fleet(tmp_path: Path) -> FleetConfig:
    path = tmp_path / "fleet.yaml"
    path.write_text(textwrap.dedent(_FLEET_YAML), encoding="utf-8")
    return FleetConfig.load(path)


@pytest.fixture
def record_store(tmp_path: Path):  # noqa: ANN201 — RecordStore context manager
    db_path = str(tmp_path / "record.db")
    run_migrations(db_path)
    with RecordStore(db_path) as store:
        yield store


# ---------------------------------------------------------------------------
# ADR 0014 D3 — the affected set, one topological rule for every kind
# ---------------------------------------------------------------------------


class TestAffectedScopes:
    def test_publication_change_reaches_chain_children_and_readers(
        self, fleet: FleetConfig
    ) -> None:
        """g_funcA's face composes into its chain children and into g_funcB, which reads it."""
        assert affected_scopes(
            fleet, item="pub_1", kind="withdrawn", source_scope_id="g_funcA"
        ) == ["g_funcB", "g_teamX", "g_teamY"]

    def test_an_addition_affects_exactly_what_a_withdrawal_does(self, fleet: FleetConfig) -> None:
        """ADR 0014 D1: additions trigger exactly as removals do — one rule, not two."""
        added = affected_scopes(fleet, item="pub_1", kind="published", source_scope_id="g_funcA")
        amended = affected_scopes(fleet, item="pub_1", kind="amended", source_scope_id="g_funcA")
        withdrawn = affected_scopes(
            fleet, item="pub_1", kind="withdrawn", source_scope_id="g_funcA"
        )

        assert added == withdrawn
        assert amended == withdrawn

    def test_publication_change_never_reaches_grandchildren(self, fleet: FleetConfig) -> None:
        """Publication travels exactly one edge (ADR 0013 D3) — a grandchild sees it
        only if the child relays it, which is the child's own act."""
        assert "g_teamX" not in affected_scopes(
            fleet, item="pub_1", kind="published", source_scope_id="g_exec"
        )

    def test_a_scope_with_no_children_and_no_readers_affects_nobody(
        self, fleet: FleetConfig
    ) -> None:
        assert (
            affected_scopes(fleet, item="pub_1", kind="published", source_scope_id="g_lonely") == []
        )

    def test_directive_change_reaches_every_chain_descendant(self, fleet: FleetConfig) -> None:
        """A directive binds the whole subtree, at every depth (ADR 0014 D3)."""
        assert affected_scopes(
            fleet, item="c_1", kind="directive_appended", source_scope_id="g_exec"
        ) == ["g_funcA", "g_funcB", "g_lonely", "g_teamX", "g_teamY"]

    def test_directive_change_excludes_the_holding_scope_itself(self, fleet: FleetConfig) -> None:
        """A scope's own contribution is not a trigger for the scope (ADR 0014 D1)."""
        assert "g_funcA" not in affected_scopes(
            fleet, item="c_1", kind="directive_retired", source_scope_id="g_funcA"
        )

    def test_directive_change_never_follows_a_reference_edge(self, fleet: FleetConfig) -> None:
        """g_funcB references g_funcA, but a reference delivers publication, not ancestry."""
        assert "g_funcB" not in affected_scopes(
            fleet, item="c_1", kind="directive_superseded", source_scope_id="g_funcA"
        )

    def test_operator_directive_change_includes_the_attachment_scope(
        self, fleet: FleetConfig
    ) -> None:
        """An operator directive attached at S binds S and its subtree (ADR 0008 D2)."""
        assert affected_scopes(
            fleet,
            item="op_1",
            kind="operator_directive_changed",
            source_scope_id="g_funcA",
        ) == ["g_funcA", "g_teamX", "g_teamY"]

    def test_an_operator_authored_directive_change_includes_the_scope(
        self, fleet: FleetConfig
    ) -> None:
        """Implementation pin 2 — the operator correcting S's own directive is
        an operator directive change on S: S and its descendants. Precondition
        first: the same kind, authored by the scope, excludes S."""
        assert "g_funcA" not in affected_scopes(
            fleet, item="c_1", kind="directive_superseded", source_scope_id="g_funcA"
        )

        assert affected_scopes(
            fleet,
            item="c_1",
            kind="directive_superseded",
            source_scope_id="g_funcA",
            by_operator=True,
        ) == ["g_funcA", "g_teamX", "g_teamY"]

    def test_a_reference_cycle_does_not_loop(self, fleet: FleetConfig) -> None:
        """g_funcA and g_funcB reference each other; the walk is one hop, so it
        terminates by construction and each scope appears at most once."""
        for source in ("g_funcA", "g_funcB"):
            result = affected_scopes(fleet, item="pub_1", kind="published", source_scope_id=source)
            assert len(result) == len(set(result))
            assert source not in result

    def test_unknown_source_scope_affects_nobody(self, fleet: FleetConfig) -> None:
        assert (
            affected_scopes(fleet, item="pub_1", kind="published", source_scope_id="g_nope") == []
        )

    def test_an_unknown_kind_is_refused(self, fleet: FleetConfig) -> None:
        """A kind outside the settled vocabulary has no affected-set rule, and
        guessing one would deliver notice to the wrong scopes."""
        with pytest.raises(ValueError, match="kind"):
            affected_scopes(fleet, item="pub_1", kind="deleted", source_scope_id="g_funcA")

    def test_claim_superseded_is_still_refused(self, fleet: FleetConfig) -> None:
        """ADR 0017 P4: claim_corrected gets its own rule; claim_superseded does not —
        P3's "recorded, no notice" stands, so it is still an unrecognised kind here."""
        with pytest.raises(ValueError, match="kind"):
            affected_scopes(fleet, item="c_1", kind="claim_superseded", source_scope_id="g_funcA")

    def test_claim_corrected_reaches_the_holding_scopes_readers_same_topology_as_publication(
        self, fleet: FleetConfig
    ) -> None:
        """ADR 0017 P4: who reads a corrected claim is who reads the holding scope's
        face — the same rule PUBLICATION_KINDS already uses."""
        assert affected_scopes(
            fleet, item="c_1", kind="claim_corrected", source_scope_id="g_funcA"
        ) == ["g_funcB", "g_teamX", "g_teamY"]

    def test_claim_corrected_by_owner_excludes_the_holding_scope_by_default(
        self, fleet: FleetConfig
    ) -> None:
        """Same-scope (the holding scope authored its own correction, `by_owner=True`
        the default): it is excluded from its own affected set exactly like any other
        self-authored retraction — reached instead by `emit`'s self-notice."""
        assert "g_funcA" not in affected_scopes(
            fleet, item="c_1", kind="claim_corrected", source_scope_id="g_funcA"
        )

    def test_claim_corrected_by_owner_false_notifies_the_holding_scope_alone(
        self, fleet: FleetConfig
    ) -> None:
        """Cross-scope (a DIFFERENT scope's outcome caused the correction,
        `by_owner=False`): the holding scope did nothing itself, so it is told as an
        ordinary reader owed a refresh — but its OWN readers (chain children,
        referencing peers) are NOT told directly here. Caught by strata-evals'
        unpublished-claim control: nobody but the holding scope may have actually
        seen the claim, so the fan-out to readers happens only through the holding
        scope's OWN subsequent withdrawal (`by_owner=True`), never from this call."""
        assert affected_scopes(
            fleet,
            item="c_1",
            kind="claim_corrected",
            source_scope_id="g_funcA",
            by_owner=False,
        ) == ["g_funcA"]


# ---------------------------------------------------------------------------
# ADR 0014 D4/D5 — emission
# ---------------------------------------------------------------------------


class TestEmit:
    def test_emits_a_notice_and_a_row_that_reference_each_other(
        self, fleet: FleetConfig, record_store: RecordStore
    ) -> None:
        """The notice IS the trigger row's other half (ADR 0014 D5)."""
        (change_id,) = emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_1",
            kind="withdrawn",
            source_scope_id="g_funcA",
            before="the old claim",
        )

        # An independent input change mints exactly ONE id.
        assert change_id.startswith("chg_")
        events = record_store.list_change_events(scope_id="g_teamX")
        assert len(events) == 1
        event = events[0]
        assert event.change_id == change_id
        assert event.item_id == "pub_1"
        assert event.kind == "withdrawn"
        # The affected scope and the source scope are different facts and
        # both are on the row: g_teamX must refresh, g_funcA is what changed.
        assert event.scope_id == "g_teamX"
        assert event.source_scope_id == "g_funcA"
        assert event.before == "the old claim"
        assert event.hop == 0
        assert event.processed_at is None

        contribution = record_store.get_contribution(event.contribution_id)
        assert contribution is not None
        assert contribution.scope_id == "g_teamX"
        assert contribution.subject == "manager-refresh"
        # The payload the judge is shown carries every field D5 names.
        assert change_id in contribution.content
        assert "pub_1" in contribution.content
        assert "g_funcA" in contribution.content
        assert "the old claim" in contribution.content

    def test_emits_one_row_per_affected_scope(
        self, fleet: FleetConfig, record_store: RecordStore
    ) -> None:
        emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_1",
            kind="published",
            source_scope_id="g_funcA",
            after="a new claim",
        )

        for scope_id in ("g_teamX", "g_teamY", "g_funcB"):
            assert len(record_store.list_change_events(scope_id=scope_id)) == 1
        # The source itself is not affected by its own act.
        assert record_store.list_change_events(scope_id="g_funcA") == []

    def test_a_derived_change_inherits_the_change_id(
        self, fleet: FleetConfig, record_store: RecordStore
    ) -> None:
        """Inheritance is the whole termination guarantee (ADR 0014 D4)."""
        (original,) = emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_1",
            kind="withdrawn",
            source_scope_id="g_funcB",
        )

        # g_funcA read that withdrawal and reacted by withdrawing its own
        # relayed copy — the derived change carries the SAME id.
        derived = emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_2",
            kind="withdrawn",
            source_scope_id="g_funcA",
            wave_ids=[original],
            hop=1,
        )

        assert derived == [original]
        # Precondition: the derived change really did reach somebody.
        events = record_store.list_change_events(scope_id="g_teamX")
        assert [e.change_id for e in events] == [original]

    def test_a_second_notice_for_a_pending_change_id_is_still_written_pending(
        self, fleet: FleetConfig, record_store: RecordStore
    ) -> None:
        """Once-per-id bounds the REFRESH, never the NOTICE (ADR 0014 D4/D5).

        A scope with a pending row for this change id takes the second notice
        pending too: the drain batches what is pending into ONE refresh
        (implementation pin 1), so the scope still refreshes once — and the
        record keeps both notices, because a notice that vanishes is not
        notice.
        """
        (change_id,) = emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_1",
            kind="withdrawn",
            source_scope_id="g_funcA",
        )
        # Precondition: the first emission landed, unprocessed.
        assert len(record_store.list_change_events(scope_id="g_funcB", unprocessed_only=True)) == 1

        emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_2",
            kind="withdrawn",
            source_scope_id="g_funcA",
            wave_ids=[change_id],
            hop=1,
        )

        events = record_store.list_change_events(scope_id="g_funcB")
        assert [e.item_id for e in events] == ["pub_1", "pub_2"]
        assert all(e.change_id == change_id for e in events)
        # Both pending: one batched refresh will consume them together.
        assert len(record_store.list_change_events(scope_id="g_funcB", unprocessed_only=True)) == 2

    def test_a_notice_for_an_already_processed_change_id_lands_processed(
        self, fleet: FleetConfig, record_store: RecordStore
    ) -> None:
        """A scope that already REFRESHED for this change id never refreshes
        again for it (ADR 0014 D4) — but it is still told what happened: the
        row is written, stamped processed at birth, and its notice says why
        no refresh follows."""
        (change_id,) = emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_1",
            kind="withdrawn",
            source_scope_id="g_funcA",
        )
        (event,) = record_store.list_change_events(scope_id="g_teamX")
        record_store.mark_change_event_processed(event.id)
        # Precondition: the refresh really did run for this id.
        assert record_store.list_change_events(scope_id="g_teamX", unprocessed_only=True) == []

        emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_2",
            kind="withdrawn",
            source_scope_id="g_funcA",
            wave_ids=[change_id],
            hop=1,
        )

        events = record_store.list_change_events(scope_id="g_teamX")
        assert [e.item_id for e in events] == ["pub_1", "pub_2"]
        # Recorded, never enqueued: no second refresh for one change id.
        assert record_store.list_change_events(scope_id="g_teamX", unprocessed_only=True) == []
        notice = record_store.get_contribution(events[-1].contribution_id)
        assert notice is not None
        assert f"already refreshed for {change_id}" in notice.content

    def test_the_same_item_is_not_announced_twice_for_one_change_id(
        self, fleet: FleetConfig, record_store: RecordStore
    ) -> None:
        """Dedup is per (change id, scope, item): a cascade that revisits the
        SAME item under the same wave adds nothing, while a different item
        under that wave is a genuinely new notice (the test above)."""
        (change_id,) = emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_1",
            kind="withdrawn",
            source_scope_id="g_funcA",
        )
        assert len(record_store.list_change_events(scope_id="g_teamX")) == 1

        emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_1",
            kind="withdrawn",
            source_scope_id="g_funcA",
            wave_ids=[change_id],
            hop=1,
        )

        assert len(record_store.list_change_events(scope_id="g_teamX")) == 1

    def test_a_different_change_id_is_not_skipped(
        self, fleet: FleetConfig, record_store: RecordStore
    ) -> None:
        """The once-per rule is per change id, not per scope."""
        emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_1",
            kind="withdrawn",
            source_scope_id="g_funcA",
        )
        emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_2",
            kind="withdrawn",
            source_scope_id="g_funcA",
        )

        assert len(record_store.list_change_events(scope_id="g_teamX")) == 2

    def test_beyond_the_hop_budget_the_event_is_recorded_but_not_enqueued(
        self, fleet: FleetConfig, record_store: RecordStore
    ) -> None:
        """ADR 0014 D4's backstop: hitting it is recorded, never silent, and the
        event lands already processed so no drain will ever run it."""
        emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_1",
            kind="withdrawn",
            source_scope_id="g_funcA",
            wave_ids=["chg_deep"],
            hop=HOP_BUDGET + 1,
        )

        (event,) = record_store.list_change_events(scope_id="g_teamX")
        # Precondition: the event IS in the record — recorded, per D4.
        assert event.change_id == "chg_deep"
        assert event.hop == HOP_BUDGET + 1
        # ...and nothing is queued for a refresh.
        assert event.processed_at is not None
        assert record_store.list_change_events(scope_id="g_teamX", unprocessed_only=True) == []
        contribution = record_store.get_contribution(event.contribution_id)
        assert contribution is not None
        assert "hop budget" in contribution.content.lower()

    def test_emitting_for_nobody_writes_nothing(
        self, fleet: FleetConfig, record_store: RecordStore
    ) -> None:
        """A scope with no children and no readers: precondition first — an
        emission from a scope that DOES have readers writes rows."""
        emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_1",
            kind="published",
            source_scope_id="g_funcA",
        )
        assert record_store.list_change_events(scope_id="g_teamX")

        emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_2",
            kind="published",
            source_scope_id="g_lonely",
        )

        assert record_store.list_contributions(scope_id="g_lonely") == []

    def test_a_store_failure_is_swallowed_and_logged(
        self,
        fleet: FleetConfig,
        record_store: RecordStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Emission must never fail the originating act (ADR 0014 D6: the writer
        writes its event and returns)."""

        def _boom(**_kwargs: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(record_store, "append_change_notice", _boom)

        with caplog.at_level("ERROR", logger="strata.change_events"):
            (change_id,) = emit(
                fleet=fleet,
                record_store=record_store,
                item="pub_1",
                kind="withdrawn",
                source_scope_id="g_funcA",
            )

        assert change_id.startswith("chg_")
        assert "database is locked" in caplog.text
        # The failure itself is in the record, as a note against the source
        # scope — a lost notice that leaves no trace is not acceptable.
        notes = [
            c
            for c in record_store.list_contributions(scope_id="g_funcA")
            if c.subject == "change-emission-failed"
        ]
        # One note per scope that was left uninformed — three affected scopes,
        # three undelivered notices, three notes naming them.
        assert len(notes) == 3
        assert all(change_id in note.content for note in notes)
        assert {"g_funcB", "g_teamX", "g_teamY"} == {
            scope_id
            for note in notes
            for scope_id in ("g_funcB", "g_teamX", "g_teamY")
            if scope_id in note.content
        }

        # The notice and its row are ONE write, so a failure leaves neither
        # half: no `manager-refresh` contribution nothing will ever judge.
        assert record_store.list_change_events(scope_id="g_teamX") == []
        assert [
            c
            for c in record_store.list_contributions(scope_id="g_teamX")
            if c.subject == "manager-refresh"
        ] == []

    def test_a_broken_traversal_is_swallowed_too(
        self,
        fleet: FleetConfig,
        record_store: RecordStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Not only a store failure: nothing about computing WHO to tell may
        fail the act that already succeeded (ADR 0014 D6)."""

        def _boom(_self: FleetConfig, _scope_id: str) -> list:
            raise RuntimeError("the fleet is unreadable")

        monkeypatch.setattr(FleetConfig, "chain_children", _boom)

        with caplog.at_level("ERROR", logger="strata.change_events"):
            (change_id,) = emit(
                fleet=fleet,
                record_store=record_store,
                item="pub_1",
                kind="withdrawn",
                source_scope_id="g_funcA",
            )

        assert change_id.startswith("chg_")
        assert "the fleet is unreadable" in caplog.text
        assert record_store.list_change_events(scope_id="g_teamX") == []

    # -----------------------------------------------------------------------
    # ADR 0017 P4 — claim_corrected's split fan-out (self-notice vs. cross-scope)
    # -----------------------------------------------------------------------

    def test_claim_corrected_same_scope_self_notices_and_does_not_enqueue_the_holder(
        self, fleet: FleetConfig, record_store: RecordStore
    ) -> None:
        """by_owner=True (the default): the holding scope authored its own
        correction, so it gets the #197-style self-notice (born processed, no
        refresh) — never an ordinary unprocessed row alongside its readers'."""
        (change_id,) = emit(
            fleet=fleet,
            record_store=record_store,
            item="c_1",
            kind="claim_corrected",
            source_scope_id="g_funcA",
            before="the old claim",
            after="the corrected claim",
        )

        for scope_id in ("g_teamX", "g_teamY", "g_funcB"):
            events = record_store.list_change_events(scope_id=scope_id)
            assert len(events) == 1
            assert events[0].kind == "claim_corrected"
            assert events[0].processed_at is None  # an ordinary, enqueued refresh

        holder_events = record_store.list_change_events(scope_id="g_funcA")
        assert len(holder_events) == 1
        assert holder_events[0].change_id == change_id
        assert holder_events[0].self_notice == 1
        assert holder_events[0].processed_at is not None  # born processed, no refresh
        assert "the corrected claim" in holder_events[0].after

    def test_claim_corrected_cross_scope_enqueues_the_holder_as_an_ordinary_reader(
        self, fleet: FleetConfig, record_store: RecordStore
    ) -> None:
        """by_owner=False: a DIFFERENT scope's outcome caused this — the holding
        scope did nothing itself, so it gets an ordinary UNPROCESSED row like any
        other affected scope, never a self-notice (a born-processed row would never
        wake its own judge)."""
        emit(
            fleet=fleet,
            record_store=record_store,
            item="c_1",
            kind="claim_corrected",
            source_scope_id="g_funcA",
            before="the old claim",
            after="the corrected claim",
            by_owner=False,
        )

        holder_events = record_store.list_change_events(scope_id="g_funcA")
        assert len(holder_events) == 1
        assert holder_events[0].self_notice == 0
        assert holder_events[0].processed_at is None  # enqueued, an ordinary refresh
        assert holder_events[0].kind == "claim_corrected"
        assert "the corrected claim" in holder_events[0].after

    def test_claim_corrected_notice_carries_the_claim_id_alongside_the_item(
        self, fleet: FleetConfig, record_store: RecordStore
    ) -> None:
        """ADR 0017 P4 (CEO decision A, follow-up 2): a reader's refresh judge must
        see, under the same change id: which of its OWN published items just went
        stale (`item`), which upstream claim caused it (`claim_id`), and the
        correcting content (`after`) — the new face alone is not enough."""
        (change_id,) = emit(
            fleet=fleet,
            record_store=record_store,
            item="pub_9",
            kind="claim_corrected",
            source_scope_id="g_funcA",
            before="The service listens on port 8443.",
            after="Used port 8443, the service refused; the right port is unknown.",
            claim_id="c_target01",
            by_owner=True,
        )

        for scope_id in ("g_teamX", "g_teamY", "g_funcB"):
            events = record_store.list_change_events(scope_id=scope_id)
            assert len(events) == 1
            event = events[0]
            assert event.change_id == change_id
            notice = record_store.get_contribution(event.contribution_id)
            assert notice is not None
            assert "pub_9" in notice.content
            assert "c_target01" in notice.content
            assert "right port is unknown" in notice.content
