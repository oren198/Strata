"""API-level tests for GET /scopes/{scope_id}/declines — the Console's
"turned down" proof surface (UI-only endpoint; no engine flow depends on it).

The client fixture (and the fleet.yaml it seeds) are copied verbatim from
tests/test_app.py so no live judge is called.
"""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager
from strata.migrator import run_migrations
from strata.scope_manager import ScopeManager, ScopeManagerJudgment
from strata.settings import Settings
from strata.summary_store import Directive, ScopeSummary

# Simpler valid fleet with just one active scope.
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
      - id: g_archived
        name: Archived Scope
        stratum_id: L1
        status: archived

    edges: []
""").strip()


def _make_judgment(
    decision: str = "accept_as_directive",
    reasoning: str = "Test reasoning.",
    summary: ScopeSummary | None = None,
) -> ScopeManagerJudgment:
    return ScopeManagerJudgment(
        decision=decision,  # type: ignore[arg-type]
        reasoning=reasoning,
        new_summary=summary,
    )


def _make_summary(scope_id: str, decision: str) -> ScopeSummary:
    if decision == "accept_as_directive":
        return ScopeSummary(
            scope_id=scope_id,
            directives=[
                Directive(
                    id="c_000001",
                    content="use gRPC, not REST",
                    subject="rpc-protocol",
                    source_scope_id=scope_id,
                    source_skill="architect",
                    created_at="2026-05-23T20:00:00+00:00",
                )
            ],
            context="",
            updated_at="2026-05-23T20:00:01+00:00",
        )
    return ScopeSummary(
        scope_id=scope_id,
        directives=[],
        context="gRPC preferred for inter-service calls.",
        updated_at="2026-05-23T20:00:01+00:00",
    )


@pytest.fixture()
def client(tmp_path):
    """Yield a TestClient backed by a fresh DB + FleetConfig from a tmp fleet.yaml."""
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
    mock_manager.judge.return_value = _make_judgment(
        decision="accept_as_directive",
        summary=_make_summary("g_active", "accept_as_directive"),
    )

    application.dependency_overrides[get_scope_manager] = lambda: mock_manager

    with TestClient(application) as tc:
        tc.scope_id = "g_active"  # type: ignore[attr-defined]
        tc.mock_manager = mock_manager  # type: ignore[attr-defined]
        tc.summaries_dir = summaries_dir  # type: ignore[attr-defined]
        tc.db_path = db_path  # type: ignore[attr-defined]
        yield tc


def test_declines_endpoint_returns_judge_reasons(client):
    from strata.record_store import ContributorRef, RecordStore

    with RecordStore(client.db_path) as store:
        c = store.append_contribution(
            scope_id="g_active", content="use REST",
            proposed_classification="directive", subject="rpc",
            supersedes=None,
            contributor=ContributorRef(scope_id="g_active", skill="architect",
                                       session_id="sess_1",
                                       ts="2026-08-20T10:00:00+00:00"),
        )
        store.record_judgment(contribution_id=c.id, decision="decline",
                              judged_by="scope-manager",
                              notes="Contradicts the standing gRPC directive.")

    resp = client.get("/scopes/g_active/declines")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope_id"] == "g_active"
    assert len(body["declines"]) == 1
    entry = body["declines"][0]
    assert entry["contribution_id"] == c.id
    assert entry["content"] == "use REST"
    assert entry["reason"] == "Contradicts the standing gRPC directive."
    assert entry["judged_by"] == "scope-manager"
    assert entry["contributor"]["session_id"] == "sess_1"
    assert body["page"]["total"] == 1
    assert body["page"]["next_before_id"] is None


def test_declines_endpoint_omits_accepted(client):
    from strata.record_store import ContributorRef, RecordStore

    with RecordStore(client.db_path) as store:
        c = store.append_contribution(
            scope_id="g_active", content="use gRPC",
            proposed_classification="directive", subject=None, supersedes=None,
            contributor=ContributorRef(scope_id="g_active", skill="architect",
                                       session_id="s", ts="2026-08-20T10:00:00+00:00"),
        )
        store.record_judgment(contribution_id=c.id, decision="accept_as_directive",
                              judged_by="scope-manager", notes="Good.")

    body = client.get("/scopes/g_active/declines").json()
    assert body["declines"] == []
    assert body["page"]["total"] == 0


def test_declines_endpoint_reports_null_reason_as_null(client):
    from strata.record_store import ContributorRef, RecordStore

    with RecordStore(client.db_path) as store:
        c = store.append_contribution(
            scope_id="g_active", content="vague", proposed_classification="context",
            subject=None, supersedes=None,
            contributor=ContributorRef(scope_id="g_active", skill=None,
                                       session_id="s", ts="2026-08-20T10:00:00+00:00"),
        )
        store.record_judgment(contribution_id=c.id, decision="decline",
                              judged_by="scope-manager", notes=None)

    body = client.get("/scopes/g_active/declines").json()
    assert body["declines"][0]["reason"] is None


def test_declines_endpoint_unknown_scope_is_404(client):
    resp = client.get("/scopes/g_nope/declines")
    assert resp.status_code == 404


def test_declines_endpoint_bad_cursor_is_422(client):
    resp = client.get("/scopes/g_active/declines", params={"before_id": "c_nope"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_page"


def test_declines_endpoint_reports_mechanical_decline_counter(client, tmp_path):
    """Mechanical 'nothing to record' closeouts surface as a per-scope COUNT, never entries."""
    from strata.session_state import SessionStateStore, sessions_dir_for

    sessions = SessionStateStore(sessions_dir_for(client.summaries_dir))
    sessions.record_read("sess_a", "g_active")
    sessions.record_decline("sess_a")
    sessions.record_read("sess_b", "g_active")   # read, no decline
    sessions.record_read("sess_c", "g_other")
    sessions.record_decline("sess_c")            # declined, never read g_active

    body = client.get("/scopes/g_active/declines").json()
    assert body["mechanical_declines"]["sessions_that_read_and_recorded_nothing"] == 1
    assert body["mechanical_declines"]["window_days"] == 30
    # Never a record entry:
    assert all("mechanical" not in d for d in body["declines"])
