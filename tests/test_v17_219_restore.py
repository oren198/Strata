"""The restore act (companion to #219 C) — undoes a published item wrongly
withdrawn by a correction sweep (verbatim P4, #219 C ``carries``, or either
one's relay cascade), under its original id and bytes, telling exactly the
readers who got the false notice that it was false.

Covers the design note's own test list:
1. Restorable set: a sweep withdrawal is restorable; a deliberate withdraw is not.
2. Owner path: accept restores (same id/bytes, relays back, claim_restored to
   exactly the original readers); decline changes nothing.
3. Operator path: restores without a judgment row, operator provenance.
4. A reader added after the withdrawal gets no claim_restored.
5. Wave: one change id, inherited, hop-bounded (one new id for the restore's
   own notice).
6. The reader refresh renders the restored item (claim_restored is a real,
   well-formed change event).
7. Input identity: no claim_restored event -> byte-identical refresh input
   (and judge_publication's act_kind='restore' changes input ONLY for restore).
8. A relay scope that re-judged since the withdrawal gets evidence only, the
   cascade stops there (contract line 4).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from strata.change_events import CLAIM_CORRECTED, CLAIM_RESTORED
from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.publication import (
    PublishedItem,
    _write_publication,
    operator_restore,
    propose_restore,
    propose_withdraw,
    read_publication,
    restorable_withdrawal,
)
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import PublicationJudgment
from strata.summary_store import ScopeSummary, SummaryStore

from .test_v16_221_relay_sweep import _setup as _setup_relay_correction

_FLEET_YAML_SINGLE = """
strata:
  - id: L0
    name: Root
    ordinal: 0
scopes:
  - id: g_root
    name: Root
    stratum_id: L0
