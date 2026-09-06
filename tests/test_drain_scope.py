"""Draining a scope's pending input changes (ADR 0014 D6, implementation pin 1).

A change event is a ``subject="manager-refresh"`` contribution plus its
``change_events`` row (ADR 0014 D5). Draining a scope judges every unprocessed
one of them in a SINGLE batch — coalescing IS batch judgment (pin 1), not a
mechanism of its own — writes one summary, and marks every event processed
whatever the verdict.

The events and their notices are written here directly through the record
store: Phase B owns emission and the affected set, so these tests fabricate
what a writer would have emitted rather than waiting for one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.app import DrainFailed, drain_scope
from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.record_store import JUDGE_FAILED, Contribution, ContributorRef, RecordStore
from strata.scope_manager import BatchVerdict, ScopeManagerBatchJudgment, ScopeManagerJudgment
from strata.summary_store import ScopeSummary, SummaryStore

EXISTING_CONTEXT = "What this scope believed before anything changed."


def _fleet(root: Path) -> FleetConfig:
    fleet = {
        "strata": [{"id": "L0", "name": "executive", "ordinal": 0}],
        "scopes": [{"id": "g_drain", "name": "Root", "stratum_id": "L0"}],
        "edges": [],
    }
    root.mkdir(parents=True, exist_ok=True)
    path = root / "fleet.yaml"
    path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return FleetConfig.load(path)


def _setup(tmp_path: Path) -> tuple[str, FleetConfig, SummaryStore]:
    fleet = _fleet(tmp_path)
    db_path = str(tmp_path / "test.db")
    run_migrations(db_path)
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    summary_store.write(
        "g_drain",
        ScopeSummary(
            scope_id="g_drain",
            directives=[],
            context=EXISTING_CONTEXT,
            updated_at="2026-09-05T00:00:00+00:00",
        ),
    )
    return db_path, fleet, summary_store


def _emit(
    record_store: RecordStore,
    *,
    change_id: str,
    item_id: str,
    kind: str = "withdrawn",
    scope_id: str = "g_drain",
) -> Contribution:
    """Write what a Phase B emitter writes: the notice, then its event row."""
    notice = record_store.append_contribution(
        scope_id=scope_id,
        content=f"[Input change {change_id}: item {item_id} was {kind}.]",
        proposed_classification="context",
        subject="manager-refresh",
        supersedes=None,
        contributor=ContributorRef(
            scope_id=scope_id,
            skill="scope-manager",
            session_id="refresh",
            ts="2026-09-05T00:00:00+00:00",
        ),
    )
    record_store.append_change_event(
        change_id=change_id,
        contribution_id=notice.id,
        scope_id=scope_id,
        item_id=item_id,
        kind=kind,
        before="Ship behind a flag.",
        after=None,
    )
    return notice


class _ScriptedJudge:
    """Accepts everything as context, recording the calls it was given."""

    def __init__(self, *, decision: str = "accept_as_context", new_context: str | None = None):
        self.decision = decision
        self.new_context = new_context
        self.judge_calls: list[dict] = []
        self.batch_calls: list[dict] = []

    def _summary(self, scope) -> ScopeSummary | None:  # noqa: ANN001
        if self.decision == "decline" or self.new_context is None:
            return None
        return ScopeSummary(
            scope_id=scope.id,
            directives=[],
            context=self.new_context,
            updated_at="2026-09-05T01:00:00+00:00",
        )

    def judge(self, **kwargs):  # noqa: ANN003, ANN201
        self.judge_calls.append(kwargs)
        return ScopeManagerJudgment(
            decision=self.decision,
            reasoning="judged the refreshed inputs",
            new_summary=self._summary(kwargs["scope"]),
            new_context=self.new_context if self.decision != "decline" else None,
            change_id=kwargs.get("change_id"),
        )

    def judge_batch(self, **kwargs):  # noqa: ANN003, ANN201
        self.batch_calls.append(kwargs)
        return ScopeManagerBatchJudgment(
            verdicts=[
                BatchVerdict(
                    contribution_id=c.id,
                    decision=self.decision,
                    reasoning="judged the refreshed inputs",
                )
                for c in kwargs["new_contributions"]
            ],
            new_summary=self._summary(kwargs["scope"]),
            new_context=self.new_context if self.decision != "decline" else None,
            change_ids=list(kwargs.get("change_ids") or []),
        )


class _FailingJudge:
    def judge(self, **_kwargs):  # noqa: ANN003, ANN201
        raise RuntimeError("scope-manager is down")

    def judge_batch(self, **_kwargs):  # noqa: ANN003, ANN201
        raise RuntimeError("scope-manager is down")


def _drain(db_path: str, fleet: FleetConfig, summary_store: SummaryStore, judge):  # noqa: ANN001, ANN201
    with RecordStore(db_path) as rs:
        return drain_scope(
            "g_drain",
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
            scope_manager=judge,
            summary_max_words=500,
        )


# ---------------------------------------------------------------------------
# Coalescing IS batch judgment (pin 1)
# ---------------------------------------------------------------------------


def test_a_batch_of_n_events_is_one_judge_call_and_n_processed_rows(tmp_path: Path) -> None:
    db_path, fleet, summary_store = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        for n in range(3):
            _emit(rs, change_id=f"chg_{n}", item_id=f"p_{n}")

    judge = _ScriptedJudge(new_context="Reconciled with the current inputs.")
    outcome = _drain(db_path, fleet, summary_store, judge)

    assert len(judge.batch_calls) == 1
    assert judge.judge_calls == []
    assert outcome.events_processed == 3

    with RecordStore(db_path) as rs:
        assert rs.list_change_events(scope_id="g_drain", unprocessed_only=True) == []
        assert len(rs.list_change_events(scope_id="g_drain")) == 3
        assert len(rs.list_judgments(scope_id="g_drain")) == 3


def test_the_drain_judges_in_input_change_refresh_mode(tmp_path: Path) -> None:
    """ADR 0014 D2: admitting ops are allowed on this path, so the mode says so."""
    db_path, fleet, summary_store = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        _emit(rs, change_id="chg_a", item_id="p_1")
        _emit(rs, change_id="chg_b", item_id="p_2")

    judge = _ScriptedJudge(new_context="Reconciled.")
    _drain(db_path, fleet, summary_store, judge)

    call = judge.batch_calls[0]
    assert call["mode"] == "input_change_refresh"
    assert list(call["change_ids"]) == ["chg_a", "chg_b"]
    assert [e.item_id for e in call["input_changes"]] == ["p_1", "p_2"]


def test_one_pending_event_still_drains(tmp_path: Path) -> None:
    """A batch of one takes the single-contribution path, exactly as ADR 0011 D3 says."""
    db_path, fleet, summary_store = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        _emit(rs, change_id="chg_a", item_id="p_1")

    judge = _ScriptedJudge(new_context="Reconciled.")
    outcome = _drain(db_path, fleet, summary_store, judge)

    assert outcome.events_processed == 1
    assert judge.judge_calls[0]["mode"] == "input_change_refresh"
    assert judge.judge_calls[0]["change_id"] == "chg_a"


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_draining_a_scope_with_nothing_pending_makes_no_judge_call(tmp_path: Path) -> None:
    db_path, fleet, summary_store = _setup(tmp_path)

    judge = _ScriptedJudge(new_context="Reconciled.")
    outcome = _drain(db_path, fleet, summary_store, judge)

    assert outcome.events_processed == 0
    assert outcome.judged is False
    assert judge.batch_calls == [] and judge.judge_calls == []


def test_a_second_drain_is_a_no_op(tmp_path: Path) -> None:
    db_path, fleet, summary_store = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        _emit(rs, change_id="chg_a", item_id="p_1")
        _emit(rs, change_id="chg_b", item_id="p_2")

    judge = _ScriptedJudge(new_context="Reconciled.")
    _drain(db_path, fleet, summary_store, judge)
    second = _drain(db_path, fleet, summary_store, judge)

    assert len(judge.batch_calls) == 1
    assert second.events_processed == 0
    assert second.judged is False


# ---------------------------------------------------------------------------
# Judge outage (pin 5): the events stay, so the refresh is still owed
# ---------------------------------------------------------------------------


def test_a_judge_failure_leaves_the_events_unprocessed(tmp_path: Path) -> None:
    db_path, fleet, summary_store = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        _emit(rs, change_id="chg_a", item_id="p_1")
        _emit(rs, change_id="chg_b", item_id="p_2")

    with pytest.raises(DrainFailed) as exc_info:
        _drain(db_path, fleet, summary_store, _FailingJudge())

    assert exc_info.value.scope_id == "g_drain"

    with RecordStore(db_path) as rs:
        assert len(rs.list_change_events(scope_id="g_drain", unprocessed_only=True)) == 2
        assert rs.list_judgments(scope_id="g_drain") == []
        states = rs.list_contribution_states(scope_id="g_drain")
        assert [s.state for s in states] == ["judge_failed", "judge_failed"]
        attempts = rs.list_judgment_attempts(scope_id="g_drain")
        assert attempts and all(a.outcome == JUDGE_FAILED for a in attempts)


def test_a_failed_drain_can_be_retried(tmp_path: Path) -> None:
    db_path, fleet, summary_store = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        _emit(rs, change_id="chg_a", item_id="p_1")
        _emit(rs, change_id="chg_b", item_id="p_2")

    with pytest.raises(DrainFailed):
        _drain(db_path, fleet, summary_store, _FailingJudge())

    outcome = _drain(db_path, fleet, summary_store, _ScriptedJudge(new_context="Reconciled."))
    assert outcome.events_processed == 2


# ---------------------------------------------------------------------------
# The engine never edits the scope's memory (ADR 0014 D2) — with the
# vacuous-pass guard (pin 10): assert the judgment row exists FIRST.
# ---------------------------------------------------------------------------


def test_a_declining_refresh_leaves_the_context_untouched(tmp_path: Path) -> None:
    db_path, fleet, summary_store = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        _emit(rs, change_id="chg_a", item_id="p_1")
        _emit(rs, change_id="chg_b", item_id="p_2")

    outcome = _drain(db_path, fleet, summary_store, _ScriptedJudge(decision="decline"))

    # Guard first: a refresh that never ran would leave the context untouched
    # too, and prove nothing.
    with RecordStore(db_path) as rs:
        judgments = rs.list_judgments(scope_id="g_drain")
        assert len(judgments) == 2
        assert all(j.decision == "decline" for j in judgments)

    assert outcome.events_processed == 2
    assert summary_store.read("g_drain").context == EXISTING_CONTEXT


def test_an_event_is_processed_whatever_the_verdict(tmp_path: Path) -> None:
    """ADR 0014 D5: consumed once a refresh has processed it, decline included."""
    db_path, fleet, summary_store = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        _emit(rs, change_id="chg_a", item_id="p_1")
        _emit(rs, change_id="chg_b", item_id="p_2")

    _drain(db_path, fleet, summary_store, _ScriptedJudge(decision="decline"))

    with RecordStore(db_path) as rs:
        assert rs.list_change_events(scope_id="g_drain", unprocessed_only=True) == []


def test_an_already_judged_notice_is_marked_processed_without_re_judging(
    tmp_path: Path,
) -> None:
    """The crash-in-between case: a verdict exists, only the marking did not land.

    Idempotence comes from the no-op-if-judged rule (pin 1), so an event whose
    notice already carries a verdict is consumed, never judged a second time.
    """
    db_path, fleet, summary_store = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        notice = _emit(rs, change_id="chg_a", item_id="p_1")
        rs.record_judgment(
            contribution_id=notice.id,
            decision="accept_as_context",
            judged_by="scope-manager",
            notes="judged before the crash",
        )

    judge = _ScriptedJudge(new_context="Reconciled.")
    outcome = _drain(db_path, fleet, summary_store, judge)

    assert judge.judge_calls == [] and judge.batch_calls == []
    assert outcome.events_processed == 1
    with RecordStore(db_path) as rs:
        assert rs.list_change_events(scope_id="g_drain", unprocessed_only=True) == []


def test_the_drained_judgment_carries_the_next_hop(tmp_path: Path) -> None:
    """ADR 0014 D4's hop budget only bounds a wave if the hop count travels.

    A refresh-derived emission that restarted at hop 0 would leave the backstop
    covering nothing — the reference cycle it exists for is exactly where hops
    accumulate. The judgment carries max(drained hop) + 1, so an emitter
    writing derived events reads the next hop off the judgment rather than
    guessing it.
    """
    db_path, fleet, summary_store = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        first = _emit(rs, change_id="chg_a", item_id="p_1")
        second = _emit(rs, change_id="chg_b", item_id="p_2")
        rs.append_change_event(
            change_id="chg_c",
            contribution_id=first.id,
            scope_id="g_drain",
            item_id="p_3",
            kind="amended",
            hop=2,
        )
        assert second is not None

    judge = _ScriptedJudge(new_context="Reconciled.")
    _drain(db_path, fleet, summary_store, judge)

    assert judge.batch_calls[0]["hop"] == 3


def test_a_first_hop_wave_leaves_the_judgment_at_hop_one(tmp_path: Path) -> None:
    db_path, fleet, summary_store = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        _emit(rs, change_id="chg_a", item_id="p_1")

    judge = _ScriptedJudge(new_context="Reconciled.")
    _drain(db_path, fleet, summary_store, judge)

    assert judge.judge_calls[0]["hop"] == 1
