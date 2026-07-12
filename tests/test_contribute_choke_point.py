"""Tests for the contribute choke point in ``strata.app``.

Covers the two defects the choke point was introduced to fix:

- **Issue #38** — per-scope serialization. Two concurrent contributions to the
  same scope must both be reflected in the final summary; the summary must
  always be explainable by the record (owner ruling 2026-07-10). Without the
  lock, both judge against the same stale summary and the last write wins —
  one accepted directive silently vanishes from the summary while its judgment
  survives in the record.

- **Issue #57** — judge-failure recovery. A ``judge()`` failure records the
  contribution and a judgment-attempt-failed *event* (never a fabricated
  verdict), leaves NO judgment row, and surfaces the contribution id so a
  retry routes to re-judge. ``rejudge_contribution`` is idempotent: a no-op
  when a verdict already exists, otherwise it judges against the *current*
  summary.

These exercise the module-level functions directly (the shared choke point);
the MCP-surface wiring is covered in ``test_mcp_server.py``.

Vocabulary follows CONTEXT.md: scope, contribution, judgment, record, scope
summary, scope-manager.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

# Make strata importable when running from the repo root.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.app import (  # noqa: E402
    JudgeUnavailable,
    rejudge_contribution,
    run_contribution,
)
from strata.fleet_config import FleetConfig  # noqa: E402
from strata.migrator import run_migrations  # noqa: E402
from strata.publication import read_publication  # noqa: E402
from strata.record_store import ContributorRef, RecordStore  # noqa: E402
from strata.scope_manager import ScopeManagerJudgment  # noqa: E402
from strata.summary_store import Directive, ScopeSummary, SummaryStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _fleet(root: Path) -> FleetConfig:
    """A single-scope fleet (g_root, L0) written under *root*."""
    fleet = {
        "strata": [{"id": "L0", "name": "executive", "ordinal": 0}],
        "scopes": [{"id": "g_root", "name": "Root", "stratum_id": "L0"}],
        "edges": [],
    }
    path = root / "fleet.yaml"
    path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return FleetConfig.load(path)


def _contributor() -> ContributorRef:
    return ContributorRef(
        scope_id="g_root",
        skill="strata-developer",
        session_id="sess_test",
        ts="2026-07-10T00:00:00+00:00",
    )


class _AccumulatingManager:
    """A scope-manager fake that accepts every contribution as a directive.

    It builds the rewritten summary from ``current_summary`` plus one new
    directive for the contribution being judged — a faithful read-modify-write.
    A deliberate delay between reading the incoming summary and returning the
    rewrite widens the race window: without the per-scope lock, a second
    concurrent judge reads the SAME stale summary here and its write clobbers
    the first accepted directive.
    """

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay

    def judge(
        self,
        *,
        scope,
        stratum,
        parent_summary,
        current_summary,
        recent_contributions,
        new_contribution,
        summary_max_words,
        entitlement,
        operator_memory=None,
        current_publication=None,
        peer_publications=None,
    ):  # noqa: ANN001, ANN201, E501
        existing = list(current_summary.directives) if current_summary is not None else []
        time.sleep(self.delay)
        new_directive = Directive(
            id=new_contribution.id,
            content=new_contribution.content,
            subject=new_contribution.subject,
            source_scope_id=scope.id,
            source_skill="strata-developer",
            created_at="2026-07-10T00:00:00+00:00",
        )
        summary = ScopeSummary(
            scope_id=scope.id,
            directives=[*existing, new_directive],
            context="",
            updated_at="2026-07-10T00:00:00+00:00",
        )
        return ScopeManagerJudgment(
            decision="accept_as_directive",
            reasoning="accepted",
            new_summary=summary,
        )


class _FailingManager:
    """A scope-manager fake whose ``judge`` always raises *exc*."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def judge(self, **_kwargs):  # noqa: ANN003, ANN201
        raise self._exc


# ---------------------------------------------------------------------------
# Issue #38 — per-scope serialization
# ---------------------------------------------------------------------------


