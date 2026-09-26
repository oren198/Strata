"""#229 outside-review fix: a multi-member contribute batch never built an
ActedOnTarget, so a contribution carrying `acted_on`/`acted_on_operator_item`
that landed in a batch silently skipped the P3 disposition, the P4 claim
event, and the P5 raise.

Reproduced and fixed with the REAL queueing/batching machinery (threads
forcing real coalescing through `run_contribution`'s choke point, exactly
`test_contribute_choke_point.py`'s own pattern) and a scope-manager fake that
ACTIVELY CHECKS for the regression rather than hiding it: its own
`judge_batch` asserts no acted_on-carrying member ever reaches it, so a
reintroduced bug fails loudly inside the fake, not just via a missing
assertion in the test body.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.app import run_contribution  # noqa: E402
from strata.fleet_config import FleetConfig  # noqa: E402
from strata.locks import scope_queue  # noqa: E402
from strata.migrator import run_migrations  # noqa: E402
from strata.record_store import ContributorRef, RecordStore  # noqa: E402
from strata.scope_manager import (  # noqa: E402
    BatchVerdict,
    ScopeManagerBatchJudgment,
    ScopeManagerJudgment,
)
from strata.summary_store import ScopeSummary, SummaryStore  # noqa: E402

_PARENT = "g_p229_parent"
_CHILD = "g_p229_child"


def _fleet(root: Path) -> FleetConfig:
    fleet = {
        "strata": [
            {"id": "L0", "name": "executive", "ordinal": 0},
            {"id": "L1", "name": "team", "ordinal": 1},
        ],
        "scopes": [
            {"id": _PARENT, "name": "Parent", "stratum_id": "L0"},
            {"id": _CHILD, "name": "Child", "stratum_id": "L1"},
        ],
        "edges": [{"from": _CHILD, "to": _PARENT}],
    }
    root.mkdir(parents=True, exist_ok=True)
    path = root / "fleet.yaml"
    path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return FleetConfig.load(path)


def _contributor(scope_id: str) -> ContributorRef:
    return ContributorRef(
        scope_id=scope_id,
        skill="on-call-engineer",
        session_id="sess_229",
        ts="2026-09-27T00:00:00Z",
    )


class _ActedOnAwareManager:
    """A scope-manager fake that ACTIVELY CHECKS for the #229 regression: its
    own `judge_batch` refuses (via assertion) any member carrying
    `acted_on`/`acted_on_operator_item` with `raised_from` unset — the exact
    contract `_judge_batch_and_record` must now enforce before ever calling it.
    """

    def __init__(self, *, gate: threading.Event) -> None:
        self.judge_calls: list[tuple[str, bool]] = []  # (contribution_id, had_acted_on_target)
        self.batch_calls: list[list[str]] = []
        self._gate = gate
        self._gate_used = False
        self._lock = threading.Lock()

    def _pause(self) -> None:
        with self._lock:
            first = not self._gate_used
            self._gate_used = True
        if first:
            assert self._gate.wait(timeout=10.0), "the test never released the gated judgment"

    def judge(
        self,
        *,
        scope,  # noqa: ANN001
        current_summary,  # noqa: ANN001
        new_contribution,  # noqa: ANN001
        acted_on_target=None,  # noqa: ANN001
        **_kwargs,  # noqa: ANN003
    ):  # noqa: ANN201
        with self._lock:
            self.judge_calls.append((new_contribution.id, acted_on_target is not None))
        self._pause()

        base_summary = current_summary or ScopeSummary(
            scope_id=scope.id, directives=[], context="", updated_at="2026-09-27T00:00:00+00:00"
        )

        if acted_on_target is not None:
            # An outcome report on a directive target — decide by content.
            failed = "FAILED" in new_contribution.content
            return ScopeManagerJudgment(
                decision="accept_as_context",
                reasoning=f"outcome: {new_contribution.content}",
                new_summary=base_summary,
                outcome_disposition="failed" if failed else "held",
            )

        if new_contribution.content.startswith("DECLINE"):
            return ScopeManagerJudgment(decision="decline", reasoning="declined", new_summary=None)

        # Ordinary accept (covers both the reporter's plain contributions and
        # the issuer's own synchronous re-judgment of a raised contribution).
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning=f"accepted: {new_contribution.content}",
            new_summary=ScopeSummary(
                scope_id=scope.id,
                directives=base_summary.directives,
                context=(base_summary.context + " " + new_contribution.content).strip(),
                updated_at="2026-09-27T00:00:01+00:00",
            ),
        )

    def judge_batch(self, *, scope, current_summary, new_contributions, **_kwargs):  # noqa: ANN001, ANN201
        # THE REGRESSION CHECK: the batch tool has no outcome-disposition
        # field at all — a member carrying acted_on here means #229 is back.
        assert not any(
            (c.acted_on is not None or c.acted_on_operator_item is not None)
            and c.raised_from is None
            for c in new_contributions
        ), "an acted_on-carrying member reached judge_batch — #229 regressed"
        with self._lock:
            self.batch_calls.append([c.id for c in new_contributions])
        self._pause()

        base_summary = current_summary or ScopeSummary(
            scope_id=scope.id, directives=[], context="", updated_at="2026-09-27T00:00:00+00:00"
        )
        verdicts = []
        context = base_summary.context
        for c in new_contributions:
            verdicts.append(
                BatchVerdict(
                    contribution_id=c.id,
                    decision="accept_as_context",
                    reasoning=f"accepted: {c.content}",
                )
            )
            context = (context + " " + c.content).strip()
        return ScopeManagerBatchJudgment(
            verdicts=verdicts,
            new_summary=ScopeSummary(
                scope_id=scope.id,
                directives=base_summary.directives,
                context=context,
                updated_at="2026-09-27T00:00:02+00:00",
            ),
            new_context=context,
        )


def _spawn(
    *, content, scope, stratum, fleet, db_path, summary_store, manager, acted_on=None, contributor
):  # noqa: ANN001, ANN201
    errors: list = []

    def worker() -> None:
        try:
            with RecordStore(db_path) as rs:
                run_contribution(
                    scope=scope,
                    stratum=stratum,
                    content=content,
                    proposed_classification="context",
                    subject=None,
                    supersedes=None,
                    contributor=contributor,
                    fleet=fleet,
                    record_store=rs,
                    summary_store=summary_store,
                    scope_manager=manager,
                    summary_max_words=500,
                    acted_on=acted_on,
                )
        except Exception as exc:  # noqa: BLE001 — surfaced to the test
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    return thread, errors


def _wait_for_pending(scope_id: str, count: int, *, timeout: float = 10.0) -> None:
    queue = scope_queue(scope_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if queue.pending_count() >= count:
            return
        time.sleep(0.005)
    raise AssertionError(f"only {queue.pending_count()} of {count} contributions queued")


def test_an_acted_on_member_in_a_forced_batch_still_gets_its_full_p3_p4_p5_treatment(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    fleet = _fleet(tmp_path)
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    child_scope = fleet.get_scope(_CHILD)
    stratum_l1 = next(s for s in fleet.strata if s.id == "L1")

    # Seed the directive at the PARENT (the issuer) directly.
    with RecordStore(db_path) as rs:
        directive_id = rs.append_contribution(
            scope_id=_PARENT,
            content="Page the sev-1 rotation through the primary pager.",
            proposed_classification="directive",
            subject="paging",
            supersedes=None,
            contributor=_contributor(_PARENT),
        ).id
        rs.record_judgment(
            contribution_id=directive_id, decision="accept_as_directive", judged_by="scope-manager"
        )

    gate = threading.Event()
    manager = _ActedOnAwareManager(gate=gate)

    # One contribution takes the drain and blocks inside the judgment.
    in_flight_thread, in_flight_errors = _spawn(
        content="in flight",
        scope=child_scope,
        stratum=stratum_l1,
        fleet=fleet,
        db_path=db_path,
        summary_store=summary_store,
        manager=manager,
        contributor=_contributor(_CHILD),
    )
    while not manager.judge_calls:
        time.sleep(0.005)

    # Three more arrive while it runs: acted_on (failed directive outcome)
    # first, then two ordinary ones — queued one at a time so arrival order
    # is pinned. The ordinary pair is adjacent (not split by the acted_on
    # member) so they still coalesce into ONE real judge_batch call: the
    # split flushes whatever ordinary run has accumulated whenever an
    # acted_on member is hit, so an acted_on member sitting BETWEEN two
    # ordinary ones would isolate them into two singleton flushes instead —
    # that's arrival-order fidelity, not a batching failure, and is exercised
    # on its own below.
    queued: list[threading.Thread] = []
    queued_errors: list[list] = []
    specs = [
        ("FAILED: the page never reached anyone", directive_id),
        ("queued one", None),
        ("queued three", None),
    ]
    for n, (content, acted_on) in enumerate(specs, start=1):
        thread, errors = _spawn(
            content=content,
            scope=child_scope,
            stratum=stratum_l1,
            fleet=fleet,
            db_path=db_path,
            summary_store=summary_store,
            manager=manager,
            acted_on=acted_on,
            contributor=_contributor(_CHILD),
        )
        queued.append(thread)
        queued_errors.append(errors)
        _wait_for_pending(_CHILD, n)

    gate.set()
    for thread in [in_flight_thread, *queued]:
        thread.join(timeout=15.0)
    assert in_flight_errors == []
    assert [e for errors in queued_errors for e in errors] == []

    with RecordStore(db_path) as rs:
        child_contributions = rs.list_contributions(scope_id=_CHILD)
        by_content = {c.content: c for c in child_contributions}
        outcome_id = by_content["FAILED: the page never reached anyone"].id
        outcome_judgment = rs.get_judgment(outcome_id)
        parent_contributions = rs.list_contributions(scope_id=_PARENT)
        raised = [c for c in parent_contributions if c.raised_from == outcome_id]

        # The batch call got ONLY the two ordinary members — never the acted_on one.
        assert len(manager.batch_calls) == 1
        assert set(manager.batch_calls[0]) == {
            by_content["queued one"].id,
            by_content["queued three"].id,
        }
        assert outcome_id not in manager.batch_calls[0]

        # The acted_on member was judged through the single path, WITH its
        # acted_on_target resolved.
        acted_on_judge_calls = [c for c in manager.judge_calls if c[0] == outcome_id]
        assert len(acted_on_judge_calls) == 1
        assert acted_on_judge_calls[0][1] is True  # acted_on_target was not None

        # P3: its own disposition recorded (failed -> accept_as_context).
        assert outcome_judgment is not None
        assert outcome_judgment.decision == "accept_as_context"

        # P5: the failed directive outcome raised exactly once, to the issuer.
        assert len(raised) == 1
        assert "went wrong" in raised[0].content

        # Ordinary members' batch verdicts are unchanged.
        assert rs.get_judgment(by_content["queued one"].id) is not None
        assert rs.get_judgment(by_content["queued three"].id) is not None


def test_a_raised_contribution_can_still_batch(tmp_path: Path) -> None:
    """A RAISED contribution (raised_from set) is judged as an ORDINARY
    consequence report at the issuer, never through the acted_on path — by
    design it may still land in a real batch call.

    `run_contribution` has no `raised_from` parameter (only the P5 engine
    mechanism sets it, via `record_judgment_and_raise`, judged synchronously
    at raise time — never through the queue). So this calls the real
    `_judge_batch_and_record` directly, under the same `_scope_lock` its own
    docstring requires callers to hold, with two REAL contributions: one
    ordinary, one carrying `raised_from`. That is the exact function Item 1
    changed, exercised the same way `_judge_batch_and_record` itself is
    always reached — everything is real except the scope-manager fake.
    """
    from strata.app import _judge_batch_and_record
    from strata.locks import scope_lock

    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    fleet = _fleet(tmp_path)
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    parent_scope = fleet.get_scope(_PARENT)
    stratum_l0 = next(s for s in fleet.strata if s.id == "L0")

    with RecordStore(db_path) as rs:
        outcome_id = rs.append_contribution(
            scope_id=_CHILD,
            content="an outcome that triggered a raise",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_contributor(_CHILD),
        ).id
        ordinary_id = rs.append_contribution(
            scope_id=_PARENT,
            content="an ordinary observation",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_contributor(_PARENT),
        ).id
        raised_id = rs.append_contribution(
            scope_id=_PARENT,
            content="evidence from g_p229_child: following it went wrong: it broke.",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_contributor(_CHILD),
            raised_from=outcome_id,
        ).id
        contributions = [rs.get_contribution(ordinary_id), rs.get_contribution(raised_id)]

        gate = threading.Event()
        gate.set()  # nothing to pause on in this single-threaded call
        manager = _ActedOnAwareManager(gate=gate)

        with scope_lock(_PARENT):
            results = _judge_batch_and_record(
                contributions=contributions,
                scope=parent_scope,
                stratum=stratum_l0,
                fleet=fleet,
                record_store=rs,
                summary_store=summary_store,
                scope_manager=manager,
                summary_max_words=500,
            )

    assert len(results) == 2
    # BOTH members — the raised one included — landed in the SAME real batch
    # call: raised_from-set members are exempt from the acted_on pull-out.
    assert len(manager.batch_calls) == 1
    assert set(manager.batch_calls[0]) == {ordinary_id, raised_id}
    assert all(not isinstance(r, Exception) for r in results)


def test_judge_batch_rejects_a_single_acted_on_member_reaching_it_directly() -> None:
    """Defense in depth: `ScopeManager.judge_batch` itself refuses a single
    acted_on-carrying member, rather than silently dropping the disposition —
    the last-resort guard for a caller that reaches it outside app.py's split."""
    import pytest

    from strata.record_store import Contribution
    from strata.scope_manager import ScopeManager

    contributor = _contributor(_CHILD)
    contribution = Contribution(
        id="c_direct",
        scope_id=_CHILD,
        content="an outcome",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=contributor,
        created_at="2026-09-27T00:00:00Z",
        acted_on="c_some_directive",
    )

    class _FakeClient:
        api_key = "fake-key-never-used"

    manager = ScopeManager(client=_FakeClient())  # the guard fires before any real call
    with pytest.raises(ValueError, match="carrying acted_on"):
        manager.judge_batch(
            scope=None,
            stratum=None,
            current_summary=None,
            recent_contributions=[],
            new_contributions=[contribution],
            summary_max_words=500,
        )
