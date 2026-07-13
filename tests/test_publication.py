"""Tests for src/strata/publication.py — the publication channel (ADR 0007, issue #90).

Covers:

1. The publication artifact: render/parse round-trip (byte-identical),
   atomic write, honestly-empty read for a scope that has published nothing.
2. Judged publish/withdraw acts (propose_publish / propose_withdraw): accept
   records the act + judgment + rewrites the artifact; decline records the
   act + judgment but leaves the artifact untouched; structural anchor
   errors (zero anchors; an explicit ``directive:`` anchor naming an id not
   in the current summary) raise BEFORE any act row is appended.
3. Mechanical propagation (propagate_directive_removals): a directive-only-
   anchored item is withdrawn (with ``trigger`` set, no judgment row) when
   its directive vanishes; a subject-anchored item survives.
4. Judged propagation (apply_judged_withdrawals): withdrawal acts carry a
   judgment row using the SAME judged_by/reasoning as the triggering
   contribution judgment; unknown ids are ignored, not errors.
5. Bootstrap (bootstrap_publication): accepted candidates become ordinary
   accepted publish acts; a decline (or an empty item list) records nothing.

Vocabulary follows CONTEXT.md verbatim: publication, withdrawal, scope,
scope summary, directive, context, record, provenance, supersession,
retirement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.publication import (
    BootstrapOutcome,
    PublishedItem,
    _parse_publication,
    _render_publication,
    _write_publication,
    apply_judged_withdrawals,
    bootstrap_publication,
    list_scopes_with_publications,
    propagate_directive_removals,
    propose_publish,
    propose_withdraw,
    read_publication,
    read_publication_text,
)
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import (
    BootstrapJudgment,
    BootstrapPublishedItemInput,
    PublicationJudgment,
)
from strata.summary_store import Directive, ScopeSummary, SummaryStore

# ---------------------------------------------------------------------------
# Fixture fleet — g_exec (L0) <- g_func (L1) <- g_team (L2)
# ---------------------------------------------------------------------------


def _make_fleet(tmp_path: Path) -> FleetConfig:
    import yaml

    fleet = {
        "strata": [
            {"id": "L0", "name": "executive", "ordinal": 0},
            {"id": "L1", "name": "function", "ordinal": 1},
            {"id": "L2", "name": "team", "ordinal": 2},
        ],
        "scopes": [
            {"id": "g_exec", "name": "Executive", "stratum_id": "L0"},
            {"id": "g_func", "name": "Function", "stratum_id": "L1"},
            {"id": "g_team", "name": "Team", "stratum_id": "L2"},
        ],
        "edges": [
            {"from": "g_func", "to": "g_exec"},
            {"from": "g_team", "to": "g_func"},
        ],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return FleetConfig.load(fleet_path)


@pytest.fixture()
def record_store(tmp_path: Path):
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    store = RecordStore(db_path)
    yield store
    store.close()


@pytest.fixture()
def summaries_dir(tmp_path: Path) -> str:
    return str(tmp_path / "summaries")


@pytest.fixture()
def summary_store(summaries_dir: str) -> SummaryStore:
    return SummaryStore(summaries_dir)


@pytest.fixture()
def fleet(tmp_path: Path) -> FleetConfig:
    return _make_fleet(tmp_path)


def _proposer(scope_id: str = "g_team") -> ContributorRef:
    return ContributorRef(
        scope_id=scope_id,
        skill="strata-developer",
        session_id="sess_test",
        ts="2026-07-12T00:00:00+00:00",
    )


def _seed_summary_with_directive(
    summary_store: SummaryStore, scope_id: str, directive_id: str = "c_dir1"
) -> ScopeSummary:
    summary = ScopeSummary(
        scope_id=scope_id,
        directives=[
            Directive(
                id=directive_id,
                content="Use protobuf for all RPC.",
                subject="rpc",
                source_scope_id=scope_id,
                source_skill="strata-developer",
                created_at="2026-07-12T00:00:00+00:00",
            )
        ],
        context="Deploys happen at 3pm UTC.",
        updated_at="2026-07-12T00:00:00+00:00",
    )
    return summary_store.write(scope_id, summary)


def _seed_published_item(
    record_store: RecordStore,
    summaries_dir: str,
    scope_id: str,
    *,
    kind: str = "directive",
    content: str = "content",
    subject: str | None = None,
    anchors: list[str] | None = None,
) -> PublishedItem:
    """Append a real ``publish`` act (so a later ``withdraws`` FK reference resolves) and write it.

    Mirrors what :func:`strata.publication.propose_publish` does, minus the
    judging — used by propagation tests that need a published item whose id
    genuinely exists in ``publication_acts`` (the ``withdraws`` column is a
    foreign key).
    """
    act = record_store.append_publication_act(
        scope_id=scope_id,
        act="publish",
        kind=kind,
        content=content,
        subject=subject,
        anchors=anchors or [],
        withdraws=None,
        trigger=None,
        proposer=_proposer(scope_id),
    )
    record_store.record_publication_judgment(
        act_id=act.id, decision="accept", judged_by="scope-manager", reasoning="seeded for test"
    )
    item = PublishedItem(
        id=act.id,
        kind=kind,
        content=content,
        subject=subject,
        anchors=anchors or [],
        published_at=act.created_at,
    )
    existing = read_publication(scope_id, summaries_dir=summaries_dir)
    _write_publication(scope_id, [*existing, item], summaries_dir=summaries_dir)
    return item


class _FakeScopeManager:
    """A scope-manager fake for judge_publication / judge_bootstrap_publication."""

    def __init__(
        self,
        publication_judgment: PublicationJudgment | None = None,
        bootstrap_judgment: BootstrapJudgment | None = None,
    ) -> None:
        self._publication_judgment = publication_judgment
        self._bootstrap_judgment = bootstrap_judgment
        self.publication_calls: list[dict] = []
        self.bootstrap_calls: list[dict] = []

    def judge_publication(self, **kwargs) -> PublicationJudgment:
        self.publication_calls.append(kwargs)
        return self._publication_judgment

    def judge_bootstrap_publication(self, **kwargs) -> BootstrapJudgment:
        self.bootstrap_calls.append(kwargs)
        return self._bootstrap_judgment


# ---------------------------------------------------------------------------
# 1. The publication artifact — render/parse round-trip, atomic write,
#    honestly-empty read.
# ---------------------------------------------------------------------------


def test_artifact_round_trip_byte_identical_multiline_content(summaries_dir: str) -> None:
    """Multi-line, markdown-ish content survives write -> read -> re-render byte-for-byte."""
    items = [
        PublishedItem(
            id="pub_aaa111",
            kind="directive",
            content="Use protobuf for all RPC.\n\n- No exceptions.\n- See ADR-12.",
            subject="rpc-protocol",
            anchors=["directive:c_dir1"],
            published_at="2026-07-12T10:00:00+00:00",
        ),
        PublishedItem(
            id="pub_bbb222",
            kind="context",
            content="Deploys happen at 3pm UTC.\n> nested quote\n## fake heading",
            subject=None,
            anchors=["subject:deploy-notes"],
            published_at="2026-07-12T10:05:00+00:00",
        ),
    ]
    _write_publication("g_team", items, summaries_dir=summaries_dir)

    read_back = read_publication("g_team", summaries_dir=summaries_dir)
    assert read_back == items

    # Re-rendering the parsed items reproduces the exact same file content.
    original_text = Path(summaries_dir, "g_team.pub.md").read_text(encoding="utf-8")
    assert _render_publication("g_team", read_back) == original_text
    assert _parse_publication(_render_publication("g_team", items)) == items


def test_read_publication_empty_for_scope_with_no_artifact(summaries_dir: str) -> None:
    """A scope that has published nothing yet returns an empty list — the honestly empty face."""
    assert read_publication("g_never_published", summaries_dir=summaries_dir) == []


def test_read_publication_text_none_for_missing_artifact(summaries_dir: str) -> None:
    assert read_publication_text("g_never_published", summaries_dir=summaries_dir) is None


def test_list_scopes_with_publications(summaries_dir: str) -> None:
    _write_publication("g_team", [], summaries_dir=summaries_dir)
    _write_publication("g_func", [], summaries_dir=summaries_dir)
    assert list_scopes_with_publications(summaries_dir) == ["g_func", "g_team"]


def test_publication_artifact_write_is_atomic_no_tmp_left_behind(summaries_dir: str) -> None:
    _write_publication("g_team", [], summaries_dir=summaries_dir)
    tmp_path = Path(summaries_dir, "g_team.pub.md.tmp")
    assert not tmp_path.exists()
    assert Path(summaries_dir, "g_team.pub.md").exists()


# ---------------------------------------------------------------------------
# 2. propose_publish / propose_withdraw
# ---------------------------------------------------------------------------


def test_propose_publish_accept_records_act_and_judgment_and_updates_artifact(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    _seed_summary_with_directive(summary_store, "g_team")
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Fit for export.")
    )

    outcome = propose_publish(
        "g_team",
        "Use protobuf for all RPC.",
        "directive",
        "rpc-protocol",
        ["c_dir1"],
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.decision == "accept"
    assert outcome.artifact_updated is True

    acts = record_store.list_publication_acts(scope_id="g_team")
    assert len(acts) == 1
    act = acts[0]
    assert act.act == "publish"
    assert act.anchors == ["directive:c_dir1"]
    assert act.trigger is None

    judgments = record_store.list_publication_judgments(scope_id="g_team")
    assert len(judgments) == 1
    assert judgments[0].decision == "accept"
    assert judgments[0].judged_by == "scope-manager"

    items = read_publication("g_team", summaries_dir=summaries_dir)
    assert len(items) == 1
    assert items[0].id == act.id
    assert items[0].content == "Use protobuf for all RPC."
    assert items[0].anchors == ["directive:c_dir1"]


def test_propose_publish_decline_records_rows_only_artifact_untouched(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    _seed_summary_with_directive(summary_store, "g_team")
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="decline", reasoning="Reads as scratch.")
    )

    outcome = propose_publish(
        "g_team",
        "half-formed idea",
        "context",
        None,
        ["c_dir1"],
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.decision == "decline"
    assert outcome.artifact_updated is False

    acts = record_store.list_publication_acts(scope_id="g_team")
    assert len(acts) == 1
    judgments = record_store.list_publication_judgments(scope_id="g_team")
    assert len(judgments) == 1
    assert judgments[0].decision == "decline"

    # Artifact was never even created.
    assert read_publication("g_team", summaries_dir=summaries_dir) == []
    assert read_publication_text("g_team", summaries_dir=summaries_dir) is None


def test_propose_publish_zero_anchors_raises_and_appends_no_act_row(
    fleet, record_store, summary_store
) -> None:
    _seed_summary_with_directive(summary_store, "g_team")
    manager = _FakeScopeManager()

    with pytest.raises(ValueError, match="at least one anchor"):
        propose_publish(
            "g_team",
            "content",
            "context",
            None,
            [],
            _proposer(),
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=manager,
        )

    assert record_store.list_publication_acts(scope_id="g_team") == []
    assert manager.publication_calls == []


def test_propose_publish_unknown_directive_anchor_raises_and_appends_no_act_row(
    fleet, record_store, summary_store
) -> None:
    _seed_summary_with_directive(summary_store, "g_team", directive_id="c_dir1")
    manager = _FakeScopeManager()

    with pytest.raises(ValueError, match="not in this"):
        propose_publish(
            "g_team",
            "content",
            "context",
            None,
            ["directive:c_does_not_exist"],
            _proposer(),
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=manager,
        )

    assert record_store.list_publication_acts(scope_id="g_team") == []
    assert manager.publication_calls == []


def test_propose_publish_unknown_scope_raises_valueerror(record_store, summary_store) -> None:
    empty_fleet = FleetConfig(strata=[], scopes=[], edges=[])
    manager = _FakeScopeManager()
    with pytest.raises(ValueError, match="Scope not found"):
        propose_publish(
            "g_nonexistent",
            "content",
            "context",
            None,
            ["subject:x"],
            _proposer(),
            fleet=empty_fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=manager,
        )


def test_propose_withdraw_accept_removes_item_and_records_rows(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    _seed_summary_with_directive(summary_store, "g_team")
    publish_manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Fit for export.")
    )
    published = propose_publish(
        "g_team",
        "Use protobuf for all RPC.",
        "directive",
        "rpc-protocol",
        ["c_dir1"],
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=publish_manager,
    )

    withdraw_manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="No longer relevant.")
    )
    outcome = propose_withdraw(
        "g_team",
        published.act_id,
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=withdraw_manager,
    )

    assert outcome.decision == "accept"
    assert outcome.artifact_updated is True
    assert read_publication("g_team", summaries_dir=summaries_dir) == []

    acts = record_store.list_publication_acts(scope_id="g_team")
    assert len(acts) == 2
    withdraw_act = next(a for a in acts if a.act == "withdraw")
    assert withdraw_act.withdraws == published.act_id
    assert withdraw_act.trigger is None

    judgments = record_store.list_publication_judgments(scope_id="g_team")
    assert len(judgments) == 2


def test_propose_withdraw_unknown_item_raises_keyerror_no_act_row(
    fleet, record_store, summary_store
) -> None:
    manager = _FakeScopeManager()
    with pytest.raises(KeyError):
        propose_withdraw(
            "g_team",
            "pub_does_not_exist",
            _proposer(),
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=manager,
        )
    assert record_store.list_publication_acts(scope_id="g_team") == []
    assert manager.publication_calls == []


def test_propose_publish_judge_failure_leaves_act_row_unjudged(
    fleet, record_store, summary_store
) -> None:
    """A judge_publication failure propagates AS-IS, after the act row already exists."""
    _seed_summary_with_directive(summary_store, "g_team")

    class _RaisingManager:
        def judge_publication(self, **_kwargs):
            raise RuntimeError("scope-manager unavailable")

    with pytest.raises(RuntimeError, match="scope-manager unavailable"):
        propose_publish(
            "g_team",
            "content",
            "context",
            None,
            ["c_dir1"],
            _proposer(),
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=_RaisingManager(),
        )

    # The act row exists — the record never lies — but carries no judgment.
    acts = record_store.list_publication_acts(scope_id="g_team")
    assert len(acts) == 1
    assert record_store.list_publication_judgments(scope_id="g_team") == []


# ---------------------------------------------------------------------------
# 3. Mechanical propagation (propagate_directive_removals)
# ---------------------------------------------------------------------------


def test_mechanical_propagation_withdraws_directive_only_anchored_item(
    record_store, summaries_dir
) -> None:
    item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_team",
        content="Use protobuf.",
        anchors=["directive:c_dir1"],
    )

    withdrawn = propagate_directive_removals(
        "g_team",
        {"c_dir1"},
        "c_trigger1",
        surviving_directive_ids=set(),
        record_store=record_store,
        summaries_dir=summaries_dir,
    )

    assert [i.id for i in withdrawn] == [item.id]
    assert read_publication("g_team", summaries_dir=summaries_dir) == []

    acts = record_store.list_publication_acts(scope_id="g_team")
    withdraw_act = next(a for a in acts if a.act == "withdraw")
    assert withdraw_act.withdraws == item.id
    assert withdraw_act.trigger == "c_trigger1"
    # Mechanical propagation gets NO judgment row for the withdraw act (the
    # seeded publish act above has its own judgment row, from the fixture).
    withdraw_judgment = record_store.get_publication_judgment(withdraw_act.id)
    assert withdraw_judgment is None


def test_mechanical_propagation_spares_item_with_surviving_subject_anchor(
    record_store, summaries_dir
) -> None:
    item = PublishedItem(
        id="pub_x2",
        kind="directive",
        content="Use protobuf, per our conventions doc.",
        subject="conventions",
        anchors=["directive:c_dir1", "subject:conventions"],
        published_at="2026-07-12T00:00:00+00:00",
    )
    _write_publication("g_team", [item], summaries_dir=summaries_dir)

    withdrawn = propagate_directive_removals(
        "g_team",
        {"c_dir1"},
        "c_trigger1",
        surviving_directive_ids=set(),
        record_store=record_store,
        summaries_dir=summaries_dir,
    )

    assert withdrawn == []
    assert read_publication("g_team", summaries_dir=summaries_dir) == [item]
    assert record_store.list_publication_acts(scope_id="g_team") == []


def test_mechanical_propagation_spares_item_anchored_to_a_different_surviving_directive(
    record_store, summaries_dir
) -> None:
    item = PublishedItem(
        id="pub_x3",
        kind="directive",
        content="Two-anchor item.",
        subject=None,
        anchors=["directive:c_dir1", "directive:c_dir2"],
        published_at="2026-07-12T00:00:00+00:00",
    )
    _write_publication("g_team", [item], summaries_dir=summaries_dir)

    withdrawn = propagate_directive_removals(
        "g_team",
        {"c_dir1"},  # c_dir2 survives
        "c_trigger1",
        surviving_directive_ids={"c_dir2"},
        record_store=record_store,
        summaries_dir=summaries_dir,
    )

    assert withdrawn == []
    assert read_publication("g_team", summaries_dir=summaries_dir) == [item]


def test_mechanical_propagation_fires_when_the_last_anchor_vanishes_in_a_later_event(
    record_store, summaries_dir
) -> None:
    # Review fix (PR #97): anchor vanishing is a property of the summary's
    # CURRENT state, not of one removal batch. A two-anchor item loses
    # c_dir1 in one event (it survives — c_dir2 still stands), then loses
    # c_dir2 in a LATER event: that second event removes only c_dir2, but
    # the item's anchors have now ALL vanished and it must be withdrawn.
    item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_team",
        content="Two-anchor item, anchors vanish across separate events.",
        anchors=["directive:c_dir1", "directive:c_dir2"],
    )

    first = propagate_directive_removals(
        "g_team",
        {"c_dir1"},
        "c_trigger1",
        surviving_directive_ids={"c_dir2"},
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert first == []

    second = propagate_directive_removals(
        "g_team",
        {"c_dir2"},
        "c_trigger2",
        surviving_directive_ids=set(),
        record_store=record_store,
        summaries_dir=summaries_dir,
    )

    assert [i.id for i in second] == [item.id]
    assert read_publication("g_team", summaries_dir=summaries_dir) == []
    withdraw_act = next(
        a for a in record_store.list_publication_acts(scope_id="g_team") if a.act == "withdraw"
    )
    assert withdraw_act.trigger == "c_trigger2"


def test_mechanical_propagation_noop_for_empty_publication(record_store, summaries_dir) -> None:
    assert (
        propagate_directive_removals(
            "g_never_published",
            {"c_dir1"},
            "c_trigger1",
            surviving_directive_ids=set(),
            record_store=record_store,
            summaries_dir=summaries_dir,
        )
        == []
    )


# ---------------------------------------------------------------------------
# 4. Judged propagation (apply_judged_withdrawals)
# ---------------------------------------------------------------------------


def test_judged_propagation_withdraws_named_item_with_judgment_row(
    record_store, summaries_dir
) -> None:
    item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_team",
        kind="context",
        content="Stale belief.",
        subject="status",
        anchors=["subject:status"],
    )

    withdrawn = apply_judged_withdrawals(
        "g_team",
        [item.id],
        judged_by="scope-manager",
        reasoning="Rewrite dropped this belief.",
        record_store=record_store,
        summaries_dir=summaries_dir,
    )

    assert [i.id for i in withdrawn] == [item.id]
    assert read_publication("g_team", summaries_dir=summaries_dir) == []

    acts = record_store.list_publication_acts(scope_id="g_team")
    withdraw_act = next(a for a in acts if a.act == "withdraw")
    assert withdraw_act.trigger is None

    withdraw_judgment = record_store.get_publication_judgment(withdraw_act.id)
    assert withdraw_judgment is not None
    assert withdraw_judgment.decision == "accept"
    assert withdraw_judgment.judged_by == "scope-manager"
    assert withdraw_judgment.reasoning == "Rewrite dropped this belief."


def test_judged_propagation_ignores_unknown_item_id(record_store, summaries_dir) -> None:
    _write_publication("g_team", [], summaries_dir=summaries_dir)

    withdrawn = apply_judged_withdrawals(
        "g_team",
        ["pub_does_not_exist"],
        judged_by="scope-manager",
        reasoning="whatever",
        record_store=record_store,
        summaries_dir=summaries_dir,
    )

    assert withdrawn == []
    assert record_store.list_publication_acts(scope_id="g_team") == []


# ---------------------------------------------------------------------------
# 5. Bootstrap (bootstrap_publication)
# ---------------------------------------------------------------------------


def test_bootstrap_accept_records_items_as_ordinary_accepted_publish_acts(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    _seed_summary_with_directive(summary_store, "g_team")
    manager = _FakeScopeManager(
        bootstrap_judgment=BootstrapJudgment(
            decision="accept",
            reasoning="Two items are fit for export.",
            items=[
                BootstrapPublishedItemInput(
                    content="Use protobuf for all RPC.",
                    kind="directive",
                    subject="rpc",
                    anchors=["c_dir1"],
                ),
                BootstrapPublishedItemInput(
                    content="Deploys happen at 3pm UTC.",
                    kind="context",
                    subject=None,
                    anchors=["deploy-notes"],
                ),
            ],
        )
    )

    outcome: BootstrapOutcome = bootstrap_publication(
        "g_team",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.decision == "accept"
    assert len(outcome.items) == 2

    acts = record_store.list_publication_acts(scope_id="g_team")
    assert len(acts) == 2
    assert all(a.act == "publish" for a in acts)
    judgments = record_store.list_publication_judgments(scope_id="g_team")
    assert len(judgments) == 2
    assert all(j.decision == "accept" and j.judged_by == "scope-manager" for j in judgments)

    items = read_publication("g_team", summaries_dir=summaries_dir)
    assert len(items) == 2
    anchor_sets = {tuple(i.anchors) for i in items}
    assert ("directive:c_dir1",) in anchor_sets
    assert ("subject:deploy-notes",) in anchor_sets


def test_bootstrap_decline_records_nothing(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    manager = _FakeScopeManager(
        bootstrap_judgment=BootstrapJudgment(
            decision="decline", reasoning="Nothing fit to publish yet.", items=[]
        )
    )

    outcome = bootstrap_publication(
        "g_team",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.decision == "decline"
    assert outcome.items == []
    assert record_store.list_publication_acts(scope_id="g_team") == []
    assert read_publication("g_team", summaries_dir=summaries_dir) == []


def test_bootstrap_drops_candidate_with_invalid_anchors_keeps_the_rest(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    _seed_summary_with_directive(summary_store, "g_team", directive_id="c_dir1")
    manager = _FakeScopeManager(
        bootstrap_judgment=BootstrapJudgment(
            decision="accept",
            reasoning="Proposed two, one is invalid.",
            items=[
                BootstrapPublishedItemInput(
                    content="Good item.",
                    kind="context",
                    subject=None,
                    anchors=["deploy-notes"],
                ),
                BootstrapPublishedItemInput(
                    content="Bad item — anchors a directive that doesn't exist.",
                    kind="directive",
                    subject=None,
                    anchors=["directive:c_does_not_exist"],
                ),
            ],
        )
    )

    outcome = bootstrap_publication(
        "g_team",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.decision == "accept"
    assert len(outcome.items) == 1
    assert outcome.items[0].content == "Good item."
    assert len(record_store.list_publication_acts(scope_id="g_team")) == 1


def test_bootstrap_unknown_scope_raises_valueerror(record_store, summary_store) -> None:
    empty_fleet = FleetConfig(strata=[], scopes=[], edges=[])
    manager = _FakeScopeManager()
    with pytest.raises(ValueError, match="Scope not found"):
        bootstrap_publication(
            "g_nonexistent",
            fleet=empty_fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=manager,
        )