def _run_concurrent_round(round_dir: Path) -> tuple[int, int]:
    """Fire two concurrent contributions to g_root; return (n_directives, n_judgments)."""
    round_dir.mkdir()
    db_path = str(round_dir / "strata.db")
    run_migrations(db_path)
    fleet = _fleet(round_dir)
    summary_store = SummaryStore(str(round_dir / "summaries"))
    manager = _AccumulatingManager()
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]

    def worker(n: int) -> None:
        # Each thread uses its own RecordStore connection (WAL, single writer
        # per connection) — exactly the shape of two concurrent requests.
        with RecordStore(db_path) as rs:
            run_contribution(
                scope=scope,
                stratum=stratum,
                content=f"directive {n}",
                proposed_classification="directive",
                subject=f"subject-{n}",
                supersedes=None,
                contributor=_contributor(),
                fleet=fleet,
                record_store=rs,
                summary_store=summary_store,
                scope_manager=manager,
                summary_max_words=500,
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = summary_store.read("g_root")
    with RecordStore(db_path) as rs:
        judgments = rs.list_judgments(scope_id="g_root")
    assert final is not None
    return len(final.directives), len(judgments)


def test_concurrent_contributions_both_reflected(tmp_path: Path) -> None:
    """Two concurrent contributions: both judgments in the record AND both
    accepted directives in the final summary — the summary is explainable by
    the record (issue #38). Repeated across many rounds so the race is
    meaningfully exercised.
    """
    rounds = 25
    for i in range(rounds):
        n_directives, n_judgments = _run_concurrent_round(tmp_path / f"round{i}")
        assert n_judgments == 2, f"round {i}: record must carry both judgments, got {n_judgments}"
        assert n_directives == 2, (
            f"round {i}: summary must reflect BOTH accepted directives "
            f"(the concurrency defect drops one), got {n_directives}"
        )


# ---------------------------------------------------------------------------
# Issue #57 — judge-failure recovery
# ---------------------------------------------------------------------------


def _setup(tmp_path: Path) -> tuple[str, FleetConfig, SummaryStore]:
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    fleet = _fleet(tmp_path)
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    return db_path, fleet, summary_store


def test_judge_failure_records_event_no_judgment_and_carries_id(tmp_path: Path) -> None:
    """A judge() failure records the contribution + a judgment-attempt-failed
    event with the error class, writes NO judgment, and raises an error
    carrying the contribution id (issue #57).
    """
    db_path, fleet, summary_store = _setup(tmp_path)
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]
    manager = _FailingManager(ValueError("LLM unavailable"))

    with pytest.raises(JudgeUnavailable) as exc_info, RecordStore(db_path) as rs:
        run_contribution(
            scope=scope,
            stratum=stratum,
            content="contribution before the crash",
            proposed_classification="directive",
            subject=None,
            supersedes=None,
            contributor=_contributor(),
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=manager,
            summary_max_words=500,
        )

    exc = exc_info.value
    assert exc.error_class == "ValueError"
    assert exc.contribution_id.startswith("c_")

    with RecordStore(db_path) as rs:
        contributions = rs.list_contributions(scope_id="g_root")
        judgments = rs.list_judgments(scope_id="g_root")
        attempts = rs.list_judgment_attempts(scope_id="g_root")

    # The record carries the contribution (the record never lies)...
    assert len(contributions) == 1
    assert contributions[0].id == exc.contribution_id
    # ...an attempt event with the error class...
    assert len(attempts) == 1
    assert attempts[0].contribution_id == exc.contribution_id
    assert attempts[0].error_class == "ValueError"
    # ...and NO judgment (a failure is never dressed as a verdict).
    assert judgments == []
    # The pending contribution never reaches readers: no summary was written.
    assert summary_store.read("g_root") is None


