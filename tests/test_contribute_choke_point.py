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
from strata.record_store import JUDGE_FAILED, ContributorRef, RecordStore  # noqa: E402
from strata.scope_manager import DirectiveOp, ScopeManagerJudgment  # noqa: E402
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