edges: []
"""

_CLAIM = "The service listens on port 8443."
_CORRECTION = "Used port 8443, the service refused; the right port is unknown."


def _proposer(scope_id: str = "g_root") -> ContributorRef:
    return ContributorRef(
        scope_id=scope_id, skill="engineer", session_id="s1", ts="2026-01-01T00:00:00Z"
    )


def _setup_same_scope_correction(tmp_path: Path):
    """One scope, one published item, corrected same-scope (verbatim P4 sweep
    withdraws it, no judge withdraw_published, no #219 C involved). Returns
    (db_path, fleet_yaml, summaries_dir, summary_store, item_id)."""
    from strata import app

    db_path = str(tmp_path / "test.db")
    fleet_yaml = tmp_path / "fleet.yaml"
    fleet_yaml.write_text(textwrap.dedent(_FLEET_YAML_SINGLE), encoding="utf-8")
    summaries_dir = tmp_path / "summaries"
    run_migrations(db_path)
    summary_store = SummaryStore(str(summaries_dir))

    with RecordStore(db_path) as store:
        target = store.append_contribution(
            scope_id="g_root",
            content=_CLAIM,
            proposed_classification="context",
            subject="service-port",
            supersedes=None,
            contributor=_proposer(),
        )
        store.record_judgment(
            contribution_id=target.id, decision="accept_as_context", judged_by="scope-manager"
        )
        act = store.append_publication_act(
            scope_id="g_root",
            act="publish",
            kind="context",
            content=_CLAIM,
            subject="service-port",
            anchors=["subject:service-port"],
            withdraws=None,
            trigger=None,
            proposer=_proposer(),
        )
        store.record_publication_judgment(
            act_id=act.id, decision="accept", judged_by="scope-manager"
        )
        _write_publication(
            "g_root",
            [
                PublishedItem(
                    id=act.id,
                    kind="context",
                    content=_CLAIM,
                    subject="service-port",
                    anchors=["subject:service-port"],
                    published_at="2026-01-01T00:00:00Z",
                )
            ],
            summaries_dir=str(summaries_dir),
        )
        summary_store.write(
            "g_root",
            ScopeSummary(
                scope_id="g_root", directives=[], context=_CLAIM, updated_at="2026-01-01T00:00:00Z"
            ),
        )

        from strata.scope_manager import ScopeManagerJudgment

        outcome_judgment = ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="Confirmed by observation.",
            new_summary=ScopeSummary(
                scope_id="g_root",
                directives=[],
                context="The service's port is under investigation.",
                updated_at="2026-01-01T00:01:00Z",
            ),
            outcome_disposition="failed_corrected",
        )
        mock_manager = MagicMock()
        mock_manager.judge.return_value = outcome_judgment

        outcome = store.append_contribution(
            scope_id="g_root",
            content=_CORRECTION,
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_proposer(),
            acted_on=target.id,
        )
        fleet_ = FleetConfig.load(fleet_yaml)
        summary_store_ = SummaryStore(str(summaries_dir))
        app._judge_and_record(
            contribution=outcome,
            scope=fleet_.get_scope("g_root"),
            stratum=fleet_.strata[0],
            fleet=fleet_,
            record_store=store,
            summary_store=summary_store_,
            scope_manager=mock_manager,
            summary_max_words=500,
        )

    assert read_publication("g_root", summaries_dir=str(summaries_dir)) == []
    return db_path, fleet_yaml, summaries_dir, summary_store, act.id


class _AcceptingPublicationManager:
    def judge_publication(self, **kwargs: Any) -> PublicationJudgment:  # noqa: ARG002
        return PublicationJudgment(decision="accept", reasoning="Still believed; not carried.")


class _DecliningPublicationManager:
    def judge_publication(self, **kwargs: Any) -> PublicationJudgment:  # noqa: ARG002
        return PublicationJudgment(decision="decline", reasoning="Still carries the claim.")


# ---------------------------------------------------------------------------
# 1. Restorable set.
# ---------------------------------------------------------------------------


def test_sweep_withdrawal_is_restorable(tmp_path: Path) -> None:
    db_path, fleet_yaml, summaries_dir, _summary_store, item_id = _setup_same_scope_correction(
        tmp_path
    )
    with RecordStore(db_path) as store:
        restorable = restorable_withdrawal("g_root", item_id, record_store=store)
    assert restorable is not None
    claim_id, change_ids = restorable
    assert claim_id
    assert change_ids


def test_deliberate_withdrawal_is_not_restorable(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.db")
    fleet_yaml = tmp_path / "fleet.yaml"
    fleet_yaml.write_text(textwrap.dedent(_FLEET_YAML_SINGLE), encoding="utf-8")
    summaries_dir = tmp_path / "summaries"
    run_migrations(db_path)
    fleet = FleetConfig.load(fleet_yaml)
    summary_store = SummaryStore(str(summaries_dir))

    with RecordStore(db_path) as store:
        act = store.append_publication_act(
            scope_id="g_root",
            act="publish",
            kind="context",
            content=_CLAIM,
            subject="service-port",
            anchors=["subject:service-port"],
            withdraws=None,
            trigger=None,
            proposer=_proposer(),
        )
        store.record_publication_judgment(
            act_id=act.id, decision="accept", judged_by="scope-manager"
        )
        _write_publication(
            "g_root",
            [
                PublishedItem(
                    id=act.id,
                    kind="context",
                    content=_CLAIM,
                    subject="service-port",
                    anchors=["subject:service-port"],
                    published_at="2026-01-01T00:00:00Z",
                )
            ],
            summaries_dir=str(summaries_dir),
        )
        outcome = propose_withdraw(
            "g_root",
            act.id,
            _proposer(),
            fleet=fleet,
            record_store=store,
            summary_store=summary_store,
            scope_manager=_AcceptingPublicationManager(),
        )
        assert outcome.decision == "accept"
        restorable = restorable_withdrawal("g_root", act.id, record_store=store)
    assert restorable is None


# ---------------------------------------------------------------------------
# 2. Owner path.
# ---------------------------------------------------------------------------


def test_owner_path_accept_restores_same_id_and_bytes(tmp_path: Path) -> None:
    db_path, fleet_yaml, summaries_dir, summary_store, item_id = _setup_same_scope_correction(
        tmp_path
    )
    fleet = FleetConfig.load(fleet_yaml)
    with RecordStore(db_path) as store:
        outcome = propose_restore(
            "g_root",
            item_id,
            "false withdrawal",
            _proposer(),
            fleet=fleet,
            record_store=store,
            summary_store=summary_store,
            scope_manager=_AcceptingPublicationManager(),
        )
    assert outcome.decision == "accept"
    assert outcome.artifact_updated

    restored = read_publication("g_root", summaries_dir=str(summaries_dir))
    assert [i.id for i in restored] == [item_id]
    assert restored[0].content == _CLAIM

    with RecordStore(db_path) as store:
        withdraw_act = next(
            a
            for a in store.list_publication_acts(scope_id="g_root")
            if a.act == "withdraw" and a.withdraws == item_id
        )
        assert withdraw_act.restored_by == outcome.act_id


def test_owner_path_decline_changes_nothing(tmp_path: Path) -> None:
    db_path, fleet_yaml, summaries_dir, summary_store, item_id = _setup_same_scope_correction(
        tmp_path
    )
    fleet = FleetConfig.load(fleet_yaml)
    with RecordStore(db_path) as store:
        outcome = propose_restore(
            "g_root",
            item_id,
            "false withdrawal",
            _proposer(),
            fleet=fleet,
            record_store=store,
            summary_store=summary_store,
            scope_manager=_DecliningPublicationManager(),
        )
    assert outcome.decision == "decline"
    assert not outcome.artifact_updated
    assert read_publication("g_root", summaries_dir=str(summaries_dir)) == []

    with RecordStore(db_path) as store:
        withdraw_act = next(
            a
            for a in store.list_publication_acts(scope_id="g_root")
            if a.act == "withdraw" and a.withdraws == item_id
        )
        assert withdraw_act.restored_by is None


def test_owner_path_rejects_a_non_restorable_item(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.db")
    fleet_yaml = tmp_path / "fleet.yaml"
    fleet_yaml.write_text(textwrap.dedent(_FLEET_YAML_SINGLE), encoding="utf-8")
    summaries_dir = tmp_path / "summaries"
    run_migrations(db_path)
    fleet = FleetConfig.load(fleet_yaml)
    summary_store = SummaryStore(str(summaries_dir))

    with RecordStore(db_path) as store:
        act = store.append_publication_act(
            scope_id="g_root",
            act="publish",
            kind="context",
            content=_CLAIM,
            subject="service-port",
            anchors=["subject:service-port"],
            withdraws=None,
            trigger=None,
            proposer=_proposer(),
        )
        store.record_publication_judgment(
            act_id=act.id, decision="accept", judged_by="scope-manager"
        )
        _write_publication(
            "g_root",
            [
                PublishedItem(
                    id=act.id,
                    kind="context",
                    content=_CLAIM,
                    subject="service-port",
                    anchors=["subject:service-port"],
                    published_at="2026-01-01T00:00:00Z",
                )
            ],
            summaries_dir=str(summaries_dir),
        )
        propose_withdraw(
            "g_root",
            act.id,
            _proposer(),
            fleet=fleet,
            record_store=store,
            summary_store=summary_store,
            scope_manager=_AcceptingPublicationManager(),
        )
        with pytest.raises(ValueError, match="not withdrawn by a correction sweep"):
            propose_restore(
                "g_root",
                act.id,
                None,
                _proposer(),
                fleet=fleet,
                record_store=store,
                summary_store=summary_store,
                scope_manager=_AcceptingPublicationManager(),
            )


# ---------------------------------------------------------------------------
# 3. Operator path.
# ---------------------------------------------------------------------------


def test_operator_path_restores_without_a_judgment_row(tmp_path: Path) -> None:
    db_path, fleet_yaml, summaries_dir, _summary_store, item_id = _setup_same_scope_correction(
        tmp_path
    )
    fleet = FleetConfig.load(fleet_yaml)
    with RecordStore(db_path) as store:
        outcome = operator_restore(
            "g_root",
            item_id,
            "operator review",
            fleet=fleet,
            record_store=store,
            summaries_dir=str(summaries_dir),
        )
        assert outcome.decision == "accept"
        row = store._conn.execute(  # noqa: SLF001 — asserting no row exists, not a public read
            "SELECT 1 FROM publication_judgments WHERE act_id = ?", (outcome.act_id,)
        ).fetchone()
        assert row is None
        restore_act = store.get_publication_act(outcome.act_id)
        assert restore_act is not None
        assert restore_act.proposer.scope_id == "operator"

    restored = read_publication("g_root", summaries_dir=str(summaries_dir))
    assert [i.id for i in restored] == [item_id]


# ---------------------------------------------------------------------------
# 4-8. Relay cascade, exact-readers, wave id, input identity.
# ---------------------------------------------------------------------------


def test_relay_restored_mechanically_and_claim_restored_reaches_exact_readers(
    tmp_path: Path,
) -> None:
    db_path, fleet_yaml, summaries_dir, change_id, relay_item_id = _setup_relay_correction(
        tmp_path, use_relay=True
    )
    fleet = FleetConfig.load(fleet_yaml)

    with RecordStore(db_path) as store:
        root_withdraw = next(
            a for a in store.list_publication_acts(scope_id="g_root") if a.act == "withdraw"
        )
        root_item_id = root_withdraw.withdraws
        assert root_item_id is not None

        outcome = operator_restore(
            "g_root",
            root_item_id,
            None,
            fleet=fleet,
            record_store=store,
            summaries_dir=str(summaries_dir),
        )
        assert outcome.decision == "accept"

    # The root item and its tracked relay both come back under their
    # original ids and bytes.
    assert [i.id for i in read_publication("g_root", summaries_dir=str(summaries_dir))] == [
        root_item_id
    ]
    child_items = read_publication("g_child", summaries_dir=str(summaries_dir))
    assert [i.id for i in child_items] == [relay_item_id]
    assert child_items[0].content == _CLAIM

    with RecordStore(db_path) as store:
        # g_child's own withdraw act is stamped restored.
        child_withdraw = next(
            a
            for a in store.list_publication_acts(scope_id="g_child")
            if a.act == "withdraw" and a.withdraws == relay_item_id
        )
        assert child_withdraw.restored_by is not None

        # claim_restored reached exactly g_root and g_child (and
        # g_grandchild, #221's own depth-2 reader) — the scopes that got the
        # original claim_corrected notice for one of these two item ids.
        for scope_id in ("g_root", "g_child", "g_grandchild"):
            restored_events = [
                e for e in store.list_change_events(scope_id=scope_id) if e.kind == CLAIM_RESTORED
            ]
            assert restored_events, f"{scope_id} got no claim_restored"


def test_relay_stops_when_relaying_scope_has_rejudged_since(tmp_path: Path) -> None:
    db_path, fleet_yaml, summaries_dir, change_id, relay_item_id = _setup_relay_correction(
        tmp_path, use_relay=True
    )
    fleet = FleetConfig.load(fleet_yaml)

    with RecordStore(db_path) as store:
        root_withdraw = next(
            a for a in store.list_publication_acts(scope_id="g_root") if a.act == "withdraw"
        )
        root_item_id = root_withdraw.withdraws
        assert root_item_id is not None

        # Simulate g_child's own judge having drained the ordinary
        # claim_corrected notice about g_root's item (contract line 4's
        # "standing changed") — mark every one of g_child's own non-self
        # claim_corrected rows for root_item_id processed.
        for event in store.list_change_events(scope_id="g_child"):
            if (
                not event.self_notice
                and event.kind == CLAIM_CORRECTED
                and event.item_id == root_item_id
            ):
                store.mark_change_event_processed(event.id)

        outcome = operator_restore(
            "g_root",
            root_item_id,
            None,
            fleet=fleet,
            record_store=store,
            summaries_dir=str(summaries_dir),
        )
        assert outcome.decision == "accept"

    assert [i.id for i in read_publication("g_root", summaries_dir=str(summaries_dir))] == [
        root_item_id
    ]
    # g_child's own copy is NOT mechanically restored — its standing changed.
    assert read_publication("g_child", summaries_dir=str(summaries_dir)) == []
    # Nor is g_grandchild's — nothing to cascade from.
    assert read_publication("g_grandchild", summaries_dir=str(summaries_dir)) == []

    with RecordStore(db_path) as store:
        child_withdraw = next(
            a
            for a in store.list_publication_acts(scope_id="g_child")
            if a.act == "withdraw" and a.withdraws == relay_item_id
        )
        assert child_withdraw.restored_by is None
        # g_child still gets the claim_restored notice as evidence.
        assert any(e.kind == CLAIM_RESTORED for e in store.list_change_events(scope_id="g_child"))


def test_a_reader_added_after_the_withdrawal_gets_no_claim_restored(tmp_path: Path) -> None:
    db_path, fleet_yaml, summaries_dir, summary_store, item_id = _setup_same_scope_correction(
        tmp_path
    )
    fleet = FleetConfig.load(fleet_yaml)

    with RecordStore(db_path) as store:
        # A late reader: a change event for the SAME item, but under a
        # DIFFERENT change id (never part of the original correction's
        # wave) — simulating a scope that only came to read the item after
        # the fact and so never received the original false notice.
        late = store.append_contribution(
            scope_id="g_late",
            content="noting the port claim",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_proposer("g_late"),
        )
        store.append_change_event(
            change_id="chg_unrelated",
            contribution_id=late.id,
            scope_id="g_late",
            item_id=item_id,
            kind=CLAIM_CORRECTED,
            source_scope_id="g_root",
        )

        outcome = operator_restore(
            "g_root",
            item_id,
            None,
            fleet=fleet,
            record_store=store,
            summaries_dir=str(summaries_dir),
        )
        assert outcome.decision == "accept"

        assert all(e.kind != CLAIM_RESTORED for e in store.list_change_events(scope_id="g_late"))


def test_restore_mints_one_new_change_id_for_its_own_notice(tmp_path: Path) -> None:
    db_path, fleet_yaml, summaries_dir, summary_store, item_id = _setup_same_scope_correction(
        tmp_path
    )
    fleet = FleetConfig.load(fleet_yaml)
    with RecordStore(db_path) as store:
        original = {
            e.change_id for e in store.list_change_events(scope_id="g_root") if e.self_notice
        }
        operator_restore(
            "g_root",
            item_id,
            None,
            fleet=fleet,
            record_store=store,
            summaries_dir=str(summaries_dir),
        )
        restored_events = [
            e for e in store.list_change_events(scope_id="g_root") if e.kind == CLAIM_RESTORED
        ]
        assert len(restored_events) == 1
        assert restored_events[0].change_id not in original


def test_already_restored_cannot_be_restored_again(tmp_path: Path) -> None:
    db_path, fleet_yaml, summaries_dir, summary_store, item_id = _setup_same_scope_correction(
        tmp_path
    )
    fleet = FleetConfig.load(fleet_yaml)
    with RecordStore(db_path) as store:
        operator_restore(
            "g_root",
            item_id,
            None,
            fleet=fleet,
            record_store=store,
            summaries_dir=str(summaries_dir),
        )
        with pytest.raises(ValueError, match="already been restored"):
            operator_restore(
                "g_root",
                item_id,
                None,
                fleet=fleet,
                record_store=store,
                summaries_dir=str(summaries_dir),
            )


# ---------------------------------------------------------------------------
# 7. Input identity.
# ---------------------------------------------------------------------------


def test_judge_publication_restore_kind_does_not_change_publish_or_withdraw_input() -> None:
    """judge_publication(act_kind='restore') is new, additive code — a
    publish or withdraw call's rendered proposal block is unaffected by it
    (restore-only fields never leak into the other two branches)."""
    from strata.scope_manager import ScopeManager

    mock_client = MagicMock()
    manager = ScopeManager(client=mock_client, model="m")
    block = MagicMock()
    block.type = "tool_use"
    block.input = {"decision": "accept", "reasoning": "ok"}
    response = MagicMock()
    response.content = [block]
    mock_client.messages.create.return_value = response

    scope = MagicMock(id="g_root")
    scope.name = "Root"
    manager.judge_publication(
        scope=scope,
        act_kind="withdraw",
        current_summary=None,
        current_publication=[],
        withdraw_item=PublishedItem(
            id="pub_x",
            kind="context",
            content="c",
            subject=None,
            anchors=["subject:s"],
            published_at="2026-01-01T00:00:00Z",
        ),
    )
    user_message = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "restore" not in user_message.lower()
    assert "refuted claim" not in user_message.lower()
