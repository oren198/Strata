"""API-level tests for the Console's publication surfaces (ADR 0013):

- GET /scopes/{scope_id}/publication — what a scope publishes right now
  (view 1): its current items, verbatim, including republication provenance
  (origin/relay) for a relayed item.
- GET /scopes/{scope_id}/publication/record — the scope's publish/withdraw
  act history (view 2): every act plus its judgment state, honestly
  distinguishing a judged verdict, a mechanically-cascaded withdrawal, a
  judge failure, and an act still awaiting judgment.

Both are UI-only, read-only endpoints (constraint G1): no engine flow calls
them. The client fixture is copied from tests/test_app_declines.py so no
live judge is ever called; publication acts are seeded directly through
RecordStore / the publication artifact writer, never through a route or MCP.
"""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager
from strata.migrator import run_migrations
from strata.publication import PublishedItem, _write_publication
from strata.record_store import JUDGE_FAILED, ContributorRef, RecordStore
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
      - id: g_other
        name: Other Scope
        stratum_id: L1
        status: active

    edges: []
""").strip()


@pytest.fixture()
def client(tmp_path):
    """Yield a TestClient backed by a fresh DB + FleetConfig from a tmp fleet.yaml.

    Copied verbatim (fixture shape) from tests/test_app_declines.py — the
    proven-safe pattern here: a fully isolated tmp_path store, never the
    operator's real fleet.
    """
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
        tc.mock_manager = mock_manager  # type: ignore[attr-defined]
        tc.summaries_dir = summaries_dir  # type: ignore[attr-defined]
        tc.db_path = db_path  # type: ignore[attr-defined]
        yield tc


def _proposer(scope_id="g_active", session_id="sess_1"):
    return ContributorRef(
        scope_id=scope_id,
        skill="architect",
        session_id=session_id,
        ts="2026-08-20T10:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# GET /scopes/{scope_id}/publication — view 1: what a scope publishes now.
# ---------------------------------------------------------------------------


def test_publication_endpoint_unknown_scope_is_404(client):
    resp = client.get("/scopes/g_nope/publication")
    assert resp.status_code == 404


def test_publication_endpoint_empty_for_scope_with_no_artifact(client):
    resp = client.get("/scopes/g_active/publication")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope_id"] == "g_active"
    assert body["items"] == []


def test_publication_endpoint_returns_seeded_items_verbatim(client):
    items = [
        PublishedItem(
            id="pub_000001",
            kind="context",
            content="We prefer gRPC for inter-service calls.",
            subject="rpc-protocol",
            anchors=["subject:rpc-protocol"],
            published_at="2026-08-20T10:00:00+00:00",
        ),
    ]
    _write_publication("g_active", items, summaries_dir=client.summaries_dir)

    resp = client.get("/scopes/g_active/publication")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope_id"] == "g_active"
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == "pub_000001"
    assert item["content"] == "We prefer gRPC for inter-service calls."
    assert item["subject"] == "rpc-protocol"
    assert item["anchors"] == ["subject:rpc-protocol"]
    # Non-relay item: no invented origin/relay provenance.
    assert item["origin_scope_id"] is None
    assert item["relay_scope_id"] is None
    assert item["relay_item_id"] is None


def test_publication_endpoint_marks_relayed_item_provenance(client):
    items = [
        PublishedItem(
            id="pub_000002",
            kind="context",
            content="Root policy: prefer async messaging.",
            subject=None,
            anchors=["subject:messaging"],
            published_at="2026-08-20T10:00:00+00:00",
            origin_scope_id="g_root",
            relay_scope_id="g_other",
            relay_item_id="pub_000001",
        ),
    ]
    _write_publication("g_active", items, summaries_dir=client.summaries_dir)

    resp = client.get("/scopes/g_active/publication")
    body = resp.json()
    item = body["items"][0]
    assert item["origin_scope_id"] == "g_root"
    assert item["relay_scope_id"] == "g_other"
    assert item["relay_item_id"] == "pub_000001"


# ---------------------------------------------------------------------------
# GET /scopes/{scope_id}/publication/record — view 2: the publish/withdraw
# act history, honestly distinguishing judged / mechanical / judge_failed /
# pending.
# ---------------------------------------------------------------------------


def test_publication_record_endpoint_unknown_scope_is_404(client):
    resp = client.get("/scopes/g_nope/publication/record")
    assert resp.status_code == 404


def test_publication_record_endpoint_empty_for_scope_with_no_acts(client):
    resp = client.get("/scopes/g_active/publication/record")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope_id"] == "g_active"
    assert body["acts"] == []
    assert body["judgments"] == []
    assert body["judgment_attempts"] == []
    assert body["act_states"] == []


def test_publication_record_endpoint_distinguishes_all_four_states(client):
    with RecordStore(client.db_path) as store:
        # 1. Judged — a publish act with a recorded verdict.
        judged_act = store.append_publication_act(
            scope_id="g_active",
            act="publish",
            kind="context",
            content="We prefer gRPC.",
            subject="rpc",
            anchors=["subject:rpc"],
            withdraws=None,
            trigger=None,
            proposer=_proposer(),
        )
        store.record_publication_judgment(
            act_id=judged_act.id,
            decision="accept",
            judged_by="scope-manager",
            reasoning="Clear and well-anchored.",
        )

        # 2. Mechanical — a withdrawal carrying a trigger, no judgment row.
        mechanical_act = store.append_publication_act(
            scope_id="g_active",
            act="withdraw",
            kind=None,
            content=None,
            subject=None,
            anchors=None,
            withdraws=judged_act.id,
            trigger="c_000001",
            proposer=_proposer(),
        )

        # 3. Judge failed — a publish act with a failed judgment attempt.
        failed_act = store.append_publication_act(
            scope_id="g_active",
            act="publish",
            kind="context",
            content="Draft policy, never judged successfully.",
            subject=None,
            anchors=["subject:draft"],
            withdraws=None,
            trigger=None,
            proposer=_proposer(),
        )
        store.record_publication_judgment_attempt(
            act_id=failed_act.id,
            error_class="AuthenticationError",
            message="401 from the judge model.",
            outcome=JUDGE_FAILED,
        )

        # 4. Pending — a publish act with no judgment and no attempt yet.
        pending_act = store.append_publication_act(
            scope_id="g_active",
            act="publish",
            kind="context",
            content="Just submitted, still in flight.",
            subject=None,
            anchors=["subject:inflight"],
            withdraws=None,
            trigger=None,
            proposer=_proposer(),
        )

    resp = client.get("/scopes/g_active/publication/record")
    assert resp.status_code == 200
    body = resp.json()

    assert {a["id"] for a in body["acts"]} == {
        judged_act.id,
        mechanical_act.id,
        failed_act.id,
        pending_act.id,
    }
    # The withdraw act's `withdraws` reference survives serialization, so a
    # client can join it back to what it removed.
    withdraw_row = next(a for a in body["acts"] if a["id"] == mechanical_act.id)
    assert withdraw_row["withdraws"] == judged_act.id
    assert withdraw_row["content"] is None

    states = {s["act_id"]: s for s in body["act_states"]}
    assert states[judged_act.id]["state"] == "judged"
    assert states[judged_act.id]["decision"] == "accept"

    assert states[mechanical_act.id]["state"] == "mechanical"
    assert states[mechanical_act.id]["decision"] is None

    assert states[failed_act.id]["state"] == "judge_failed"
    assert states[failed_act.id]["error_class"] == "AuthenticationError"

    assert states[pending_act.id]["state"] == "pending"
    assert states[pending_act.id]["decision"] is None

    assert len(body["judgments"]) == 1
    assert body["judgments"][0]["act_id"] == judged_act.id

    assert len(body["judgment_attempts"]) == 1
    assert body["judgment_attempts"][0]["act_id"] == failed_act.id


# ---------------------------------------------------------------------------
# UI static registration — revert-detectable coverage for the frontend file
# (no JS test harness in this repo).
# ---------------------------------------------------------------------------


def test_ui_publications_jsx_is_served(client):
    resp = client.get("/ui/publications.jsx")
    assert resp.status_code == 200


def test_ui_index_references_publications_jsx(client):
    resp = client.get("/ui/index.html")
    assert resp.status_code == 200
    assert "publications.jsx" in resp.text
