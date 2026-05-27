"""API-level tests for the Strata FastAPI application.

All scope-manager calls are mocked — no real Anthropic API calls are made.
The record store and summary store use real tmp paths.
Fleet configuration is backed by a real fleet.yaml on disk.

Tests cover the scenarios specified in the task brief plus the new
FleetConfig-backed validation (scope_not_found, scope_not_active).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager
from strata.migrator import run_migrations
from strata.record_store import RecordStore
from strata.scope_manager import ScopeManager, ScopeManagerJudgment
from strata.settings import Settings
from strata.settings import get_settings as _get_settings
from strata.summary_store import Directive, ScopeSummary, SummaryStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONTRIBUTOR_BODY = {
    "scope_id": "g_seed01",
    "skill": "architect",
    "session_id": "sess_001",
    "ts": "2026-05-23T20:00:00Z",
}

_FLEET_YAML = textwrap.dedent("""
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

    edges:
      - from: g_active
        to: g_archived_parent_not_used
""").strip()

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


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListScopes:
    """GET /scopes"""

    def test_returns_only_active_scopes(self, client):
        """GET /scopes must return only active scopes (not archived)."""
        resp = client.get("/scopes")
        assert resp.status_code == 200
        data = resp.json()
        scope_ids = {s["id"] for s in data["scopes"]}
        assert "g_active" in scope_ids
        assert "g_archived" not in scope_ids, "archived scopes must not appear in /scopes"

    def test_empty_fleet_returns_empty_lists(self, tmp_path):
        """GET /scopes on empty fleet.yaml returns empty lists."""
        db_path = str(tmp_path / "empty.db")
        summaries_dir = str(tmp_path / "summaries")
        fleet_yaml_path = str(tmp_path / "fleet.yaml")

        run_migrations(db_path)
        (tmp_path / "fleet.yaml").write_text(
            "strata: []\nscopes: []\nedges: []\n", encoding="utf-8"
        )

        settings = Settings(
            db_path=db_path,
            summaries_dir=summaries_dir,
            fleet_yaml_path=fleet_yaml_path,
            anthropic_api_key="test-key",
        )
        application = create_app(settings=settings)
        application.dependency_overrides[get_scope_manager] = lambda: MagicMock(spec=ScopeManager)

        with TestClient(application) as tc:
            resp = tc.get("/scopes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["strata"] == []
        assert data["scopes"] == []
        assert data["edges"] == []


class TestScopeSummary:
    """GET /scopes/{scope_id}/summary"""

    def test_missing_scope_returns_404(self, client):
        resp = client.get("/scopes/g_nonexistent/summary")
        assert resp.status_code == 404

    def test_scope_with_no_summary_returns_empty(self, client):
        resp = client.get("/scopes/g_active/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope_id"] == "g_active"
        assert data["directives"] == []
        assert data["context"] == ""

    def test_summary_after_accept_returns_content(self, client):
        resp = client.post(
            "/contribute",
            json={
                "scope_id": "g_active",
                "content": "use gRPC, not REST",
                "proposed_classification": "directive",
                "subject": "rpc-protocol",
                "supersedes": None,
                "contributor": _CONTRIBUTOR_BODY,
            },
        )
        assert resp.status_code == 200

        resp = client.get("/scopes/g_active/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope_id"] == "g_active"
        directives = data["directives"]
        assert len(directives) == 1
        assert directives[0]["content"] == "use gRPC, not REST"


class TestContribute:
    """POST /contribute"""

    def test_missing_scope_returns_404_scope_not_found(self, client):
        """Invariant 9: scope not in FleetConfig → 404 scope_not_found."""
        resp = client.post(
            "/contribute",
            json={
                "scope_id": "g_does_not_exist",
                "content": "any content",
                "proposed_classification": "directive",
                "contributor": _CONTRIBUTOR_BODY,
            },
        )
        assert resp.status_code == 404
        data = resp.json()
        assert data["detail"]["error"] == "scope_not_found"

    def test_archived_scope_returns_409_scope_not_active(self, client):
        """Invariant 10: archived scope → 409 scope_not_active."""
        resp = client.post(
            "/contribute",
            json={
                "scope_id": "g_archived",
                "content": "any content",
                "proposed_classification": "directive",
                "contributor": _CONTRIBUTOR_BODY,
            },
        )
        assert resp.status_code == 409
        data = resp.json()
        assert data["detail"]["error"] == "scope_not_active"

    def test_accept_as_directive(self, client):
        """accept_as_directive — 200, summary_updated=True, file created."""
        scope_id = "g_active"
        summary = _make_summary(scope_id, "accept_as_directive")
        client.mock_manager.judge.return_value = _make_judgment(
            decision="accept_as_directive",
            summary=summary,
        )

        resp = client.post(
            "/contribute",
            json={
                "scope_id": scope_id,
                "content": "use gRPC, not REST",
                "proposed_classification": "directive",
                "subject": "rpc-protocol",
                "supersedes": None,
                "contributor": _CONTRIBUTOR_BODY,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["judgment"]["decision"] == "accept_as_directive"
        assert data["judgment"]["summary_updated"] is True

        contribution_id = data["contribution_id"]

        summary_path = Path(client.summaries_dir) / f"{scope_id}.md"
        assert summary_path.exists()

        with RecordStore(client.db_path) as rs:
            contributions = rs.list_contributions(scope_id=scope_id)
            assert any(c.id == contribution_id for c in contributions)

    def test_accept_as_context(self, client):
        """accept_as_context — 200, summary_updated=True."""
        scope_id = "g_active"
        summary = _make_summary(scope_id, "accept_as_context")
        client.mock_manager.judge.return_value = _make_judgment(
            decision="accept_as_context",
            summary=summary,
        )

        resp = client.post(
            "/contribute",
            json={
                "scope_id": scope_id,
                "content": "gRPC preferred for inter-service calls.",
                "proposed_classification": "context",
                "contributor": _CONTRIBUTOR_BODY,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["judgment"]["decision"] == "accept_as_context"
        assert data["judgment"]["summary_updated"] is True

    def test_decline(self, client):
        """decline — summary_updated=False, contribution recorded."""
        scope_id = "g_active"
        client.mock_manager.judge.return_value = _make_judgment(
            decision="decline",
            summary=None,
        )

        resp = client.post(
            "/contribute",
            json={
                "scope_id": scope_id,
                "content": "this should be declined",
                "proposed_classification": "context",
                "contributor": _CONTRIBUTOR_BODY,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["judgment"]["decision"] == "decline"
        assert data["judgment"]["summary_updated"] is False

    def test_scope_manager_raises_returns_500(self, client):
        """scope-manager raises — 500 with error key."""
        scope_id = "g_active"
        client.mock_manager.judge.side_effect = ValueError("LLM unavailable")

        resp = client.post(
            "/contribute",
            json={
                "scope_id": scope_id,
                "content": "contribution before crash",
                "proposed_classification": "directive",
                "contributor": _CONTRIBUTOR_BODY,
            },
        )
        assert resp.status_code == 500
        assert "scope_manager_failure" in str(resp.json())


class TestScopeRecord:
    """GET /scopes/{scope_id}/record"""

    def test_missing_scope_returns_404(self, client):
        resp = client.get("/scopes/g_nonexistent/record")
        assert resp.status_code == 404

    def test_record_returns_contributions_and_judgments(self, client):
        scope_id = "g_active"

        for i in range(2):
            summary = _make_summary(scope_id, "accept_as_directive")
            client.mock_manager.judge.return_value = _make_judgment(
                decision="accept_as_directive",
                summary=summary,
            )
            resp = client.post(
                "/contribute",
                json={
                    "scope_id": scope_id,
                    "content": f"contribution {i}",
                    "proposed_classification": "directive",
                    "contributor": _CONTRIBUTOR_BODY,
                },
            )
            assert resp.status_code == 200

        resp = client.get(f"/scopes/{scope_id}/record")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["contributions"]) == 2
        assert len(data["judgments"]) == 2


class TestSettings:
    """Settings env-var override."""

    def test_settings_env_override(self, tmp_path, monkeypatch):
        """STRATA_DB_PATH env var flows through to settings."""
        expected = str(tmp_path / "override.db")
        monkeypatch.setenv("STRATA_DB_PATH", expected)

        _get_settings.cache_clear()
        try:
            s = _get_settings()
            assert s.db_path == expected
        finally:
            _get_settings.cache_clear()
