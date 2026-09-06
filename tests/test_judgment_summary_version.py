"""A judgment row names the summary version it produced (strata-evals G3).

ADR 0011 D3 made a coalesced batch ONE amendment: one summary write, one
``version`` bump, however many verdict rows the batch recorded. MEASURES.md
Decision 4 ("each summary rewrite ties to exactly one judgment") was checked
by counting — ``version == accepted judgments`` — which a batch breaks by
construction, and ADR 0014's drain batches every refresh. So the tie is now
recorded rather than inferred: every accepted judgment row carries the
version its amendment wrote, a batch's rows share one, and a decline carries
none.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.app import run_contribution  # noqa: E402
from strata.fleet_config import FleetConfig  # noqa: E402
from strata.migrator import run_migrations  # noqa: E402
from strata.record_store import ContributorRef, RecordStore  # noqa: E402
from strata.scope_manager import (  # noqa: E402
    BatchVerdict,
    DirectiveOp,
    ScopeManagerBatchJudgment,
    ScopeManagerJudgment,
)
from strata.summary_store import Directive, ScopeSummary, SummaryStore  # noqa: E402


@pytest.fixture
def fleet(tmp_path: Path) -> FleetConfig:
    raw = {
        "strata": [{"id": "L0", "name": "executive", "ordinal": 0}],
        "scopes": [{"id": "g_sv", "name": "SV", "stratum_id": "L0"}],
        "edges": [],
    }
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.dump(raw), encoding="utf-8")
    return FleetConfig.load(path)


@pytest.fixture
def record_store(tmp_path: Path):  # noqa: ANN201
    db_path = str(tmp_path / "record.db")
    run_migrations(db_path)
    with RecordStore(db_path) as store:
        yield store


@pytest.fixture
def summary_store(tmp_path: Path) -> SummaryStore:
    return SummaryStore(str(tmp_path / "summaries"))


def _contributor() -> ContributorRef:
    return ContributorRef(
        scope_id="g_sv", skill="s", session_id="sess", ts="2026-09-06T00:00:00+00:00"
    )


def _summary(*directives: Directive) -> ScopeSummary:
    return ScopeSummary(
        scope_id="g_sv",
        directives=list(directives),
        context="ctx",
        updated_at="2026-09-06T00:00:00+00:00",
    )


class _Manager:
    def __init__(self, judgment) -> None:  # noqa: ANN001
        self.judgment = judgment

    def judge(self, **_kw):  # noqa: ANN003, ANN201
        return self.judgment

    def judge_batch(self, **_kw):  # noqa: ANN003, ANN201
        return self.judgment


def _run(fleet, record_store, summary_store, judgment, content="A rule."):  # noqa: ANN001, ANN201
    return run_contribution(
        scope=fleet.get_scope("g_sv"),
        stratum=fleet.strata[0],
        content=content,
        proposed_classification="directive",
        subject="rpc",
        supersedes=None,
        contributor=_contributor(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=_Manager(judgment),
        summary_max_words=500,
    )


def test_store_stamps_and_reads_back_the_summary_version(record_store) -> None:
    c = record_store.append_contribution(
        scope_id="g_sv",
        content="x",
        proposed_classification="directive",
        subject="s",
        supersedes=None,
        contributor=_contributor(),
    )
    record_store.record_judgment(
        contribution_id=c.id, decision="accept_as_directive", judged_by="t"
    )
    assert record_store.get_judgment(c.id).summary_version is None
    record_store.stamp_summary_version([c.id], version=7)
    assert record_store.get_judgment(c.id).summary_version == 7
    assert record_store.list_judgments(scope_id="g_sv")[0].summary_version == 7


def test_an_accepted_judgment_names_the_version_its_amendment_wrote(
    fleet, record_store, summary_store
) -> None:
    judgment = ScopeManagerJudgment(
        decision="accept_as_directive",
        reasoning="ok",
        new_summary=_summary(),
        directive_ops=[DirectiveOp(op="append")],
    )
    out = _run(fleet, record_store, summary_store, judgment)
    written = summary_store.read("g_sv")
    assert written.version == 1  # precondition: the amendment really wrote
    assert record_store.get_judgment(out.contribution_id).summary_version == written.version


def test_a_decline_names_no_version(fleet, record_store, summary_store) -> None:
    judgment = ScopeManagerJudgment(decision="decline", reasoning="no", new_summary=None)
    out = _run(fleet, record_store, summary_store, judgment)
    row = record_store.get_judgment(out.contribution_id)
    assert row is not None  # precondition: the decline was recorded
    assert row.summary_version is None


def test_a_batch_shares_one_version_across_its_accepted_rows(
    fleet, record_store, summary_store
) -> None:
    """ADR 0011 D3: N accepts, one write — the record now says so per row."""
    from strata.app import _judge_batch_and_record

    scope = fleet.get_scope("g_sv")
    ids = []
    for n in range(3):
        c = record_store.append_contribution(
            scope_id="g_sv",
            content=f"rule {n}",
            proposed_classification="directive",
            subject="s",
            supersedes=None,
            contributor=_contributor(),
        )
        ids.append(c.id)
    contributions = [record_store.get_contribution(i) for i in ids]
    batch = ScopeManagerBatchJudgment(
        verdicts=[
            BatchVerdict(contribution_id=ids[0], decision="accept_as_directive", reasoning="a"),
            BatchVerdict(contribution_id=ids[1], decision="decline", reasoning="d"),
            BatchVerdict(contribution_id=ids[2], decision="accept_as_context", reasoning="c"),
        ],
        new_summary=_summary(),
        new_context="ctx2",
    )
    _judge_batch_and_record(
        contributions=contributions,
        scope=scope,
        stratum=fleet.strata[0],
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=_Manager(batch),
        summary_max_words=500,
    )
    written = summary_store.read("g_sv")
    assert written.version == 1  # precondition: one write for the batch
    rows = {i: record_store.get_judgment(i) for i in ids}
    assert rows[ids[0]].summary_version == 1
    assert rows[ids[2]].summary_version == 1
    assert rows[ids[1]].summary_version is None
