"""API-level tests for the restore act's detection surface (ADR 0008 D4-shaped
Console surface):

- GET  /scopes/{scope_id}/correction-withdrawals — every withdrawal a
  correction sweep made, plus #219 C's own unresolved/overflow rows.
- POST /scopes/{scope_id}/correction-withdrawals/{item_id}/restore — the
  operator path, in person, no judgment row.
- POST /scopes/{scope_id}/correction-withdrawals/{item_id}/acknowledge —
  "keep withdrawn" without restoring.

The client fixture mirrors tests/test_app_publication.py's shape — a fully
isolated tmp_path store, a mocked scope-manager (never a live judge call).
"""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from strata import app as app_module
from strata.app import create_app, get_scope_manager
from strata.migrator import run_migrations
from strata.publication import PublishedItem, _write_publication
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import ScopeManager
from strata.settings import Settings

_FLEET_YAML_SIMPLE = textwrap.dedent("""
    strata:
      - id: L0
        name: Executive
        ordinal: 0
      - id: L1
        name: Function
        ordinal: 1

    scopes:
      - id: g_active
        name: Active Scope
        stratum_id: L1
        status: active

    edges: []
""").strip()


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    summaries_dir = str(tmp_path / "summaries")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")

    run_migrations(db_path)
    (tmp_path / "fleet.yaml").write_text(_FLEET_YAML_SIMPLE, encoding="utf-8")

    settings = Settings(
        db_path=db_path,
        summaries_dir=summaries_dir,
        fleet_yaml_path=fleet_yaml_path,
        manager_model="claude-haiku-4-5",
        anthropic_api_key="test-key",
    )

    application = create_app(settings=settings)
    mock_manager = MagicMock(spec=ScopeManager)
    application.dependency_overrides[get_scope_manager] = lambda: mock_manager

    with TestClient(application) as tc:
        tc.scope_id = "g_active"  # type: ignore[attr-defined]
        tc.summaries_dir = summaries_dir  # type: ignore[attr-defined]
        tc.db_path = db_path  # type: ignore[attr-defined]
        yield tc


def _proposer(scope_id="g_active"):
    return ContributorRef(
        scope_id=scope_id, skill="engineer", session_id="s1", ts="2026-01-01T00:00:00Z"
    )


def _seed_restorable_withdrawal(client) -> str:
    """Seed g_active with a published item, then a same-scope correction that
    withdraws it (verbatim P4 sweep) — returns the withdrawn item's id."""
    with RecordStore(client.db_path) as store:
        target = store.append_contribution(
            scope_id="g_active",
            content="The service listens on port 8443.",
            proposed_classification="context",
            subject="service-port",
            supersedes=None,
            contributor=_proposer(),
        )
        store.record_judgment(
            contribution_id=target.id, decision="accept_as_context", judged_by="scope-manager"
        )
        act = store.append_publication_act(
            scope_id="g_active",
            act="publish",
            kind="context",
            content="The service listens on port 8443.",
            subject="service-port",
            anchors=["subject:service-port"],
            withdraws=None,
            trigger=None,
            proposer=_proposer(),
        )
        store.record_publication_judgment(
            act_id=act.id, decision="accept", judged_by="scope-manager"
        )
        _write_publication(
            "g_active",
            [
                PublishedItem(
                    id=act.id,
                    kind="context",
                    content="The service listens on port 8443.",
                    subject="service-port",
                    anchors=["subject:service-port"],
                    published_at="2026-01-01T00:00:00Z",
                )
            ],
            summaries_dir=client.summaries_dir,
        )

        from strata.fleet_config import FleetConfig
        from strata.scope_manager import ScopeManagerJudgment
        from strata.summary_store import ScopeSummary, SummaryStore

        outcome_judgment = ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="Confirmed by observation.",
            new_summary=ScopeSummary(
                scope_id="g_active",
                directives=[],
                context="The service's port is under investigation.",
                updated_at="2026-01-01T00:01:00Z",
            ),
            outcome_disposition="failed_corrected",
        )
        mock_manager = MagicMock()
        mock_manager.judge.return_value = outcome_judgment

        outcome = store.append_contribution(
            scope_id="g_active",
            content="Used port 8443, the service refused; the right port is unknown.",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_proposer(),
            acted_on=target.id,
        )
        fleet_ = FleetConfig.load(_fleet_yaml_path(client))
        summary_store_ = SummaryStore(client.summaries_dir)
        app_module._judge_and_record(
            contribution=outcome,
            scope=fleet_.get_scope("g_active"),
            stratum=fleet_.strata[0],
            fleet=fleet_,
            record_store=store,
            summary_store=summary_store_,
            scope_manager=mock_manager,
            summary_max_words=500,
        )
    return act.id


def _fleet_yaml_path(client):
    from pathlib import Path

    return Path(client.summaries_dir).parent / "fleet.yaml"


# ---------------------------------------------------------------------------
# GET /scopes/{scope_id}/correction-withdrawals
# ---------------------------------------------------------------------------


def test_correction_withdrawals_endpoint_unknown_scope_is_404(client):
    resp = client.get("/scopes/g_nope/correction-withdrawals")
    assert resp.status_code == 404


def test_correction_withdrawals_endpoint_empty_for_scope_with_no_sweep(client):
    resp = client.get("/scopes/g_active/correction-withdrawals")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope_id"] == "g_active"
    assert body["withdrawals"] == []
    assert body["unresolved"] == []


def test_correction_withdrawals_endpoint_lists_a_sweep_withdrawal(client):
    item_id = _seed_restorable_withdrawal(client)

    resp = client.get("/scopes/g_active/correction-withdrawals")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["withdrawals"]) == 1
    row = body["withdrawals"][0]
    assert row["act"]["withdraws"] == item_id
    assert row["method"] == "verbatim"
    assert row["correction"]["corrected_claim_content"] == "The service listens on port 8443."


# ---------------------------------------------------------------------------
# POST .../restore
# ---------------------------------------------------------------------------


def test_restore_endpoint_restores_the_item(client):
    item_id = _seed_restorable_withdrawal(client)

    resp = client.post(f"/scopes/g_active/correction-withdrawals/{item_id}/restore", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "accept"

    pub_resp = client.get("/scopes/g_active/publication")
    assert [i["id"] for i in pub_resp.json()["items"]] == [item_id]

    withdrawals_resp = client.get("/scopes/g_active/correction-withdrawals")
    assert withdrawals_resp.json()["withdrawals"] == []


def test_restore_endpoint_unknown_item_is_404(client):
    resp = client.post("/scopes/g_active/correction-withdrawals/pub_nonexistent/restore", json={})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST .../acknowledge
# ---------------------------------------------------------------------------


def test_acknowledge_endpoint_hides_the_row_without_restoring(client):
    item_id = _seed_restorable_withdrawal(client)

    resp = client.post(f"/scopes/g_active/correction-withdrawals/{item_id}/acknowledge")
    assert resp.status_code == 200
    assert resp.json()["act"]["acknowledged"] is True

    withdrawals_resp = client.get("/scopes/g_active/correction-withdrawals")
    assert withdrawals_resp.json()["withdrawals"] == []

    pub_resp = client.get("/scopes/g_active/publication")
    assert pub_resp.json()["items"] == []  # still withdrawn, not restored

    all_resp = client.get("/scopes/g_active/correction-withdrawals?all=true")
    assert len(all_resp.json()["withdrawals"]) == 1


def test_acknowledge_endpoint_unknown_item_is_404(client):
    resp = client.post("/scopes/g_active/correction-withdrawals/pub_nonexistent/acknowledge")
    assert resp.status_code == 404
