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
    _judge_batch_and_record,
    rejudge_contribution,
    run_contribution,
)
from strata.fleet_config import FleetConfig  # noqa: E402
from strata.locks import scope_append_lock, scope_queue  # noqa: E402
from strata.locks import scope_lock as _scope_lock  # noqa: E402
from strata.migrator import run_migrations  # noqa: E402
from strata.publication import read_publication  # noqa: E402
from strata.record_store import JUDGE_FAILED, ContributorRef, RecordStore  # noqa: E402
from strata.scope_manager import (  # noqa: E402
    BatchVerdict,
    DirectiveOp,
    ScopeManagerBatchJudgment,
    ScopeManagerJudgment,
    _apply_amendment,
    _apply_batch_amendment,
)
from strata.summary_store import Directive, ScopeSummary, SummaryStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _fleet(root: Path, scope_id: str = "g_root") -> FleetConfig:
    """A single-scope fleet (``scope_id``, L0) written under *root*.

    The scope id is a parameter because the judgment queue is process-wide and
    keyed by scope (ADR 0011 D3): a test that leaves contributions queued must
    not be able to reach another test's drain.
    """
    fleet = {
        "strata": [{"id": "L0", "name": "executive", "ordinal": 0}],
        "scopes": [{"id": scope_id, "name": "Root", "stratum_id": "L0"}],
        "edges": [],
    }
    root.mkdir(parents=True, exist_ok=True)
    path = root / "fleet.yaml"
    path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return FleetConfig.load(path)


def _contributor(skill: str | None = "strata-developer") -> ContributorRef:
    return ContributorRef(
        scope_id="g_root",
        skill=skill,
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
        window_verbatim_tail=None,
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


class _SkillEchoManager:
    """A scope-manager fake that accepts as a directive, echoing the
    contributor's skill (possibly ``None``) into the directive's
    ``source_skill`` — so a skill-less contribution round-trips as a
    skill-less directive (issue #121)."""

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
        window_verbatim_tail=None,
    ):  # noqa: ANN001, ANN201, E501
        directive = Directive(
            id=new_contribution.id,
            content=new_contribution.content,
            subject=new_contribution.subject,
            source_scope_id=scope.id,
            source_skill=new_contribution.contributor.skill,
            created_at="2026-07-10T00:00:00+00:00",
        )
        summary = ScopeSummary(
            scope_id=scope.id,
            directives=[directive],
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
# Issue #121 — skill is optional end-to-end
# ---------------------------------------------------------------------------


def test_skilless_contribution_accepted_end_to_end(tmp_path: Path) -> None:
    """A contribution whose contributor carries no skill is accepted, recorded,
    and composed into the summary — with no ``None`` placeholder anywhere
    (issue #121).

    Exercises the full choke point (append -> judge -> record-judgment ->
    summary-write) with the stub-judge seam the other tests use.
    """
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    fleet = _fleet(tmp_path)
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]

    with RecordStore(db_path) as rs:
        outcome = run_contribution(
            scope=scope,
            stratum=stratum,
            content="a skill-less observation",
            proposed_classification="directive",
            subject="topic",
            supersedes=None,
            contributor=_contributor(skill=None),
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=_SkillEchoManager(),
            summary_max_words=500,
        )
        assert outcome.decision == "accept_as_directive"

        # The record preserves the skill-less provenance verbatim (None, not "").
        stored = rs.get_contribution(outcome.contribution_id)
        assert stored is not None
        assert stored.contributor.skill is None
        assert stored.contributor.scope_id == "g_root"

    # The summary composed the directive with no skill, and the on-disk
    # markdown never renders the literal "None" or a bare "skill=".
    final = summary_store.read("g_root")
    assert final is not None
    assert len(final.directives) == 1
    assert final.directives[0].source_skill is None
    raw = summary_store.path_for("g_root").read_text(encoding="utf-8")
    assert "skill=" not in raw
    assert "None" not in raw


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
        window_verbatim_tail=None,
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


class _DirectiveRetiringManager:
    """A scope-manager fake whose amendment retires the existing directive.

    Under ADR 0011 D1 a directive leaves the summary only through an
    id-addressed op, and the mechanical propagation reads the removed ids off
    those ops — so the fake has to carry the ``retire`` op, not merely a
    summary that no longer lists the directive.
    """

    def __init__(self, directive_id: str) -> None:
        self._directive_id = directive_id

    def judge(self, *, scope, new_contribution, **_kwargs):  # noqa: ANN001, ANN201
        summary = ScopeSummary(
            scope_id=scope.id,
            directives=[],  # the existing directive is gone
            context="amended, directive retired",
            updated_at="2026-07-12T00:00:00+00:00",
        )
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="the directive no longer applies",
            new_summary=summary,
            directive_ops=[DirectiveOp(op="retire", id=self._directive_id)],
            new_context="amended, directive retired",
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
            scope_manager=_DirectiveRetiringManager("c_existing1"),
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


# ---------------------------------------------------------------------------
# Issue #118 — the mechanical failed-judgment marker, end to end
# ---------------------------------------------------------------------------


