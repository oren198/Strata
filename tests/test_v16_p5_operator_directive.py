"""v1.16 P5 — operator directives: `acted_on` may name an operator act
(`op_`-prefixed) instead of a scope-held contribution. `validate_acted_on`'s
operator branch (entitlement + liveness, mirroring the scope-held rules) and
`_judge_and_record`'s resolution of the CURRENT operator item, plus the
`OperatorEvidenceInput` raise path for a `failed` verdict against one.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager, validate_acted_on
from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.operator import operator_publish, operator_retire_item
from strata.record_store import RecordStore
from strata.scope_manager import ScopeManagerJudgment
from strata.settings import Settings
from strata.summary_store import ScopeSummary

_FLEET_YAML = """
strata:
  - id: L0
    name: Executive
    ordinal: 0
  - id: L1
    name: Function
    ordinal: 1

scopes:
  - id: g_source
    name: Source
    stratum_id: L0
    status: active
  - id: g_reporter
    name: Reporter
    stratum_id: L1
    status: active
  - id: g_outside
    name: Outside
    stratum_id: L0
    status: active

edges:
  - from: g_reporter
    to: g_source
"""

_CONTRIBUTOR_BODY = {
    "scope_id": "g_reporter",
    "skill": "engineer",
    "session_id": "sess_001",
    "ts": "2026-09-25T09:00:00Z",
}


def _judgment(decision: str, *, summary=None, outcome_disposition=None) -> ScopeManagerJudgment:
    return ScopeManagerJudgment(
        decision=decision,
        reasoning="test reasoning",
        new_summary=summary,
        outcome_disposition=outcome_disposition,
    )


def _summary(scope_id: str, context: str) -> ScopeSummary:
    return ScopeSummary(
        scope_id=scope_id, directives=[], context=context, updated_at="2026-09-01T00:00:00Z"
    )


@pytest.fixture()
def env(tmp_path):
    db_path = str(tmp_path / "test.db")
    summaries_dir = str(tmp_path / "summaries")
    run_migrations(db_path)
    fleet = FleetConfig.model_validate(
        {
            "strata": [
                {"id": "L0", "name": "Executive", "ordinal": 0},
                {"id": "L1", "name": "Function", "ordinal": 1},
            ],
            "scopes": [
                {"id": "g_source", "name": "Source", "stratum_id": "L0"},
                {"id": "g_reporter", "name": "Reporter", "stratum_id": "L1"},
                {"id": "g_outside", "name": "Outside", "stratum_id": "L0"},
            ],
            "edges": [{"from": "g_reporter", "to": "g_source"}],
        }
    )
    store = RecordStore(db_path)
    return db_path, summaries_dir, fleet, store


@pytest.fixture()
def http_client(tmp_path):
    db_path = str(tmp_path / "test.db")
    summaries_dir = str(tmp_path / "summaries")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")
    run_migrations(db_path)
    Path(fleet_yaml_path).write_text(_FLEET_YAML, encoding="utf-8")
    settings = Settings(
        db_path=db_path,
        summaries_dir=summaries_dir,
        fleet_yaml_path=fleet_yaml_path,
        manager_model="claude-haiku-4-5",
        anthropic_api_key="test-key",
    )
    application = create_app(settings=settings)
    mock_manager = MagicMock()
    application.dependency_overrides[get_scope_manager] = lambda: mock_manager
    with TestClient(application) as tc:
        tc.mock_manager = mock_manager  # type: ignore[attr-defined]
        tc.db_path = db_path  # type: ignore[attr-defined]
        tc.summaries_dir = summaries_dir  # type: ignore[attr-defined]
        yield tc


# --- validate_acted_on's operator branch --------------------------------------------


def test_validate_acted_on_accepts_a_live_operator_directive_within_the_chain(env) -> None:
    _db_path, summaries_dir, fleet, store = env
    item = operator_publish(
        "g_source",
        "Never deploy on a Friday.",
        "deploy-window",
        record_store=store,
        summaries_dir=summaries_dir,
        fleet=fleet,
    )
    validate_acted_on(fleet, store, acted_on=item.id, supersedes=None, agent_scope="g_reporter")


def test_validate_acted_on_rejects_an_operator_directive_outside_the_entitled_chain(env) -> None:
    _db_path, summaries_dir, fleet, store = env
    item = operator_publish(
        "g_source",
        "Never deploy on a Friday.",
        "deploy-window",
        record_store=store,
        summaries_dir=summaries_dir,
        fleet=fleet,
    )
    with pytest.raises(RuntimeError, match="outside your entitled surface"):
        validate_acted_on(fleet, store, acted_on=item.id, supersedes=None, agent_scope="g_outside")


def test_validate_acted_on_rejects_a_retired_operator_directive(env) -> None:
    _db_path, summaries_dir, fleet, store = env
    item = operator_publish(
        "g_source",
        "Never deploy on a Friday.",
        "deploy-window",
        record_store=store,
        summaries_dir=summaries_dir,
        fleet=fleet,
    )
    operator_retire_item(
        "g_source", item.id, record_store=store, summaries_dir=summaries_dir, fleet=fleet
    )
    with pytest.raises(RuntimeError, match="no longer current"):
        validate_acted_on(fleet, store, acted_on=item.id, supersedes=None, agent_scope="g_reporter")


def test_validate_acted_on_rejects_an_unknown_operator_id(env) -> None:
    _db_path, _summaries_dir, fleet, store = env
    with pytest.raises(RuntimeError, match="does not reference an existing operator item"):
        validate_acted_on(
            fleet, store, acted_on="op_doesnotexist", supersedes=None, agent_scope="g_reporter"
        )


# --- end to end: a failed outcome against an operator directive --------------------


def test_a_failed_outcome_against_an_operator_directive_writes_unjudged_evidence(
    http_client,
) -> None:
    fleet = FleetConfig.model_validate(
        {
            "strata": [{"id": "L0", "name": "Executive", "ordinal": 0}],
            "scopes": [{"id": "g_source", "name": "Source", "stratum_id": "L0"}],
            "edges": [],
        }
    )
    store = RecordStore(http_client.db_path)
    item = operator_publish(
        "g_source",
        "Never deploy on a Friday.",
        "deploy-window",
        record_store=store,
        summaries_dir=http_client.summaries_dir,
        fleet=fleet,
    )

    http_client.mock_manager.judge.side_effect = [
        _judgment(
            "accept_as_context",
            summary=_summary("g_reporter", "deployed Friday, broke"),
            outcome_disposition="failed",
        ),
    ]
    resp = http_client.post(
        "/contribute",
        json={
            "scope_id": "g_reporter",
            "content": "Deployed on Friday; it broke within the hour.",
            "proposed_classification": "context",
            "contributor": _CONTRIBUTOR_BODY,
            "acted_on": item.id,
        },
    )
    assert resp.status_code == 200
    outcome_id = resp.json()["contribution_id"]

    outcome_contribution = store.get_contribution(outcome_id)
    assert outcome_contribution.acted_on is None
    assert outcome_contribution.acted_on_operator_item == item.id

    evidence = store.list_operator_evidence(operator_item_id=item.id)
    assert len(evidence) == 1
    assert evidence[0].raised_from == outcome_id
    assert evidence[0].reporter_scope_id == "g_reporter"
    assert f"Following {item.id} went wrong" in evidence[0].content
    assert "Deployed on Friday; it broke within the hour." in evidence[0].content
    assert evidence[0].seen_at is None

    # Unjudged, per the plan: no synchronous second judge call for an operator raise.
    assert http_client.mock_manager.judge.call_count == 1
