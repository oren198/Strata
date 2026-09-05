"""A change wave terminates on a reference cycle (ADR 0014 D4).

This is the ADR's termination case, and the only place the whole
inheritance chain is exercised end to end: an independent input change
mints one id; the refresh it triggers judges in a batch; whatever that
refresh derives INHERITS the id rather than minting a fresh one; and a
scope that has already refreshed for the id is told again but never
judges again.

Chain edges form a tree and would need none of this. Reference edges may
form cycles and need all of it — with fresh ids per derived change the
once-per-id rule would bound nothing, which is exactly what "fresh ids for
derived changes" was rejected for (ADR 0014 D4, Rejected).

Vocabulary follows CONTEXT.md: scope, publication, published item, record,
change event, refresh.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.app import drain_scope  # noqa: E402
from strata.change_events import emit as emit_change_event  # noqa: E402
from strata.fleet_config import FleetConfig  # noqa: E402
from strata.migrator import run_migrations  # noqa: E402
from strata.publication import PublishedItem, _write_publication, read_publication  # noqa: E402
from strata.record_store import ContributorRef, RecordStore  # noqa: E402
from strata.scope_manager import (  # noqa: E402
    BatchVerdict,
    ScopeManagerBatchJudgment,
    ScopeManagerJudgment,
)
from strata.summary_store import ScopeSummary, SummaryStore  # noqa: E402

# A ↔ B: each reads the other's face, and nothing else. No chain edge, so
# the cycle is the only path a wave can travel.
_FLEET_YAML = """
strata:
  - id: L0
    name: function
    ordinal: 0
scopes:
  - id: g_cycA
    name: A
    stratum_id: L0
  - id: g_cycB
    name: B
    stratum_id: L0
edges:
  - from: g_cycA
    to: g_cycB
    kind: reference
  - from: g_cycB
    to: g_cycA
    kind: reference