def test_rejudge_judges_pending_then_is_idempotent(tmp_path: Path) -> None:
    """rejudge_contribution judges a pending contribution against the current
    summary (appending exactly one judgment, updating the summary), and a
    second call is a no-op returning the same verdict (issue #57).
    """
    db_path, fleet, summary_store = _setup(tmp_path)
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]

    # Leave a pending contribution behind a judge() failure.
    with pytest.raises(JudgeUnavailable) as exc_info, RecordStore(db_path) as rs:
        run_contribution(
            scope=scope,
            stratum=stratum,
            content="recoverable contribution",
            proposed_classification="directive",
            subject=None,
            supersedes=None,
            contributor=_contributor(),
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=_FailingManager(ValueError("temporary outage")),
            summary_max_words=500,
        )
    contribution_id = exc_info.value.contribution_id

    # First re-judge: the scope-manager is back — it judges and updates state.
    good_manager = _AccumulatingManager(delay=0.0)
    with RecordStore(db_path) as rs:
        outcome = rejudge_contribution(
            contribution_id,
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=good_manager,
            summary_max_words=500,
        )
    assert outcome.decision == "accept_as_directive"
    assert outcome.summary_updated is True

    with RecordStore(db_path) as rs:
        assert len(rs.list_judgments(scope_id="g_root")) == 1
    final = summary_store.read("g_root")
    assert final is not None
    assert [d.content for d in final.directives] == ["recoverable contribution"]

    # Second re-judge: a verdict already exists → no-op. The scope-manager is
    # NOT invoked (a FailingManager proves the short-circuit), no second
    # judgment is written, and the summary is untouched.
    version_before = final.version
    with RecordStore(db_path) as rs:
        outcome2 = rejudge_contribution(
            contribution_id,
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=_FailingManager(RuntimeError("must not be called")),
            summary_max_words=500,
        )
    assert outcome2.decision == "accept_as_directive"
    assert outcome2.summary_updated is False

    with RecordStore(db_path) as rs:
        assert len(rs.list_judgments(scope_id="g_root")) == 1
    assert summary_store.read("g_root").version == version_before


def test_pending_contribution_never_enters_summary_or_perspective(tmp_path: Path) -> None:
    """A contribution with no judgment never appears in the scope summary — not
    after the failure, and not after a later accepted contribution rewrites the
    summary. Uncurated material must not reach readers (issue #57).
    """
    db_path, fleet, summary_store = _setup(tmp_path)
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]

    # A pending contribution (judge failed).
    with pytest.raises(JudgeUnavailable), RecordStore(db_path) as rs:
        run_contribution(
            scope=scope,
            stratum=stratum,
            content="PENDING material",
            proposed_classification="directive",
            subject=None,
            supersedes=None,
            contributor=_contributor(),
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=_FailingManager(ValueError("down")),
            summary_max_words=500,
        )
    # No summary at all yet — the pending item is invisible to readers.
    assert summary_store.read("g_root") is None

    # A later accepted contribution rewrites the summary from current state,
    # which still contains no trace of the pending material.
    with RecordStore(db_path) as rs:
        run_contribution(
            scope=scope,
            stratum=stratum,
            content="ACCEPTED material",
            proposed_classification="directive",
            subject=None,
            supersedes=None,
            contributor=_contributor(),
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=_AccumulatingManager(delay=0.0),
            summary_max_words=500,
        )

    final = summary_store.read("g_root")
    assert final is not None
    contents = [d.content for d in final.directives]
    assert "ACCEPTED material" in contents
    assert all("PENDING" not in c for c in contents)


# ---------------------------------------------------------------------------
# ADR 0008 D3 — run_contribution wires operator_memory_binding into judge()
# ---------------------------------------------------------------------------


class _CapturingManager:
    """A scope-manager fake that records the kwargs it was judged with."""

    def __init__(self) -> None:
        self.received_operator_memory = "UNSET"

    def judge(
        self,
        *,
        scope,
        stratum,
        parent_summary,
        current_summary,
        recent_contributions,
        new_contribution,
        summary_max_words,
        entitlement,
        operator_memory=None,
        current_publication=None,
        peer_publications=None,
    ):  # noqa: ANN001, ANN201, E501
        self.received_operator_memory = operator_memory
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="captured",
            new_summary=ScopeSummary(
                scope_id=scope.id,
                directives=[],
                context="captured",
                updated_at="2026-07-12T00:00:00+00:00",
            ),
        )