def test_terminal_judge_failure_marks_the_attempt_and_reads_as_judge_errored(
    tmp_path: Path,
) -> None:
    """A judge() failure through the real choke point produces the marker row.

    ``judge()`` exhausts its own corrective re-asks (#113's parse re-ask,
    #63's overflow re-ask) before it raises, so reaching the choke point's
    handler means the judge run is over. The attempt is marked JUDGE_FAILED
    and the contribution reads as "attempted, judge errored" — not as one
    still in flight (issue #118).
    """
    db_path, fleet, summary_store = _setup(tmp_path)
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]
    manager = _FailingManager(ValueError("new_summary was a string, twice"))

    with pytest.raises(JudgeUnavailable), RecordStore(db_path) as rs:
        run_contribution(
            scope=scope,
            stratum=stratum,
            content="the judge could not parse this one",
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

    with RecordStore(db_path) as rs:
        (attempt,) = rs.list_judgment_attempts(scope_id="g_root")
        (state,) = rs.list_contribution_states(scope_id="g_root")
        judgments = rs.list_judgments(scope_id="g_root")

    assert attempt.outcome == JUDGE_FAILED
    assert attempt.error_class == "ValueError"

    assert state.state == "judge_failed"
    assert state.error_class == "ValueError"
    assert "new_summary was a string" in (state.error_message or "")
    # Still no verdict: the marker is an event, never a fabricated decision.
    assert state.decision is None
    assert judgments == []
    # And nothing uncurated reached readers.
    assert summary_store.read("g_root") is None


def test_marker_path_makes_no_judge_call_and_only_grows_the_record(tmp_path: Path) -> None:
    """Recording the marker is mechanical, and the record only ever grows.

    Two failures then a successful re-judge: every step appends rows and
    rewrites none, and the contribution ends up judged rather than stuck at
    the marker — a re-judge is never blocked by it.
    """
    db_path, fleet, summary_store = _setup(tmp_path)
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]

    class _CountingFailingManager(_FailingManager):
        """Counts judge() calls so the marker path can be shown to make none."""

        calls = 0

        def judge(self, **kwargs):  # noqa: ANN003, ANN201
            type(self).calls += 1
            return super().judge(**kwargs)

    manager = _CountingFailingManager(ValueError("LLM unavailable"))

    def _row_counts() -> tuple[int, int, int]:
        with RecordStore(db_path) as rs:
            return (
                len(rs.list_contributions(scope_id="g_root")),
                len(rs.list_judgments(scope_id="g_root")),
                len(rs.list_judgment_attempts(scope_id="g_root")),
            )

    with pytest.raises(JudgeUnavailable) as exc_info, RecordStore(db_path) as rs:
        run_contribution(
            scope=scope,
            stratum=stratum,
            content="first try",
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
    contribution_id = exc_info.value.contribution_id

    # judge() ran exactly once — the marker itself cost no judge call.
    assert _CountingFailingManager.calls == 1
    after_first = _row_counts()
    assert after_first == (1, 0, 1)

    # A failing re-judge appends a second marked attempt; nothing is rewritten.
    with pytest.raises(JudgeUnavailable), RecordStore(db_path) as rs:
        rejudge_contribution(
            contribution_id,
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=manager,
            summary_max_words=500,
        )
    after_retry = _row_counts()
    assert after_retry == (1, 0, 2)

    # The marker never occupies the judgment slot: a working judge still lands
    # a verdict, and the two marked attempts stay on the record beside it.
    with RecordStore(db_path) as rs:
        outcome = rejudge_contribution(
            contribution_id,
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=_AccumulatingManager(delay=0.0),
            summary_max_words=500,
        )
    assert outcome.decision == "accept_as_directive"

    final = _row_counts()
    assert final == (1, 1, 2)
    # Every count is monotonic across the whole sequence.
    for before, after in ((after_first, after_retry), (after_retry, final)):
        assert all(b <= a for b, a in zip(before, after, strict=True))

    with RecordStore(db_path) as rs:
        (state,) = rs.list_contribution_states(scope_id="g_root")
    assert state.state == "judged"
    assert state.failed_attempts == 2


# ---------------------------------------------------------------------------
# ADR 0011 D1 — the amendment's ops reach the record: retirement events, the
# ops-sourced propagation source, and the dropped-op note.
# ---------------------------------------------------------------------------


class _AmendingManager:
    """A scope-manager fake returning a fixed amendment plus its applied summary."""

    def __init__(
        self,
        *,
        ops: list[DirectiveOp],
        directives: list[Directive],
        dropped_ops: list[str] | None = None,
        reasoning: str = "amended",
    ) -> None:
        self._ops = ops
        self._directives = directives
        self._dropped_ops = dropped_ops or []
        self._reasoning = reasoning

    def judge(self, *, scope, new_contribution, **_kwargs):  # noqa: ANN001, ANN201
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning=self._reasoning,
            new_summary=ScopeSummary(
                scope_id=scope.id,
                directives=self._directives,
                context="amended",
                updated_at="2026-08-12T00:00:00+00:00",
            ),
            directive_ops=self._ops,
            new_context="amended",
            dropped_ops=self._dropped_ops,
        )


def _existing_directive(directive_id: str = "c_existing1") -> Directive:
    return Directive(
        id=directive_id,
        content="Existing directive.",
        subject=None,
        source_scope_id="g_root",
        source_skill="strata-developer",
        created_at="2026-07-10T00:00:00+00:00",
    )


