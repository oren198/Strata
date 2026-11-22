"""Migration 0029 and the record-store side of the amendment intent journal (v1.22 P2)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from strata.migrator import run_migrations
from strata.record_store import RecordStore


@pytest.fixture
def store(tmp_path: Path):  # noqa: ANN201
    db = str(tmp_path / "strata.db")
    run_migrations(db)
    with RecordStore(db) as s:
        yield s


def test_the_migration_adds_the_table_the_column_and_the_unique_index(tmp_path: Path) -> None:
    db = str(tmp_path / "strata.db")
    applied = run_migrations(db)
    assert applied[applied.index("0028_human_review.sql") + 1] == "0029_amendment_intents.sql"
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(amendment_intents)")]
    assert cols == [
        "id",
        "scope_id",
        "expected_version",
        "state",
        "payload_json",
        "created_at",
        "updated_at",
    ]
    assert "intent_id" in [r[1] for r in conn.execute("PRAGMA table_info(retirements)")]
    index = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_retirements_intent'"
    ).fetchone()[0]
    assert "UNIQUE" in index and "scope_id, directive_id, intent_id" in index


def test_an_intent_is_pending_until_it_is_marked(store: RecordStore) -> None:
    intent_id = store.create_amendment_intent(
        scope_id="g_org", expected_version=3, payload_json=json.dumps({"judged_ids": ["c_1"]})
    )
    intent = store.get_amendment_intent(intent_id)
    assert intent.state == "pending" and intent.expected_version == 3
    assert [i.id for i in store.list_amendment_intents(scope_id="g_org", state="pending")] == [
        intent_id
    ]
    store.mark_amendment_intent(intent_id, "done")
    assert store.get_amendment_intent(intent_id).state == "done"
    assert store.list_amendment_intents(scope_id="g_org", state="pending") == []
    with pytest.raises(sqlite3.IntegrityError):
        store.mark_amendment_intent(intent_id, "bogus")


def test_the_intent_for_a_contribution_is_found_by_its_judged_ids(store: RecordStore) -> None:
    a = store.create_amendment_intent(
        scope_id="g_org", expected_version=1, payload_json=json.dumps({"judged_ids": ["c_a"]})
    )
    b = store.create_amendment_intent(
        scope_id="g_org",
        expected_version=2,
        payload_json=json.dumps({"judged_ids": ["c_b", "c_c"]}),
    )
    assert store.intent_for_contribution("c_a").id == a
    assert store.intent_for_contribution("c_c").id == b
    assert store.intent_for_contribution("c_none") is None


def test_a_retirement_written_twice_for_one_intent_is_one_row(store: RecordStore) -> None:
    first = store.append_retirement(
        scope_id="g_org",
        directive_id="c_x",
        retired_by="scope-manager",
        reason="r",
        intent_id="ai_1",
    )
    again = store.append_retirement(
        scope_id="g_org",
        directive_id="c_x",
        retired_by="scope-manager",
        reason="r",
        intent_id="ai_1",
    )
    assert again.id == first.id
    assert len(store.list_retirements(scope_id="g_org")) == 1
    # another intent, or no intent at all, is a different act
    store.append_retirement(
        scope_id="g_org", directive_id="c_x", retired_by="scope-manager", intent_id="ai_2"
    )
    store.append_retirement(scope_id="g_org", directive_id="c_x", retired_by="operator")
    assert len(store.list_retirements(scope_id="g_org")) == 3