def test_run_contribution_passes_operator_memory_binding_to_judge(tmp_path: Path) -> None:
    """run_contribution fetches operator_memory_binding(scope.id, ...) and passes it to judge()."""
    from strata.operator import operator_publish

    db_path, fleet, summary_store = _setup(tmp_path)
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]

    with RecordStore(db_path) as rs:
        operator_publish(
            "g_root",
            "Operator-mandated directive.",
            "directive",
            record_store=rs,
            summaries_dir=summary_store.summaries_dir,
        )

    manager = _CapturingManager()
    with RecordStore(db_path) as rs:
        run_contribution(
            scope=scope,
            stratum=stratum,
            content="some material",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_contributor(),
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=manager,
            summary_max_words=500,
        )

    assert manager.received_operator_memory != "UNSET"
    assert manager.received_operator_memory is not None
    assert [scope_id for scope_id, _ in manager.received_operator_memory] == ["g_root"]
    items = manager.received_operator_memory[0][1]
    assert items[0].content == "Operator-mandated directive."


def test_rejudge_contribution_passes_operator_memory_binding_to_judge(tmp_path: Path) -> None:
    """rejudge_contribution also fetches operator_memory_binding and passes it through."""
    from strata.operator import operator_publish

    db_path, fleet, summary_store = _setup(tmp_path)
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]

    with pytest.raises(JudgeUnavailable), RecordStore(db_path) as rs:
        run_contribution(
            scope=scope,
            stratum=stratum,
            content="PENDING material",
            proposed_classification="directive",
            subject=None,
            supersedes=None,
            contributor=_contributor(),
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=_FailingManager(ValueError("down")),
            summary_max_words=500,
        )
    with RecordStore(db_path) as rs:
        pending = rs.list_contributions(scope_id="g_root")[0]
        operator_publish(
            "g_root",
            "Operator directive for rejudge.",
            "directive",
            record_store=rs,
            summaries_dir=summary_store.summaries_dir,
        )

    manager = _CapturingManager()
    with RecordStore(db_path) as rs:
        rejudge_contribution(
            pending.id,
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=manager,
            summary_max_words=500,
        )

    assert manager.received_operator_memory is not None
    items = manager.received_operator_memory[0][1]
    assert items[0].content == "Operator directive for rejudge."


# ---------------------------------------------------------------------------
# ADR 0007 D3 — staleness propagation wired into the choke point.
# ---------------------------------------------------------------------------


def _seed_publish_act(
    record_store: RecordStore, summaries_dir, scope_id: str, *, kind: str, content: str, anchors
):
    """Append a real publish act + accept judgment, and write the matching artifact."""
    from strata.publication import PublishedItem, _write_publication

    act = record_store.append_publication_act(
        scope_id=scope_id,
        act="publish",
        kind=kind,
        content=content,
        subject=None,
        anchors=anchors,
        withdraws=None,
        trigger=None,
        proposer=_contributor(),
    )
    record_store.record_publication_judgment(
        act_id=act.id, decision="accept", judged_by="scope-manager", reasoning="seeded"
    )
    item = PublishedItem(
        id=act.id,
        kind=kind,
        content=content,
        subject=None,
        anchors=anchors,
        published_at=act.created_at,
    )
    existing = read_publication(scope_id, summaries_dir=str(summaries_dir))
    _write_publication(scope_id, [*existing, item], summaries_dir=str(summaries_dir))
    return item


class _DirectiveDroppingManager:
    """A scope-manager fake whose rewrite drops the existing directive (mechanical propagation)."""

    def judge(self, *, scope, new_contribution, **_kwargs):  # noqa: ANN001, ANN201
        summary = ScopeSummary(
            scope_id=scope.id,
            directives=[],  # the existing directive is gone
            context="rewritten, directive dropped",
            updated_at="2026-07-12T00:00:00+00:00",
        )
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="rewrite drops the directive",
            new_summary=summary,
        )


