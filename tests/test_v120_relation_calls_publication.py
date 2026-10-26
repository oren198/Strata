"""v1.20 (#242 b): the #243 publication check's relation answer is kept with the publication
act's judgment, accepted or declined, keyed by the act id (a `pub_...` id the record makes
before the judge is asked, so a declined proposal has one too)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import strata.publication as publication
from strata.publication import bootstrap_publication, propose_restore
from strata.scope_manager import (
    BootstrapPublishedItemInput,
    InheritedReaskUnavailable,
    PublicationExceptionCheck,
)
from tests import test_v18_243_publication_exception as t243
from tests.test_v18_243_publication_exception import world  # noqa: F401 — the module's fixture
from tests.test_v19_reask_fail_closed import ATTEMPTS, _status_error

EXCEPTION_ITEM = "Hotfix builds don't need the full suite; the smoke check is enough."


def _exception_client():  # noqa: ANN202
    return t243._client(  # noqa: SLF001
        t243._reask(kind="exception", instead_span="don't need the full suite")  # noqa: SLF001
    )


def test_a_declined_publication_has_its_relation_row_under_its_act_id(world) -> None:  # noqa: ANN001, F811
    outcome, _ = t243._publish(world, EXCEPTION_ITEM, "context", _exception_client())  # noqa: SLF001
    store = world[1]
    assert outcome.decision == "decline" and outcome.act_id.startswith("pub_")
    # the act exists in the record although nothing was published
    assert store.get_publication_act(outcome.act_id) is not None
    (row,) = store.get_publication_relation_calls(outcome.act_id)
    assert row["call"] == "context" and row["answer_kind"] == "exception"
    assert row["verdict"] == "decline" and row["inherited_id"] == "c_suite"
    assert row["instead_span"] == "don't need the full suite"
    assert row["marker"] and "normative marker" in row["marker"]
    assert store.get_publication_judgment(outcome.act_id).decision == "decline"


def test_an_accepted_publication_has_its_relation_row_under_its_act_id(world) -> None:  # noqa: ANN001, F811
    client = t243._client(  # noqa: SLF001
        t243._reask(  # noqa: SLF001
            kind="consequence_report",
            occurrence_span="On 12 May the full test suite caught a caching bug",
        )
    )
    outcome, _ = t243._publish(world, t243._CONSEQUENCE, "context", client)  # noqa: SLF001
    (row,) = world[1].get_publication_relation_calls(outcome.act_id)
    assert outcome.decision == "accept" and row["verdict"] == "consequence_report"
    assert row["occurrence_span"] == "On 12 May the full test suite caught a caching bug"
    assert row["fallback"] is False and row["requirement_span"] is None


def test_the_unreadable_answer_fallback_is_recorded_as_a_fallback(world) -> None:  # noqa: ANN001, F811
    outcome, _ = t243._publish(  # noqa: SLF001
        world,
        t243._CONSEQUENCE,
        "context",
        t243._client(ValueError("boom")),  # noqa: SLF001
    )
    (row,) = world[1].get_publication_relation_calls(outcome.act_id)
    assert row["fallback"] is True and row["answer_kind"] is None


def test_a_directive_item_refused_without_a_re_ask_writes_no_row(world) -> None:  # noqa: ANN001, F811
    outcome, _ = t243._publish(world, t243._EXCEPTION, "directive", t243._client())  # noqa: SLF001
    assert outcome.decision == "decline"
    assert world[1].get_publication_relation_calls(outcome.act_id) == []


def test_a_re_ask_that_fails_closed_writes_no_row_and_no_publication(world) -> None:  # noqa: ANN001, F811
    client = t243._client(*[_status_error(429)] * ATTEMPTS)  # noqa: SLF001
    store = world[1]
    with pytest.raises(InheritedReaskUnavailable):
        t243._publish(world, t243._SINGULAR_DEPARTURE, "context", client)  # noqa: SLF001
    assert t243._face(world) == []  # noqa: SLF001
    (act,) = store.list_publication_acts(scope_id="g_web")
    assert store.get_publication_judgment(act.id) is None
    assert store.get_publication_relation_calls(act.id) == []
    assert store._conn.execute("SELECT COUNT(*) FROM relation_calls").fetchone()[0] == 0  # noqa: SLF001


def test_a_bootstrap_item_refused_or_accepted_records_its_re_ask(world) -> None:  # noqa: ANN001, F811
    fleet, store, summaries = world
    client = t243._client(  # noqa: SLF001
        t243._reask(kind="exception", instead_span="don't need the full suite"),  # noqa: SLF001
        t243._reask(  # noqa: SLF001
            kind="consequence_report",
            occurrence_span="On 12 May the full test suite caught a caching bug",
        ),
    )
    manager = t243._ScriptedManager(  # noqa: SLF001
        client,
        bootstrap_items=[
            BootstrapPublishedItemInput(
                content=EXCEPTION_ITEM, kind="context", anchors=["release process"]
            ),
            BootstrapPublishedItemInput(
                content=t243._CONSEQUENCE,  # noqa: SLF001
                kind="context",
                anchors=["release process"],
            ),
        ],
    )
    outcome = bootstrap_publication(
        "g_web", fleet=fleet, record_store=store, summary_store=summaries, scope_manager=manager
    )
    assert [i.content for i in outcome.items] == [t243._CONSEQUENCE]  # noqa: SLF001
    acts = {a.content: a for a in store.list_publication_acts(scope_id="g_web")}
    refused = store.get_publication_relation_calls(acts[EXCEPTION_ITEM].id)
    accepted = store.get_publication_relation_calls(acts[t243._CONSEQUENCE].id)  # noqa: SLF001
    assert [r["verdict"] for r in refused] == ["decline"]
    assert [r["verdict"] for r in accepted] == ["consequence_report"]


def test_a_restore_records_the_check_that_ran_on_it(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    from strata.fleet_config import FleetConfig
    from tests.test_v17_219_restore import (
        RecordStore,
        _AcceptingPublicationManager,
        _proposer,
        _setup_same_scope_correction,
    )

    db_path, fleet_yaml, summaries_dir, summary_store, item_id = _setup_same_scope_correction(
        tmp_path
    )
    outcome_dict = {
        "contribution_id": "publication-item-x",
        "kind": "unrelated",
        "verdict": "admit",
        "fallback": False,
        "inherited_id": "c_x",
        "origin": "g_org",
        "reason": "answered unrelated; no covered exception",
    }

    def fake_check(judgment, **kw):  # noqa: ANN001, ANN003, ANN202
        return (
            PublicationExceptionCheck(
                "accept",
                kw["content"],
                inherited_relation=outcome_dict,
                relation_extras={"marker": None},
            ),
            judgment,
        )

    monkeypatch.setattr(publication, "_apply_exception_check", fake_check)
    fleet = FleetConfig.load(fleet_yaml)
    with RecordStore(db_path) as store:
        out = propose_restore(
            "g_root",
            item_id,
            "false withdrawal",
            _proposer(),
            fleet=fleet,
            record_store=store,
            summary_store=summary_store,
            scope_manager=_AcceptingPublicationManager(),
        )
        (row,) = store.get_publication_relation_calls(out.act_id)
    assert row["answer_kind"] == "unrelated" and row["inherited_id"] == "c_x"


def test_a_relation_row_belongs_to_exactly_one_of_a_contribution_or_an_act(world) -> None:  # noqa: ANN001, F811
    conn = world[1]._conn  # noqa: SLF001
    both = (
        "INSERT INTO relation_calls (id, contribution_id, act_id, call, verdict) "
        "VALUES ('rc_1', 'c_x', 'pub_x', 'context', 'admit')"
    )
    neither = (
        "INSERT INTO relation_calls (id, contribution_id, act_id, call, verdict) "
        "VALUES ('rc_2', NULL, NULL, 'context', 'admit')"
    )
    for sql in (both, neither):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(relation_calls)")}
    assert {"idx_relation_calls_contribution", "idx_relation_calls_act"} <= indexes