"""


@pytest.fixture
def fleet(tmp_path: Path) -> FleetConfig:
    path = tmp_path / "fleet.yaml"
    path.write_text(textwrap.dedent(_FLEET_YAML), encoding="utf-8")
    return FleetConfig.load(path)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "record.db")
    run_migrations(path)
    return path


@pytest.fixture
def summary_store(tmp_path: Path) -> SummaryStore:
    store = SummaryStore(str(tmp_path / "summaries"))
    for scope_id in ("g_cycA", "g_cycB"):
        store.write(
            scope_id,
            ScopeSummary(
                scope_id=scope_id,
                directives=[],
                context=f"What {scope_id} believed before the wave.",
                updated_at="2026-09-05T00:00:00+00:00",
            ),
        )
    return store


def _seed_publication(
    record_store: RecordStore, summary_store: SummaryStore, scope_id: str
) -> list[PublishedItem]:
    """Give *scope_id* four published items, ids and all, as real publish acts.

    Four because a judge that always withdraws two still has somewhere to
    go for a SECOND refresh: broken inheritance then shows up as an extra
    judge call rather than as an infinite loop nobody can debug.
    """
    items = []
    for n in (1, 2, 3, 4):
        act = record_store.append_publication_act(
            scope_id=scope_id,
            act="publish",
            kind="context",
            content=f"{scope_id} asserts claim {n}.",
            subject="claims",
            anchors=["subject:claims"],
            withdraws=None,
            trigger=None,
            proposer=ContributorRef(
                scope_id=scope_id,
                skill="strata-developer",
                session_id="seed",
                ts="2026-09-05T00:00:00+00:00",
            ),
        )
        items.append(
            PublishedItem(
                id=act.id,
                kind="context",
                content=f"{scope_id} asserts claim {n}.",
                subject="claims",
                anchors=["subject:claims"],
                published_at="2026-09-05T00:00:00+00:00",
            )
        )
    _write_publication(scope_id, items, summaries_dir=str(summary_store.summaries_dir))
    return items


class _AlwaysWithdrawsJudge:
    """Accepts, and withdraws two still-published items every time it runs.

    The worst case D4 has to survive: a judge whose every refresh changes
    this scope's face, so every refresh derives changes the other scope
    hears about. Termination cannot come from the judge going quiet — only
    from the change id.

    Two withdrawals rather than one so the next scope's drain is a COALESCED
    batch, which is the shape a refresh-derived emission actually takes
    (Phase A finding 2): a single notice takes the single-judgment path, and
    a wave that only ever travels that path never exercises the plural id.
    """

    def __init__(self, summaries_dir: str) -> None:
        self.summaries_dir = summaries_dir
        self.calls: list[dict] = []

    def _to_withdraw(self, scope_id: str) -> list[str]:
        return [i.id for i in read_publication(scope_id, summaries_dir=self.summaries_dir)][:2]

    def _summary(self, scope_id: str) -> ScopeSummary:
        return ScopeSummary(
            scope_id=scope_id,
            directives=[],
            context=f"{scope_id} has reconciled with {len(self.calls)} refresh(es).",
            updated_at="2026-09-05T01:00:00+00:00",
        )

    def judge(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        scope = kwargs["scope"]
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="reacted to the input change",
            new_summary=self._summary(scope.id),
            new_context="reconciled",
            withdraw_published=self._to_withdraw(scope.id),
            change_id=kwargs.get("change_id"),
            hop=kwargs.get("hop", 0),
        )

    def judge_batch(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        scope = kwargs["scope"]
        return ScopeManagerBatchJudgment(
            verdicts=[
                BatchVerdict(
                    contribution_id=c.id,
                    decision="accept_as_context",
                    reasoning="reacted to the input change",
                )
                for c in kwargs["new_contributions"]
            ],
            new_summary=self._summary(scope.id),
            new_context="reconciled",
            withdraw_published=self._to_withdraw(scope.id),
            change_ids=list(kwargs.get("change_ids") or []),
            hop=kwargs.get("hop", 0),
        )


def _refresh_calls(judge: _AlwaysWithdrawsJudge, scope_id: str) -> list[dict]:
    return [c for c in judge.calls if c["scope"].id == scope_id]


def test_one_withdrawal_on_a_reference_cycle_refreshes_each_scope_once(
    fleet: FleetConfig, db_path: str, summary_store: SummaryStore
) -> None:
    """ADR 0014 D4's termination guarantee, on the topology that needs it.

    A withdraws an item. B refreshes for that change id and reacts by
    withdrawing one of its own; A hears about B's reaction under the SAME
    id, refreshes once, and reacts in turn — and B, which has already
    refreshed for this id, is told and does not judge again. The wave stops
    at one refresh per scope per change id, with no scope left holding
    unprocessed work.
    """
    judge = _AlwaysWithdrawsJudge(str(summary_store.summaries_dir))

    with RecordStore(db_path) as record_store:
        seeded = {
            scope_id: _seed_publication(record_store, summary_store, scope_id)
            for scope_id in ("g_cycA", "g_cycB")
        }
        # A's face has already lost its first item — the withdrawal this
        # wave is about. Removed from the artifact first so the judge below
        # cannot pick it again, which would let the dedup rule (same item,
        # same wave, announced once) do work the termination rule is
        # supposed to do.
        withdrawn = seeded["g_cycA"][0]
        _write_publication(
            "g_cycA",
            seeded["g_cycA"][1:],
            summaries_dir=str(summary_store.summaries_dir),
        )

        # The independent input change: A's face lost an item. Emitted
        # directly rather than through a withdraw act, so the test is about
        # inheritance and nothing else.
        emit_change_event(
            fleet=fleet,
            record_store=record_store,
            item=withdrawn.id,
            kind="withdrawn",
            source_scope_id="g_cycA",
            before=withdrawn.content,
        )
        # Precondition: the wave really did reach the other side of the cycle.
        # The id is read off the notice rather than the emitter's return, so
        # this test says nothing about a call shape — only about what the
        # wave does.
        pending = record_store.list_change_events(scope_id="g_cycB", unprocessed_only=True)
        assert len(pending) == 1
        change_id = pending[0].change_id

        # Drive the wave to a standstill, alternating sides the way two
        # independent MCP reads would. The cap is a test-harness backstop,
        # never the mechanism: if it is what stops the loop, the wave did
        # not terminate.
        drains = 0
        for _ in range(6):
            processed = 0
            for scope_id in ("g_cycA", "g_cycB"):
                outcome = drain_scope(
                    scope_id,
                    fleet=fleet,
                    record_store=record_store,
                    summary_store=summary_store,
                    scope_manager=judge,
                    summary_max_words=500,
                )
                processed += outcome.events_processed
            drains += 1
            if processed == 0:
                break
        else:  # pragma: no cover — only reached when the wave never settles
            pytest.fail("the wave never settled: the change id bounded nothing")

        # ONE refresh per scope for this change id — the whole guarantee.
        assert len(_refresh_calls(judge, "g_cycB")) == 1
        assert len(_refresh_calls(judge, "g_cycA")) == 1
        assert drains <= 3

        # Every notice carries the ORIGINAL id: nothing derived minted a
        # fresh one (ADR 0014 D4, Rejected: "fresh ids for derived changes").
        for scope_id in ("g_cycA", "g_cycB"):
            events = record_store.list_change_events(scope_id=scope_id)
            assert events, f"{scope_id} was never told anything"
            assert {e.change_id for e in events} == {change_id}
            # Nothing is left owed: a drain that judged nothing still
            # consumed what it was shown.
            assert record_store.list_change_events(scope_id=scope_id, unprocessed_only=True) == []

        # D4 bounds the REFRESH, never the NOTICE: B was told a second time
        # and the row was stamped processed at birth (ADR 0014 D5).
        b_events = record_store.list_change_events(scope_id="g_cycB")
        assert len(b_events) == 3
        assert all(e.processed_at is not None for e in b_events)

        # The hop count travels with the wave, so D4's backstop budget
        # covers the cycle instead of restarting at zero on every derivation.
        assert [e.hop for e in b_events] == [0, 2, 2]
        a_events = record_store.list_change_events(scope_id="g_cycA")
        assert [e.hop for e in a_events] == [1, 1]