def test_mechanical_propagation_withdraws_item_on_accepted_rewrite(tmp_path: Path) -> None:
    """An accepted rewrite drops a directive; a published item anchored only to it is withdrawn."""
    db_path, fleet, summary_store = _setup(tmp_path)
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]

    summary_store.write(
        "g_root",
        ScopeSummary(
            scope_id="g_root",
            directives=[
                Directive(
                    id="c_existing1",
                    content="Existing directive.",
                    subject=None,
                    source_scope_id="g_root",
                    source_skill="strata-developer",
                    created_at="2026-07-10T00:00:00+00:00",
                )
            ],
            context="",
            updated_at="2026-07-10T00:00:00+00:00",
        ),
    )

    with RecordStore(db_path) as rs:
        item = _seed_publish_act(
            rs,
            summary_store.summaries_dir,
            "g_root",
            kind="directive",
            content="Published version of the directive.",
            anchors=["directive:c_existing1"],
        )

        outcome = run_contribution(
            scope=scope,
            stratum=stratum,
            content="new observation",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_contributor(),
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=_DirectiveDroppingManager(),
            summary_max_words=500,
        )

        remaining = read_publication("g_root", summaries_dir=str(summary_store.summaries_dir))
        assert remaining == []

        acts = rs.list_publication_acts(scope_id="g_root")
        withdraw_act = next(a for a in acts if a.act == "withdraw")
        assert withdraw_act.withdraws == item.id
        assert withdraw_act.trigger == outcome.contribution_id
        # Mechanical propagation: no judgment row for the withdrawal.
        assert rs.get_publication_judgment(withdraw_act.id) is None


class _WithdrawPublishedManager:
    """A scope-manager fake whose judgment names a published item for judged withdrawal."""

    def __init__(self, item_id: str) -> None:
        self._item_id = item_id

    def judge(self, *, scope, new_contribution, **_kwargs):  # noqa: ANN001, ANN201
        summary = ScopeSummary(
            scope_id=scope.id,
            directives=[],
            context="rewritten, belief changed",
            updated_at="2026-07-12T00:00:00+00:00",
        )
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="belief changed, withdraw the stale export",
            new_summary=summary,
            withdraw_published=[self._item_id],
        )


def test_judged_propagation_withdraws_item_named_by_judgment(tmp_path: Path) -> None:
    """withdraw_published on an accepted judgment withdraws that item WITH a judgment row."""
    db_path, fleet, summary_store = _setup(tmp_path)
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]

    with RecordStore(db_path) as rs:
        item = _seed_publish_act(
            rs,
            summary_store.summaries_dir,
            "g_root",
            kind="context",
            content="Stale belief.",
            anchors=["subject:status"],
        )

        run_contribution(
            scope=scope,
            stratum=stratum,
            content="new observation contradicting the stale belief",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_contributor(),
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=_WithdrawPublishedManager(item.id),
            summary_max_words=500,
        )

        remaining = read_publication("g_root", summaries_dir=str(summary_store.summaries_dir))
        assert remaining == []

        acts = rs.list_publication_acts(scope_id="g_root")
        withdraw_act = next(a for a in acts if a.act == "withdraw")
        assert withdraw_act.withdraws == item.id
        assert withdraw_act.trigger is None

        judgment = rs.get_publication_judgment(withdraw_act.id)
        assert judgment is not None
        assert judgment.decision == "accept"
        assert judgment.judged_by == "scope-manager"
        assert judgment.reasoning == "belief changed, withdraw the stale export"


class _PublicationCapturingManager:
    """A scope-manager fake that records the current_publication/peer_publications it was given."""

    def __init__(self) -> None:
        self.received_current_publication = "UNSET"
        self.received_peer_publications = "UNSET"

    def judge(self, *, scope, current_publication=None, peer_publications=None, **_kwargs):  # noqa: ANN001, ANN201
        self.received_current_publication = current_publication
        self.received_peer_publications = peer_publications
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="captured",
            new_summary=ScopeSummary(
                scope_id=scope.id,
                directives=[],
                context="captured",
                updated_at="2026-07-12T00:00:00+00:00",
            ),
        )


def test_run_contribution_passes_current_publication_to_judge(tmp_path: Path) -> None:
    """run_contribution reads this scope's own publication and passes it to judge()."""
    db_path, fleet, summary_store = _setup(tmp_path)
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]

    with RecordStore(db_path) as rs:
        item = _seed_publish_act(
            rs,
            summary_store.summaries_dir,
            "g_root",
            kind="context",
            content="Currently published.",
            anchors=["subject:x"],
        )

        manager = _PublicationCapturingManager()
        run_contribution(
            scope=scope,
            stratum=stratum,
            content="observation",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_contributor(),
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=manager,
            summary_max_words=500,
        )

    assert manager.received_current_publication is not None
    assert manager.received_current_publication != "UNSET"
    assert [i.id for i in manager.received_current_publication] == [item.id]
    # No referenced peers in this single-scope fleet — an empty list, not None.
    assert manager.received_peer_publications == []