def _run(tmp_path: Path, manager, *, seeded: list[Directive] | None = None):  # noqa: ANN001
    """Seed a summary, run one contribution through the choke point.

    Returns ``(db_path, outcome, summary_store)`` — the record store is closed,
    so a caller that wants to inspect the record opens its own.
    """
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    fleet = _fleet(tmp_path)
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]

    if seeded is not None:
        summary_store.write(
            "g_root",
            ScopeSummary(
                scope_id="g_root",
                directives=seeded,
                context="",
                updated_at="2026-07-10T00:00:00+00:00",
            ),
        )

    with RecordStore(db_path) as rs:
        outcome = run_contribution(
            scope=scope,
            stratum=stratum,
            content="an observation",
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
    return db_path, outcome, summary_store


def test_retire_op_appends_a_retirement_row_by_the_scope_manager(tmp_path: Path) -> None:
    """A `retire` op lands as a Retirement event with retired_by='scope-manager'."""
    directive = _existing_directive()
    manager = _AmendingManager(
        ops=[DirectiveOp(op="retire", id=directive.id)],
        directives=[],
        reasoning="the rule no longer applies",
    )
    db_path, outcome, summary_store = _run(tmp_path, manager, seeded=[directive])

    with RecordStore(db_path) as rs:
        retirements = rs.list_retirements(scope_id="g_root")
        assert len(retirements) == 1
        assert retirements[0].directive_id == directive.id
        assert retirements[0].retired_by == "scope-manager"
        assert retirements[0].reason == "the rule no longer applies"

    # No tombstone in the summary (CONTEXT.md § Retirement).
    written = summary_store.read("g_root")
    assert written is not None
    assert written.directives == []
    assert outcome.summary_updated is True


def test_supersede_op_removes_without_a_retirement_row(tmp_path: Path) -> None:
    """Supersession's explanation is the incoming directive, not a Retirement row."""
    old = _existing_directive()
    replacement = Directive(
        id="c_new1",
        content="Replacement directive.",
        subject=None,
        source_scope_id="g_root",
        source_skill="strata-developer",
        created_at="2026-08-12T00:00:00+00:00",
    )
    manager = _AmendingManager(
        ops=[DirectiveOp(op="append"), DirectiveOp(op="supersede", id=old.id)],
        directives=[replacement],
    )
    db_path, _outcome, summary_store = _run(tmp_path, manager, seeded=[old])

    with RecordStore(db_path) as rs:
        assert rs.list_retirements(scope_id="g_root") == []

    written = summary_store.read("g_root")
    assert written is not None
    assert [d.id for d in written.directives] == ["c_new1"]


def test_mechanical_propagation_reads_removed_ids_from_the_ops(tmp_path: Path) -> None:
    """The withdrawal fires off the supersede op, not off a summary diff.

    The amendment's applied summary still lists a directive with the same id
    (a supersession that keeps the id would be a strange but harmless case);
    what makes the published item stale is the op naming it, and the ops are
    now the only source the propagation reads.
    """
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    fleet = _fleet(tmp_path)
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]

    directive = _existing_directive()
    summary_store.write(
        "g_root",
        ScopeSummary(
            scope_id="g_root",
            directives=[directive],
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
            anchors=[f"directive:{directive.id}"],
        )

        outcome = run_contribution(
            scope=scope,
            stratum=stratum,
            content="a replacement rule",
            proposed_classification="directive",
            subject=None,
            supersedes=None,
            contributor=_contributor(),
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=_AmendingManager(
                ops=[DirectiveOp(op="append"), DirectiveOp(op="supersede", id=directive.id)],
                directives=[
                    Directive(
                        id="c_replacement",
                        content="Replacement directive.",
                        subject=None,
                        source_scope_id="g_root",
                        source_skill="strata-developer",
                        created_at="2026-08-12T00:00:00+00:00",
                    )
                ],
            ),
            summary_max_words=500,
        )

        assert read_publication("g_root", summaries_dir=str(summary_store.summaries_dir)) == []
        withdraw_act = next(
            a for a in rs.list_publication_acts(scope_id="g_root") if a.act == "withdraw"
        )
        assert withdraw_act.withdraws == item.id
        assert withdraw_act.trigger == outcome.contribution_id


def test_dropped_op_is_noted_in_the_judgment_record(tmp_path: Path) -> None:
    """The record shows which part of the amendment the engine dropped."""
    manager = _AmendingManager(
        ops=[],
        directives=[],
        dropped_ops=["retire(c_ghost)"],
        reasoning="recording the observation",
    )
    db_path, outcome, _summary_store = _run(tmp_path, manager)

    with RecordStore(db_path) as rs:
        judgment = rs.get_judgment(outcome.contribution_id)
        assert judgment is not None
        assert judgment.notes is not None
        assert "recording the observation" in judgment.notes
        assert "retire(c_ghost)" in judgment.notes
        # The caller's own outcome carries the verdict unchanged.
        assert outcome.reasoning == "recording the observation"


# ---------------------------------------------------------------------------
# ADR 0011 D2 — the recency window the choke point hands the judge
# ---------------------------------------------------------------------------


class _WindowCapturingManager:
    """A scope-manager fake that records the recency window it was handed."""

    def __init__(self) -> None:
        self.windows: list[list] = []

    def judge(self, *, scope, new_contribution, recent_contributions, **_kwargs):  # noqa: ANN001, ANN201
        self.windows.append(list(recent_contributions))
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="recorded",
            new_summary=ScopeSummary(
                scope_id=scope.id,
                directives=[],
                context=new_contribution.content,
                updated_at="2026-08-12T00:00:00+00:00",
            ),
            new_context=new_contribution.content,
        )


