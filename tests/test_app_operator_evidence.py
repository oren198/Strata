"""API-level tests for GET /scopes/{scope_id}/operator-evidence and
POST /operator-evidence/{evidence_id}/seen — the Console surface for ADR
0017 P5's operator_evidence rows, shown WITH the directive they concern.

Follows test_app_perspective.py's fixture pattern verbatim.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager
from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.operator import operator_publish
from strata.record_store import (
    ContributorRef,
    OperatorEvidenceInput,
    RecordStore,
)
from strata.scope_manager import ScopeManager
from strata.settings import Settings

_FLEET_YAML = textwrap.dedent("""
    strata:
      - id: L0
        name: Executive
        ordinal: 0
      - id: L1
        name: Function
        ordinal: 1

    scopes:
      - id: g_root
        name: Root Scope
        stratum_id: L0
        status: active
      - id: g_child
        name: Child Scope
        stratum_id: L1
        status: active

    edges:
      - from: g_child
        to: g_root
""").strip()


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    summaries_dir = str(tmp_path / "summaries")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")

    run_migrations(db_path)
    (tmp_path / "fleet.yaml").write_text(_FLEET_YAML, encoding="utf-8")

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
        tc.db_path = db_path  # type: ignore[attr-defined]
        tc.summaries_dir = summaries_dir  # type: ignore[attr-defined]
        yield tc


def _fleet(client) -> FleetConfig:
    return FleetConfig.load(Path(client.db_path).parent / "fleet.yaml")


def _contributor(scope_id: str) -> ContributorRef:
    return ContributorRef(
        scope_id=scope_id, skill="engineer", session_id="sess_test", ts="2026-09-26T00:00:00Z"
    )


def _seed_evidence(
    db_path: str, summaries_dir: str, *, fleet, attachment_scope: str, reporter_scope: str
):
    """One operator directive at *attachment_scope* with one evidence row raised
    by *reporter_scope*. Returns ``(directive_item, evidence_row)``."""
    rs = RecordStore(db_path)
    item = operator_publish(
        attachment_scope,
        "Never deploy on a Friday.",
        "deploy-window",
        record_store=rs,
        summaries_dir=summaries_dir,
        fleet=fleet,
    )
    outcome_id = rs.append_contribution(
        scope_id=reporter_scope,
        content="Deployed on Friday; it broke.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=_contributor(reporter_scope),
    ).id
    rs.record_judgment_and_raise(
        contribution_id=outcome_id,
        decision="accept_as_context",
        judged_by="scope-manager",
        raise_operator_evidence=OperatorEvidenceInput(
            operator_item_id=item.id,
            raised_from=outcome_id,
            reporter=_contributor(reporter_scope),
            content=f"evidence from {reporter_scope}: following {item.id} went wrong: it broke.",
        ),
    )
    (evidence,) = rs.list_operator_evidence(operator_item_id=item.id)
    rs.close()
    return item, evidence


# --- GET /scopes/{scope_id}/operator-evidence ------------------------------------------


def test_operator_evidence_endpoint_shape(client) -> None:
    fleet = _fleet(client)
    item, evidence = _seed_evidence(
        client.db_path,
        client.summaries_dir,
        fleet=fleet,
        attachment_scope="g_root",
        reporter_scope="g_child",
    )

    resp = client.get("/scopes/g_root/operator-evidence")
    assert resp.status_code == 200
    body = resp.json()
    assert "evidence" in body
    assert len(body["evidence"]) == 1
    row = body["evidence"][0]
    assert row["id"] == evidence.id
    assert row["operator_item_id"] == item.id
    assert row["reporter_scope_id"] == "g_child"
    assert row["reporter_skill"] == "engineer"
    assert row["reporter_session_id"] == "sess_test"
    assert "created_at" in row
    assert row["content"] == evidence.content
    assert row["seen_at"] is None


def test_operator_evidence_endpoint_is_scoped_to_the_attachment_scope(client) -> None:
    fleet = _fleet(client)
    _seed_evidence(
        client.db_path,
        client.summaries_dir,
        fleet=fleet,
        attachment_scope="g_root",
        reporter_scope="g_child",
    )

    resp = client.get("/scopes/g_child/operator-evidence")
    assert resp.status_code == 200
    assert resp.json()["evidence"] == []


def test_operator_evidence_endpoint_404s_for_an_unknown_scope(client) -> None:
    resp = client.get("/scopes/g_nonexistent/operator-evidence")
    assert resp.status_code == 404


def test_operator_evidence_endpoint_excludes_seen_by_default_includes_with_all(client) -> None:
    fleet = _fleet(client)
    _item, evidence = _seed_evidence(
        client.db_path,
        client.summaries_dir,
        fleet=fleet,
        attachment_scope="g_root",
        reporter_scope="g_child",
    )
    with RecordStore(client.db_path) as rs:
        rs.mark_operator_evidence_seen(evidence.id, seen_at="2026-09-26T12:00:00Z")

    resp = client.get("/scopes/g_root/operator-evidence")
    assert resp.status_code == 200
    assert resp.json()["evidence"] == []

    resp = client.get("/scopes/g_root/operator-evidence?all=true")
    assert resp.status_code == 200
    body = resp.json()["evidence"]
    assert len(body) == 1
    assert body[0]["seen_at"] == "2026-09-26T12:00:00Z"


# --- POST /operator-evidence/{evidence_id}/seen ----------------------------------------


def test_reading_the_evidence_endpoint_never_marks_it_seen(client) -> None:
    """Opening or reading NEVER sets seen_at — only the explicit mark-seen act does."""
    fleet = _fleet(client)
    _item, evidence = _seed_evidence(
        client.db_path,
        client.summaries_dir,
        fleet=fleet,
        attachment_scope="g_root",
        reporter_scope="g_child",
    )

    for _ in range(3):
        resp = client.get("/scopes/g_root/operator-evidence")
        assert resp.json()["evidence"][0]["seen_at"] is None

    with RecordStore(client.db_path) as rs:
        (row,) = rs.list_operator_evidence(operator_item_id=_item.id)
        assert row.seen_at is None


def test_mark_seen_endpoint_marks_it_seen(client) -> None:
    fleet = _fleet(client)
    _item, evidence = _seed_evidence(
        client.db_path,
        client.summaries_dir,
        fleet=fleet,
        attachment_scope="g_root",
        reporter_scope="g_child",
    )

    resp = client.post(f"/operator-evidence/{evidence.id}/seen")
    assert resp.status_code == 200
    assert resp.json() == {"evidence_id": evidence.id, "seen": True}

    with RecordStore(client.db_path) as rs:
        (row,) = rs.list_operator_evidence(operator_item_id=_item.id)
        assert row.seen_at is not None

    resp = client.get("/scopes/g_root/operator-evidence")
    assert resp.json()["evidence"] == []


def test_mark_seen_endpoint_404s_for_an_unknown_evidence_id(client) -> None:
    resp = client.post("/operator-evidence/oev_doesnotexist/seen")
    assert resp.status_code == 404
