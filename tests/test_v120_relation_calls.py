"""v1.20 (#242 b): the relation re-ask's answer is kept with the judgment, so a decline is
explainable after the fact. Migration 0027, the store, the app write path (single and batch),
and what the manager keeps beyond the outcome dict."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.app import _judge_batch_and_record, run_contribution  # noqa: E402
from strata.fleet_config import FleetConfig  # noqa: E402
from strata.migrator import run_migrations  # noqa: E402
from strata.record_store import ContributorRef, RecordStore  # noqa: E402
from strata.scope_manager import (  # noqa: E402
    BatchVerdict,
    ScopeManager,
    ScopeManagerBatchJudgment,
    ScopeManagerJudgment,
)
from strata.summary_store import ScopeSummary, SummaryStore  # noqa: E402
from tests.test_scope_manager import (  # noqa: E402
    CURRENT_SUMMARY,
    NEW_CONTRIBUTION,
    SCOPE,
    STRATUM,
    _fake_response,
)
from tests.test_v18_context_exception import (  # noqa: E402
    ANCESTORS,
    EXCEPTION_NORMATIVE,
    FIRST_CONTEXT,
    _answer,
)

OUTCOME = {
    "contribution_id": "",
    "kind": "exception",
    "verdict": "decline",
    "fallback": False,
    "inherited_id": "c_pump01",
    "origin": "g_arch",
    "reason": "an exception: states what happens instead of the rule",
}


@pytest.fixture
def fleet(tmp_path: Path) -> FleetConfig:
    path = tmp_path / "fleet.yaml"
    path.write_text(
        yaml.dump(
            {
                "strata": [{"id": "L0", "name": "executive", "ordinal": 0}],
                "scopes": [{"id": "g_sv", "name": "SV", "stratum_id": "L0"}],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
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
        scope_id="g_sv", skill="s", session_id="sess", ts="2026-10-26T00:00:00+00:00"
    )


def _append(store: RecordStore, text: str = "A note.") -> str:
    return store.append_contribution(
        scope_id="g_sv",
        content=text,
        proposed_classification="context",
        subject="s",
        supersedes=None,
        contributor=_contributor(),
    ).id


class _Manager:
    """A stand-in manager with a fixed verdict that, like the real one, keeps extras."""

    def __init__(self, judgment, extras=None) -> None:  # noqa: ANN001
        self.judgment = judgment
        self.extras = extras or {}

    def judge(self, **_kw):  # noqa: ANN003, ANN201
        return self.judgment

    def judge_batch(self, **_kw):  # noqa: ANN003, ANN201
        return self.judgment

    def take_relation_extras(self, contribution_id: str, call: str) -> dict:
        return self.extras.pop((contribution_id, call), {})


# -- the migration and the store ---------------------------------------------------------


def test_0027_creates_the_table_and_touches_nothing_else(tmp_path: Path) -> None:
    db = str(tmp_path / "m.db")
    applied = run_migrations(db)
    assert applied[-1] == "0027_relation_calls.sql"
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(relation_calls)")]
    assert cols == [
        "contribution_id",
        "call",
        "answer_kind",
        "inherited_id",
        "origin",
        "occurrence_span",
        "instead_span",
        "requirement_span",
        "verdict",
        "fallback",
        "reason",
        "marker",
        "created_at",
    ]
    text = (
        Path(__file__).parent.parent / "src/strata/_migrations/0027_relation_calls.sql"
    ).read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    assert code.count("CREATE TABLE") == 1 and "ALTER" not in code and "DROP" not in code


def test_a_judgment_and_its_relation_rows_are_one_transaction(record_store) -> None:  # noqa: ANN001
    cid = _append(record_store)
    record_store.record_judgment(
        contribution_id=cid,
        decision="decline",
        judged_by="t",
        relations=[{**OUTCOME, "call": "context", "marker": "m", "instead_span": "may stay"}],
    )
    (row,) = record_store.get_relation_calls(cid)
    assert (row["call"], row["answer_kind"], row["verdict"], row["fallback"]) == (
        "context",
        "exception",
        "decline",
        False,
    )
    assert (row["inherited_id"], row["origin"], row["marker"]) == ("c_pump01", "g_arch", "m")
    assert row["instead_span"] == "may stay" and row["requirement_span"] is None
    # a second judgment for the same contribution violates UNIQUE: nothing new is written
    with pytest.raises(sqlite3.IntegrityError):
        record_store.record_judgment(
            contribution_id=cid,
            decision="decline",
            judged_by="t",
            relations=[{**OUTCOME, "call": "directive", "verdict": "admit"}],
        )
    assert [r["call"] for r in record_store.get_relation_calls(cid)] == ["context"]


def test_a_judgment_without_a_relation_call_writes_no_row(record_store) -> None:  # noqa: ANN001
    cid = _append(record_store)
    record_store.record_judgment(contribution_id=cid, decision="accept_as_context", judged_by="t")
    assert record_store.get_relation_calls(cid) == []


# -- the app write path ------------------------------------------------------------------


def test_a_single_judgment_records_its_relation_answer(fleet, record_store, summary_store):  # noqa: ANN001, ANN201
    cid_holder: dict[str, str] = {}

    class Capture(_Manager):
        def judge(self, **kw):  # noqa: ANN003, ANN201
            cid = kw["new_contribution"].id
            cid_holder["id"] = cid
            self.judgment = ScopeManagerJudgment(
                decision="decline",
                reasoning="declined",
                new_summary=None,
                inherited_relation={**OUTCOME, "contribution_id": cid},
            )
            self.extras = {
                (cid, "context"): {"instead_span": "may stay running", "marker": "normative"}
            }
            return self.judgment

    out = run_contribution(
        scope=fleet.get_scope("g_sv"),
        stratum=fleet.strata[0],
        content="The pumps may stay running.",
        proposed_classification="context",
        subject="pumps",
        supersedes=None,
        contributor=_contributor(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=Capture(None),
        summary_max_words=500,
    )
    (row,) = record_store.get_relation_calls(out.contribution_id)
    assert row["answer_kind"] == "exception" and row["instead_span"] == "may stay running"
    assert row["marker"] == "normative" and row["verdict"] == "decline"


def test_a_batch_records_one_row_per_member_that_had_a_re_ask(  # noqa: ANN201
    fleet,
    record_store,
    summary_store,  # noqa: ANN001
):
    ids = [_append(record_store, f"note {n}") for n in range(3)]
    batch = ScopeManagerBatchJudgment(
        verdicts=[
            BatchVerdict(contribution_id=ids[0], decision="decline", reasoning="d"),
            BatchVerdict(contribution_id=ids[1], decision="accept_as_context", reasoning="c"),
            BatchVerdict(contribution_id=ids[2], decision="accept_as_context", reasoning="c"),
        ],
        new_summary=ScopeSummary(
            scope_id="g_sv", directives=[], context="ctx", updated_at="2026-10-26T00:00:00+00:00"
        ),
        new_context="ctx2",
        inherited_relations=[
            {**OUTCOME, "contribution_id": ids[0]},
            {**OUTCOME, "contribution_id": ids[1], "kind": "unrelated", "verdict": "admit"},
        ],
    )
    _judge_batch_and_record(
        contributions=[record_store.get_contribution(i) for i in ids],
        scope=fleet.get_scope("g_sv"),
        stratum=fleet.strata[0],
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=_Manager(batch, {(ids[0], "context"): {"marker": "mk"}}),
        summary_max_words=500,
    )
    assert [r["marker"] for r in record_store.get_relation_calls(ids[0])] == ["mk"]
    assert record_store.get_relation_calls(ids[1])[0]["answer_kind"] == "unrelated"
    assert record_store.get_relation_calls(ids[2]) == []  # no re-ask fired for it


def test_a_manager_without_extras_still_records_the_outcome(  # noqa: ANN201
    fleet,
    record_store,
    summary_store,  # noqa: ANN001
):
    class Plain:  # a stand-in with no take_relation_extras
        def judge(self, **kw):  # noqa: ANN003, ANN201
            cid = kw["new_contribution"].id
            return ScopeManagerJudgment(
                decision="decline",
                reasoning="x",
                new_summary=None,
                inherited_relation={**OUTCOME, "contribution_id": cid},
            )

    out = run_contribution(
        scope=fleet.get_scope("g_sv"),
        stratum=fleet.strata[0],
        content="x",
        proposed_classification="context",
        subject="s",
        supersedes=None,
        contributor=_contributor(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=Plain(),
        summary_max_words=500,
    )
    (row,) = record_store.get_relation_calls(out.contribution_id)
    assert row["verdict"] == "decline" and row["marker"] is None


# -- what the real manager keeps ---------------------------------------------------------


def test_the_real_manager_keeps_the_quoted_span_and_the_marker() -> None:
    client = MagicMock()
    client.messages.create.side_effect = [
        _fake_response(FIRST_CONTEXT),
        _fake_response(_answer("exception", instead_span="may stay running until 22:00")),
    ]
    manager = ScopeManager(client=client)
    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=replace(
            NEW_CONTRIBUTION, content=EXCEPTION_NORMATIVE, proposed_classification="context"
        ),
        ancestor_directives=ANCESTORS,
    )
    cid = NEW_CONTRIBUTION.id
    extras = manager.take_relation_extras(cid, "context")
    assert extras["instead_span"] == "may stay running until 22:00"
    assert extras["occurrence_span"] is None and "normative marker" in extras["marker"]
    assert manager.take_relation_extras(cid, "context") == {}  # taken once
    assert "instead_span" not in judgment.inherited_relation  # the outcome dict is unchanged