def test_window_carries_state_and_notes_with_the_judged_row(tmp_path: Path) -> None:
    """The judge's window is (contribution, state, judgment-notes) triples (ADR 0011 D2).

    The contribution under judgment is appended to the record BEFORE the window
    is read, so it is always in its own window — as a `pending` row, since its
    verdict does not exist yet. The one before it is `judged` and carries the
    notes recorded for it.
    """
    db_path, fleet, summary_store = _setup(tmp_path)
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]
    manager = _WindowCapturingManager()

    with RecordStore(db_path) as rs:
        for content in ("first observation", "second observation"):
            run_contribution(
                scope=scope,
                stratum=stratum,
                content=content,
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

    first_window, second_window = manager.windows

    # A scope's very first contribution sees itself, pending, and nothing else.
    assert [r.state for r in first_window] == ["pending"]
    assert first_window[0].contribution.content == "first observation"
    assert first_window[0].judgment_notes is None

    # The second call's window is oldest-first: the now-judged first
    # contribution with its notes, then itself, still pending.
    assert [r.state for r in second_window] == ["judged", "pending"]
    assert second_window[0].contribution.content == "first observation"
    assert second_window[0].decision == "accept_as_context"
    assert second_window[0].judgment_notes == "recorded"
    assert second_window[1].contribution.content == "second observation"
    assert second_window[1].decision is None


# ---------------------------------------------------------------------------
# ADR 0011 D3 — multi-contribution judgment with queue coalescing
# ---------------------------------------------------------------------------


def _extended(context: str, content: str) -> str:
    """The scripted judge's context rule: append the new content to the digest."""
    return f"{context} | {content}" if context else content


class _ScriptedBatchManager:
    """A deterministic scope-manager fake with BOTH judgment modes.

    The verdict is mechanical — decline anything whose content starts with
    ``DECLINE``, otherwise accept as a directive — and the amendment is built
    with the engine's own apply helpers, so serial and batch judgment differ in
    exactly one thing: how many calls they take. That is what makes the
    equivalence assertions meaningful rather than tautological.

    ``gate`` blocks the FIRST judgment (either mode) until the test releases
    it, which is how the coalescing tests get a judgment reliably in flight
    while other contributions arrive — no sleeps, no timing luck.
    """

    def __init__(self, *, gate: threading.Event | None = None) -> None:
        self.judge_calls: list[list[str]] = []
        self.batch_calls: list[list[str]] = []
        self._gate = gate
        self._gate_used = False
        self._lock = threading.Lock()

    def _pause(self) -> None:
        with self._lock:
            first = self._gate is not None and not self._gate_used
            self._gate_used = True
        if first:
            assert self._gate.wait(timeout=10.0), "the test never released the gated judgment"

    @staticmethod
    def _decision(contribution) -> str:  # noqa: ANN001
        return "decline" if contribution.content.startswith("DECLINE") else "accept_as_directive"

    def judge(self, *, scope, current_summary, new_contribution, **_kwargs):  # noqa: ANN001, ANN201
        with self._lock:
            self.judge_calls.append([new_contribution.id])
        self._pause()
        if self._decision(new_contribution) == "decline":
            return ScopeManagerJudgment(
                decision="decline",
                reasoning=f"declined: {new_contribution.content}",
                new_summary=None,
            )
        ops = [DirectiveOp(op="append")]
        context = _extended(
            current_summary.context if current_summary is not None else "",
            new_contribution.content,
        )
        return ScopeManagerJudgment(
            decision="accept_as_directive",
            reasoning=f"accepted: {new_contribution.content}",
            new_summary=_apply_amendment(
                scope=scope,
                current_summary=current_summary,
                contribution=new_contribution,
                ops=ops,
                new_context=context,
            ),
            directive_ops=ops,
            new_context=context,
        )

    def judge_batch(self, *, scope, current_summary, new_contributions, **_kwargs):  # noqa: ANN001, ANN201
        with self._lock:
            self.batch_calls.append([c.id for c in new_contributions])
        self._pause()
        verdicts: list[BatchVerdict] = []
        ops: list[DirectiveOp] = []
        context = current_summary.context if current_summary is not None else ""
        for contribution in new_contributions:
            decision = self._decision(contribution)
            if decision == "decline":
                verdicts.append(
                    BatchVerdict(
                        contribution_id=contribution.id,
                        decision="decline",
                        reasoning=f"declined: {contribution.content}",
                    )
                )
                continue
            verdicts.append(
                BatchVerdict(
                    contribution_id=contribution.id,
                    decision="accept_as_directive",
                    reasoning=f"accepted: {contribution.content}",
                )
            )
            ops.append(DirectiveOp(op="append", contribution_id=contribution.id))
            context = _extended(context, contribution.content)
        accepted = [v for v in verdicts if v.decision != "decline"]
        return ScopeManagerBatchJudgment(
            verdicts=verdicts,
            new_summary=(
                _apply_batch_amendment(
                    scope=scope,
                    current_summary=current_summary,
                    contributions={c.id: c for c in new_contributions},
                    ops=ops,
                    new_context=context,
                )
                if accepted
                else None
            ),
            directive_ops=ops,
            new_context=context if accepted else None,
        )


class _FailingBatchManager(_FailingManager):
    """A scope-manager fake whose batch judgment always raises *exc*."""

    def judge_batch(self, **_kwargs):  # noqa: ANN003, ANN201
        raise self._exc


def _contribute(
    tmp_path: Path,
    contents,  # noqa: ANN001
    *,
    manager,  # noqa: ANN001
    scope_id: str = "g_root",
    **kwargs,  # noqa: ANN003
) -> tuple[str, SummaryStore, list]:
    """Run *contents* through the choke point one at a time (the serial path)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    fleet = _fleet(tmp_path, scope_id=scope_id)
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    scope = fleet.get_scope(scope_id)
    stratum = fleet.strata[0]

    outcomes = []
    with RecordStore(db_path) as rs:
        for content in contents:
            outcomes.append(
                run_contribution(
                    scope=scope,
                    stratum=stratum,
                    content=content,
                    proposed_classification="directive",
                    subject=None,
                    supersedes=None,
                    contributor=_contributor(),
                    fleet=fleet,
                    record_store=rs,
                    summary_store=summary_store,
                    scope_manager=manager,
                    summary_max_words=500,
                    **kwargs,
                )
            )
    return db_path, summary_store, outcomes


def _append_and_judge_as_batch(
    tmp_path: Path,
    contents,  # noqa: ANN001
    *,
    manager,  # noqa: ANN001
    scope_id: str = "g_root",
):
    """Append *contents* to the record, then judge them all in ONE batch call."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    fleet = _fleet(tmp_path, scope_id=scope_id)
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    scope = fleet.get_scope(scope_id)
    stratum = fleet.strata[0]

    with RecordStore(db_path) as rs:
        contributions = [
            rs.append_contribution(
                scope_id=scope.id,
                content=content,
                proposed_classification="directive",
                subject=None,
                supersedes=None,
                contributor=_contributor(),
            )
            for content in contents
        ]
        with _scope_lock(scope.id):
            results = _judge_batch_and_record(
                contributions=contributions,
                scope=scope,
                stratum=stratum,
                fleet=fleet,
                record_store=rs,
                summary_store=summary_store,
                scope_manager=manager,
                summary_max_words=500,
            )
    return db_path, summary_store, contributions, results


# -- J10: batch equivalence --------------------------------------------------


def test_j10_batch_matches_serial_verdicts_summary_and_costs_one_version(
    tmp_path: Path,
) -> None:
    """J10 — same contributions, same order: same verdicts, same final summary.

    Three serial judgments and one batch of three differ in exactly two
    things: the number of judge calls, and the number of summary writes (three
    ``version`` increments against one, ADR 0011 D3).
    """
    contents = ["alpha rule", "beta rule", "gamma rule"]

    serial_manager = _ScriptedBatchManager()
    _, serial_store, serial_outcomes = _contribute(
        tmp_path / "serial", contents, manager=serial_manager
    )
    batch_manager = _ScriptedBatchManager()
    _, batch_store, _contributions, batch_results = _append_and_judge_as_batch(
        tmp_path / "batch", contents, manager=batch_manager
    )

    # Same verdicts, in the same order, with the same reasoning.
    assert [(o.decision, o.reasoning) for o in serial_outcomes] == [
        (r.decision, r.reasoning) for r in batch_results
    ]
    assert all(o.summary_updated for o in serial_outcomes)
    assert all(r.summary_updated for r in batch_results)

    # Same final summary: same directives, in the same order, same context.
    serial_summary = serial_store.read("g_root")
    batch_summary = batch_store.read("g_root")
    assert [d.content for d in serial_summary.directives] == contents
    assert [d.content for d in batch_summary.directives] == contents
    assert serial_summary.context == batch_summary.context == "alpha rule | beta rule | gamma rule"

    # One judge call and one version increment for the batch; three of each
    # for serial judgment.
    assert serial_manager.judge_calls == [[o.contribution_id] for o in serial_outcomes]
    assert serial_manager.batch_calls == []
    assert batch_manager.batch_calls == [[r.contribution_id for r in batch_results]]
    assert batch_manager.judge_calls == []
    assert serial_summary.version == 3
    assert batch_summary.version == 1


def test_batch_stamps_parent_version_from_the_summary_read_at_batch_start(
    tmp_path: Path,
) -> None:
    """One batch, one stamp — from the parent summary as it stood at batch start."""
    _db_path, summary_store, _contributions, _results = _append_and_judge_as_batch(
        tmp_path, ["one", "two"], manager=_ScriptedBatchManager()
    )
    written = summary_store.read("g_root")
    # A root scope has no inter-stratum parent, so the stamp is None — and it
    # is stamped exactly once, on the single write the batch performs.
    assert written.parent_version is None
    assert written.version == 1


def test_one_declined_member_does_not_poison_the_batch(tmp_path: Path) -> None:
    """Mixed verdicts land correctly, each on its own judgment row."""
    contents = ["kept rule", "DECLINE this one", "another kept rule"]
    db_path, summary_store, contributions, results = _append_and_judge_as_batch(
        tmp_path, contents, manager=_ScriptedBatchManager()
    )

    assert [r.decision for r in results] == [
        "accept_as_directive",
        "decline",
        "accept_as_directive",
    ]
    # The declined member reports no summary update; its batch-mates do.
    assert [r.summary_updated for r in results] == [True, False, True]

    # One judgment row per contribution, each against its own id, each with
    # its own reasoning (the UNIQUE constraint holds — three rows, three ids).
    with RecordStore(db_path) as rs:
        judgments = {j.contribution_id: j for j in rs.list_judgments(scope_id="g_root")}
        rows = rs.list_contribution_states(scope_id="g_root")
        states = {s.contribution_id: s.state for s in rows}
    assert len(judgments) == 3
    for contribution, expected in zip(contributions, results, strict=True):
        assert judgments[contribution.id].decision == expected.decision
        assert judgments[contribution.id].notes == expected.reasoning
        assert judgments[contribution.id].judged_by == "scope-manager"
        assert states[contribution.id] == "judged"

    # The declined contribution never reaches the summary.
    written = summary_store.read("g_root")
    assert [d.content for d in written.directives] == ["kept rule", "another kept rule"]
    assert "DECLINE" not in written.context


def test_failed_batch_call_gives_every_member_an_attempt_row_and_its_own_error(
    tmp_path: Path,
) -> None:
    """A failed batch strands nobody silently: one attempt row and one error each."""
    contents = ["first", "second", "third"]
    db_path, summary_store, contributions, results = _append_and_judge_as_batch(
        tmp_path, contents, manager=_FailingBatchManager(ValueError("LLM unavailable"))
    )

    assert all(isinstance(r, JudgeUnavailable) for r in results)
    # Each caller's error carries ITS OWN contribution id — never a batch-mate's.
    assert [r.contribution_id for r in results] == [c.id for c in contributions]
    assert {r.error_class for r in results} == {"ValueError"}
    assert all("LLM unavailable" in str(r) for r in results)

    with RecordStore(db_path) as rs:
        attempts = rs.list_judgment_attempts(scope_id="g_root")
        judgments = rs.list_judgments(scope_id="g_root")
        states = {s.contribution_id: s for s in rs.list_contribution_states(scope_id="g_root")}

    assert {a.contribution_id for a in attempts} == {c.id for c in contributions}
    assert len(attempts) == 3
    assert {a.outcome for a in attempts} == {JUDGE_FAILED}
    # No verdict was fabricated for anyone, and nothing reached readers.
    assert judgments == []
    assert all(states[c.id].state == "judge_failed" for c in contributions)
    assert summary_store.read("g_root") is None


# -- queue coalescing --------------------------------------------------------


def _spawn_contributions(
    *,
    contents,  # noqa: ANN001
    scope,  # noqa: ANN001
    stratum,  # noqa: ANN001
    fleet,  # noqa: ANN001
    db_path: str,
    summary_store: SummaryStore,
    manager,  # noqa: ANN001
    **kwargs,  # noqa: ANN003
) -> tuple[list[threading.Thread], list]:
    """Start one thread per content, each contributing through the choke point."""
    errors: list = []

    def worker(content: str) -> None:
        try:
            with RecordStore(db_path) as rs:
                run_contribution(
                    scope=scope,
                    stratum=stratum,
                    content=content,
                    proposed_classification="directive",
                    subject=None,
                    supersedes=None,
                    contributor=_contributor(),
                    fleet=fleet,
                    record_store=rs,
                    summary_store=summary_store,
                    scope_manager=manager,
                    summary_max_words=500,
                    **kwargs,
                )
        except Exception as exc:  # noqa: BLE001 — surfaced to the test
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(content,)) for content in contents]
    for thread in threads:
        thread.start()
    return threads, errors


def _wait_for_pending(scope_id: str, count: int, *, timeout: float = 10.0) -> None:
    """Block until *count* contributions are queued for *scope_id*."""
    queue = scope_queue(scope_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if queue.pending_count() >= count:
            return
        time.sleep(0.005)
    raise AssertionError(f"only {queue.pending_count()} of {count} contributions queued")


def test_contributions_arriving_during_a_judgment_are_judged_in_one_batch(
    tmp_path: Path,
) -> None:
    """Coalescing: three callers queued behind an in-flight judgment cost ONE call."""
    scope_id = "g_coalesce"
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    fleet = _fleet(tmp_path, scope_id=scope_id)
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    scope = fleet.get_scope(scope_id)
    stratum = fleet.strata[0]

    gate = threading.Event()
    manager = _ScriptedBatchManager(gate=gate)

    # One contribution takes the drain and blocks inside the judgment.
    first, first_errors = _spawn_contributions(
        contents=["in flight"],
        scope=scope,
        stratum=stratum,
        fleet=fleet,
        db_path=db_path,
        summary_store=summary_store,
        manager=manager,
    )
    while not manager.judge_calls:
        time.sleep(0.005)

    # Three more arrive while it runs — they queue instead of blocking on it.
    # Spawned one at a time, each confirmed queued before the next starts:
    # the assertions below pin ARRIVAL order, and three threads started
    # together arrive in scheduler order, not spawn order.
    queued: list[threading.Thread] = []
    queued_error_lists = []
    for n, content in enumerate(["queued one", "queued two", "queued three"], start=1):
        threads, errors = _spawn_contributions(
            contents=[content],
            scope=scope,
            stratum=stratum,
            fleet=fleet,
            db_path=db_path,
            summary_store=summary_store,
            manager=manager,
        )
        queued.extend(threads)
        queued_error_lists.append(errors)
        _wait_for_pending(scope_id, n)

    gate.set()
    for thread in [*first, *queued]:
        thread.join(timeout=15.0)
    assert first_errors == []
    assert [e for errors in queued_error_lists for e in errors] == []

    # The three queued contributions were judged in ONE call, in arrival order.
    assert len(manager.batch_calls) == 1
    with RecordStore(db_path) as rs:
        arrival = [c.id for c in rs.list_contributions(scope_id=scope_id)]
        judgments = rs.list_judgments(scope_id=scope_id)
    assert manager.batch_calls[0] == arrival[1:]
    assert manager.judge_calls == [[arrival[0]]]

    # Every contribution still has exactly one verdict, and the summary
    # reflects all four — two writes for four contributions.
    assert len(judgments) == 4
    written = summary_store.read(scope_id)
    assert [d.content for d in written.directives] == [
        "in flight",
        "queued one",
        "queued two",
        "queued three",
    ]
    assert written.version == 2


def test_batch_cap_splits_a_long_queue_into_two_calls(tmp_path: Path) -> None:
    """The cap bounds the prompt: cap + 2 queued contributions cost two calls."""
    scope_id = "g_capped"
    cap = 3
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    fleet = _fleet(tmp_path, scope_id=scope_id)
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    scope = fleet.get_scope(scope_id)
    stratum = fleet.strata[0]

    gate = threading.Event()
    manager = _ScriptedBatchManager(gate=gate)

    first, first_errors = _spawn_contributions(
        contents=["in flight"],
        scope=scope,
        stratum=stratum,
        fleet=fleet,
        db_path=db_path,
        summary_store=summary_store,
        manager=manager,
        batch_cap=cap,
    )
    while not manager.judge_calls:
        time.sleep(0.005)

    queued, queued_errors = _spawn_contributions(
        contents=[f"queued {i}" for i in range(cap + 2)],
        scope=scope,
        stratum=stratum,
        fleet=fleet,
        db_path=db_path,
        summary_store=summary_store,
        manager=manager,
        batch_cap=cap,
    )
    _wait_for_pending(scope_id, cap + 2)

    gate.set()
    for thread in [*first, *queued]:
        thread.join(timeout=15.0)
    assert first_errors == []
    assert queued_errors == []

    # Two calls: a full cap, then the remainder. No call exceeds the cap.
    assert [len(call) for call in manager.batch_calls] == [cap, 2]
    with RecordStore(db_path) as rs:
        arrival = [c.id for c in rs.list_contributions(scope_id=scope_id)]
        assert len(rs.list_judgments(scope_id=scope_id)) == cap + 3
    assert manager.batch_calls[0] == arrival[1 : 1 + cap]
    assert manager.batch_calls[1] == arrival[1 + cap :]


def test_a_lone_contribution_still_takes_the_single_judgment_path(tmp_path: Path) -> None:
    """The common case is unchanged: no contention, no batch — one judge() call."""
    manager = _ScriptedBatchManager()
    db_path, summary_store, outcomes = _contribute(tmp_path, ["only one"], manager=manager)

    assert manager.batch_calls == []
    assert manager.judge_calls == [[outcomes[0].contribution_id]]
    with RecordStore(db_path) as rs:
        (judgment,) = rs.list_judgments(scope_id="g_root")
    assert judgment.contribution_id == outcomes[0].contribution_id
    assert judgment.notes == "accepted: only one"
    assert summary_store.read("g_root").version == 1


def test_a_wedged_drain_fails_the_waiter_loudly_and_leaves_it_re_judgeable(
    tmp_path: Path,
) -> None:
    """A bounded wait: the caller gets its own error, not an indefinite hang."""
    scope_id = "g_wedged"
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    fleet = _fleet(tmp_path, scope_id=scope_id)
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    scope = fleet.get_scope(scope_id)
    stratum = fleet.strata[0]

    gate = threading.Event()
    manager = _ScriptedBatchManager(gate=gate)
    blocked, blocked_errors = _spawn_contributions(
        contents=["wedged judgment"],
        scope=scope,
        stratum=stratum,
        fleet=fleet,
        db_path=db_path,
        summary_store=summary_store,
        manager=manager,
    )
    while not manager.judge_calls:
        time.sleep(0.005)

    with pytest.raises(JudgeUnavailable) as exc_info, RecordStore(db_path) as rs:
        run_contribution(
            scope=scope,
            stratum=stratum,
            content="waiting behind the wedge",
            proposed_classification="directive",
            subject=None,
            supersedes=None,
            contributor=_contributor(),
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=manager,
            summary_max_words=500,
            queue_timeout_s=0.05,
        )

    error = exc_info.value
    assert error.error_class == "TimeoutError"

    gate.set()
    for thread in blocked:
        thread.join(timeout=15.0)
    assert blocked_errors == []

    with RecordStore(db_path) as rs:
        states = {s.contribution_id: s for s in rs.list_contribution_states(scope_id=scope_id)}
        attempts = [a for a in rs.list_judgment_attempts(scope_id=scope_id)]
    # The contribution is in the record with an attempt event, but NOT marked
    # judge_failed: no judge run ended here, so it reads as pending and stays
    # re-judgeable (issue #118).
    assert states[error.contribution_id].state == "pending"
    assert states[error.contribution_id].failed_attempts == 1
    assert [a.outcome for a in attempts if a.contribution_id == error.contribution_id] == [None]
    # The abandoned ticket was not judged on nobody's behalf.
    assert error.contribution_id not in {cid for call in manager.batch_calls for cid in call}


def test_judgment_still_runs_under_the_summary_lock(tmp_path: Path) -> None:
    """The drain holds ``scope_lock`` across judge + write (issue #38, ADR 0008 D4).

    Splitting the append off into its own lock is what lets contributions
    queue; the operator correction primitives serialize against judgment on
    the summary lock, and that must not have moved.
    """
    observed: list[tuple[bool, bool]] = []

    class _LockObservingManager(_ScriptedBatchManager):
        def judge(self, *, scope, **kwargs):  # noqa: ANN001, ANN003, ANN201
            observed.append((_scope_lock(scope.id).locked(), scope_append_lock(scope.id).locked()))
            return super().judge(scope=scope, **kwargs)

    _contribute(tmp_path, ["under the lock"], manager=_LockObservingManager())

    summary_locked, append_locked = observed[0]
    assert summary_locked is True
    # ...and the append lock is free, so the next contribution can queue.
    assert append_locked is False


class _AttributedRetiringBatchManager:
    """A batch fake whose retire op names the FIRST accepted member.

    The second member is the last accepted one, so anything the engine
    attributed by position rather than by the op's own ``contribution_id``
    would land on the wrong contribution — permanently, since a Retirement row
    and a withdraw act are record entries.
    """

    def __init__(self, directive_id: str) -> None:
        self._directive_id = directive_id

    def judge_batch(self, *, scope, current_summary, new_contributions, **_kwargs):  # noqa: ANN001, ANN201
        motivating = new_contributions[0]
        ops = [DirectiveOp(op="append", contribution_id=c.id) for c in new_contributions]
        ops.append(DirectiveOp(op="retire", id=self._directive_id, contribution_id=motivating.id))
        return ScopeManagerBatchJudgment(
            verdicts=[
                BatchVerdict(
                    contribution_id=c.id,
                    decision="accept_as_directive",
                    reasoning=f"accepted: {c.content}",
                )
                for c in new_contributions
            ],
            new_summary=_apply_batch_amendment(
                scope=scope,
                current_summary=current_summary,
                contributions={c.id: c for c in new_contributions},
                ops=ops,
                new_context="amended by the batch",
            ),
            directive_ops=ops,
            new_context="amended by the batch",
        )


def test_batch_record_pointers_name_the_op_s_own_contribution(tmp_path: Path) -> None:
    """A retire op's Retirement row and withdrawal trigger follow the op's attribution.

    ADR 0011 D3: every op names the batch member that motivated it, and the
    permanent record entries built from it read that member off the op — never
    the batch's last accepted member, and never any other guess.
    """
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    fleet = _fleet(tmp_path, scope_id="g_root")
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]

    directive = _existing_directive()
    summary_store.write(
        "g_root",
        ScopeSummary(
            scope_id="g_root",
            directives=[directive],
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
            anchors=[f"directive:{directive.id}"],
        )
        contributions = [
            rs.append_contribution(
                scope_id="g_root",
                content=content,
                proposed_classification="directive",
                subject=None,
                supersedes=None,
                contributor=_contributor(),
            )
            for content in ("motivating rule", "unrelated rule")
        ]
        motivating, last_accepted = contributions
        with _scope_lock("g_root"):
            results = _judge_batch_and_record(
                contributions=contributions,
                scope=scope,
                stratum=stratum,
                fleet=fleet,
                record_store=rs,
                summary_store=summary_store,
                scope_manager=_AttributedRetiringBatchManager(directive.id),
                summary_max_words=500,
            )

        assert [r.decision for r in results] == ["accept_as_directive"] * 2

        # The Retirement row explains itself with the reasoning of the member
        # the op named — the FIRST accepted, not the last.
        (retirement,) = rs.list_retirements(scope_id="g_root")
        assert retirement.directive_id == directive.id
        assert retirement.retired_by == "scope-manager"
        assert retirement.reason == "accepted: motivating rule"
        assert retirement.reason != f"accepted: {last_accepted.content}"

        # The mechanically propagated withdrawal is triggered by that same
        # contribution id.
        withdraw_act = next(
            a for a in rs.list_publication_acts(scope_id="g_root") if a.act == "withdraw"
        )
        assert withdraw_act.withdraws == item.id
        assert withdraw_act.trigger == motivating.id
        assert withdraw_act.trigger != last_accepted.id
        assert rs.get_publication_judgment(withdraw_act.id) is None

    written = summary_store.read("g_root")
    assert [d.content for d in written.directives] == ["motivating rule", "unrelated rule"]
    assert written.version == 2  # the seeded write, then ONE write for the batch


class _TwoMemberRemovingBatchManager:
    """A batch fake where each member un-anchors a DIFFERENT published item.

    Member A retires ``directive_a``; member B supersedes ``directive_b`` with
    its own admission. The batch writes ONE summary, so both per-trigger
    propagation calls see the same surviving set — the shape issue #137
    mis-attributed.
    """

    def __init__(self, directive_a: str, directive_b: str) -> None:
        self._directive_a = directive_a
        self._directive_b = directive_b

    def judge_batch(self, *, scope, current_summary, new_contributions, **_kwargs):  # noqa: ANN001, ANN201
        member_a, member_b = new_contributions
        ops = [
            DirectiveOp(op="append", contribution_id=member_a.id),
            DirectiveOp(op="retire", id=self._directive_a, contribution_id=member_a.id),
            DirectiveOp(op="append", contribution_id=member_b.id),
            DirectiveOp(op="supersede", id=self._directive_b, contribution_id=member_b.id),
        ]
        return ScopeManagerBatchJudgment(
            verdicts=[
                BatchVerdict(
                    contribution_id=c.id,
                    decision="accept_as_directive",
                    reasoning=f"accepted: {c.content}",
                )
                for c in new_contributions
            ],
            new_summary=_apply_batch_amendment(
                scope=scope,
                current_summary=current_summary,
                contributions={c.id: c for c in new_contributions},
                ops=ops,
                new_context="amended by the batch",
            ),
            directive_ops=ops,
            new_context="amended by the batch",
        )


def test_batch_withdrawals_are_attributed_per_member(tmp_path: Path) -> None:
    """Two members un-anchor two items in one batch; each withdrawal names its own member.

    Issue #137: the batch writes ONE summary, so every per-trigger propagation
    call sees the same post-write state. Attribution therefore has to come off
    the ids each member removed — otherwise the first member's call sweeps
    everything the whole batch un-anchored and stamps it all with that first
    member's contribution id.
    """
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    fleet = _fleet(tmp_path, scope_id="g_root")
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    scope = fleet.get_scope("g_root")
    stratum = fleet.strata[0]

    directive_a = _existing_directive("c_dirA")
    directive_b = _existing_directive("c_dirB")
    summary_store.write(
        "g_root",
        ScopeSummary(
            scope_id="g_root",
            directives=[directive_a, directive_b],
            context="",
            updated_at="2026-07-10T00:00:00+00:00",
        ),
    )

    with RecordStore(db_path) as rs:
        item_a = _seed_publish_act(
            rs,
            summary_store.summaries_dir,
            "g_root",
            kind="directive",
            content="Published off member A's directive.",
            anchors=[f"directive:{directive_a.id}"],
        )
        item_b = _seed_publish_act(
            rs,
            summary_store.summaries_dir,
            "g_root",
            kind="directive",
            content="Published off member B's directive.",
            anchors=[f"directive:{directive_b.id}"],
        )
        contributions = [
            rs.append_contribution(
                scope_id="g_root",
                content=content,
                proposed_classification="directive",
                subject=None,
                supersedes=None,
                contributor=_contributor(),
            )
            for content in ("member A retires one", "member B supersedes the other")
        ]
        member_a, member_b = contributions
        with _scope_lock("g_root"):
            results = _judge_batch_and_record(
                contributions=contributions,
                scope=scope,
                stratum=stratum,
                fleet=fleet,
                record_store=rs,
                summary_store=summary_store,
                scope_manager=_TwoMemberRemovingBatchManager(directive_a.id, directive_b.id),
                summary_max_words=500,
            )

        assert [r.decision for r in results] == ["accept_as_directive"] * 2

        triggers = {
            a.withdraws: a.trigger
            for a in rs.list_publication_acts(scope_id="g_root")
            if a.act == "withdraw"
        }
        assert triggers == {item_a.id: member_a.id, item_b.id: member_b.id}

    assert read_publication("g_root", summaries_dir=str(summary_store.summaries_dir)) == []
    # Still ONE summary write for the whole batch (the seeded write, then it).
    assert summary_store.read("g_root").version == 2
