"""API-level tests for the Strata FastAPI application.

All scope-manager calls are mocked — no real Anthropic API calls are made.
The record store and summary store use real tmp paths.

Tests cover the 11 scenarios specified in the Task 5 brief.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from scripts.run_migrations import run_migrations
from strata.app import create_app, get_scope_manager
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


def _make_judgment(
    decision: str = "accept_as_directive",
    reasoning: str = "Test reasoning.",
    summary: ScopeSummary | None = None,
) -> ScopeManagerJudgment:
    """Build a :class:`ScopeManagerJudgment` for use in mocks."""
    return ScopeManagerJudgment(
        decision=decision,  # type: ignore[arg-type]
        reasoning=reasoning,
        new_summary=summary,
    )


def _make_summary(scope_id: str, decision: str) -> ScopeSummary:
    """Build a minimal :class:`ScopeSummary` appropriate for *decision*."""
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
    # accept_as_context
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
    """Yield a TestClient backed by a fresh DB + summaries dir.

    The scope-manager dependency is overridden with a MagicMock whose
    ``judge`` method callers can configure per-test.
    """
    db_path = str(tmp_path / "test.db")
    summaries_dir = str(tmp_path / "summaries")

    # Apply migrations to a fresh DB
    run_migrations(db_path)

    # Seed a stratum and scope so most tests have something to work with
    with RecordStore(db_path) as rs:
        stratum = rs.create_stratum(name="function", ordinal=1)
        scope = rs.create_scope(name="Architecture", stratum_id=stratum.id)

    settings = Settings(
        db_path=db_path,
        summaries_dir=summaries_dir,
        manager_model="claude-haiku-4-5",
        anthropic_api_key="test-key",
    )

    application = create_app(settings=settings)

    # Default mock: accept_as_directive with an appropriate summary
    mock_manager = MagicMock(spec=ScopeManager)
    mock_manager.judge.return_value = _make_judgment(
        decision="accept_as_directive",
        summary=_make_summary(scope.id, "accept_as_directive"),
    )

    application.dependency_overrides[get_scope_manager] = lambda: mock_manager

    with TestClient(application) as tc:
        tc.scope_id = scope.id  # type: ignore[attr-defined]
        tc.mock_manager = mock_manager  # type: ignore[attr-defined]
        tc.summaries_dir = summaries_dir  # type: ignore[attr-defined]
        tc.db_path = db_path  # type: ignore[attr-defined]
        yield tc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListScopes:
    """GET /scopes"""

    def test_empty_fleet_returns_empty_lists(self, tmp_path):
        """Scenario 1: GET /scopes on empty fleet returns empty lists."""
        db_path = str(tmp_path / "empty.db")
        summaries_dir = str(tmp_path / "summaries")
        run_migrations(db_path)

        settings = Settings(
            db_path=db_path,
            summaries_dir=summaries_dir,
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

    def test_populated_fleet_returns_data(self, client):
        """GET /scopes returns the seeded stratum and scope."""
        resp = client.get("/scopes")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["strata"]) == 1
        assert len(data["scopes"]) == 1
        assert data["edges"] == []


class TestScopeSummary:
    """GET /scopes/{scope_id}/summary"""

    def test_missing_scope_returns_404(self, client):
        """Scenario 2: GET /scopes/{id}/summary on missing scope — 404."""
        resp = client.get("/scopes/g_nonexistent/summary")
        assert resp.status_code == 404

    def test_scope_with_no_summary_returns_empty(self, client):
        """Scenario 9: scope exists but has no summary yet — 200, empty."""
        resp = client.get(f"/scopes/{client.scope_id}/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope_id"] == client.scope_id
        assert data["directives"] == []
        assert data["context"] == ""

    def test_summary_after_accept_returns_content(self, client):
        """Scenario 8: GET /scopes/{id}/summary after accept — round-trip."""
        # Post a contribution that gets accepted
        resp = client.post(
            "/contribute",
            json={
                "scope_id": client.scope_id,
                "content": "use gRPC, not REST",
                "proposed_classification": "directive",
                "subject": "rpc-protocol",
                "supersedes": None,
                "contributor": _CONTRIBUTOR_BODY,
            },
        )
        assert resp.status_code == 200

        # Fetch the summary
        resp = client.get(f"/scopes/{client.scope_id}/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope_id"] == client.scope_id
        directives = data["directives"]
        assert len(directives) == 1
        assert directives[0]["content"] == "use gRPC, not REST"
        assert directives[0]["subject"] == "rpc-protocol"


class TestContribute:
    """POST /contribute"""

    def test_missing_scope_returns_404(self, client):
        """Scenario 3: POST /contribute on missing scope — 404."""
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

    def test_accept_as_directive(self, client):
        """Scenario 4: accept_as_directive — 200, summary_updated=True, file created."""
        scope_id = client.scope_id
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

        # Verify summary file was written
        import pathlib

        summary_path = pathlib.Path(client.summaries_dir) / f"{scope_id}.md"
        assert summary_path.exists(), "Summary file should have been created"

        # Verify contribution is in the record store
        with RecordStore(client.db_path) as rs:
            contributions = rs.list_contributions(scope_id=scope_id)
            assert any(c.id == contribution_id for c in contributions)

            judgments = rs.list_judgments(scope_id=scope_id)
            assert any(j.contribution_id == contribution_id for j in judgments)
            judgment = next(j for j in judgments if j.contribution_id == contribution_id)
            assert judgment.decision == "accept_as_directive"

    def test_accept_as_context(self, client):
        """Scenario 5: accept_as_context — 200, summary_updated=True, context reflected."""
        scope_id = client.scope_id
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

        # Verify summary reflects context
        ss = SummaryStore(client.summaries_dir)
        stored = ss.read(scope_id)
        assert stored is not None
        assert "gRPC" in stored.context

    def test_decline(self, client):
        """Scenario 6: decline — summary_updated=False, no summary file, contribution recorded."""
        scope_id = client.scope_id
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

        contribution_id = data["contribution_id"]

        # Verify summary file was NOT created
        import pathlib

        summary_path = pathlib.Path(client.summaries_dir) / f"{scope_id}.md"
        assert not summary_path.exists(), "Summary file should not have been created on decline"

        # Verify contribution IS in the record
        with RecordStore(client.db_path) as rs:
            contributions = rs.list_contributions(scope_id=scope_id)
            assert any(c.id == contribution_id for c in contributions)

            # Verify decline judgment recorded
            judgments = rs.list_judgments(scope_id=scope_id)
            assert any(j.contribution_id == contribution_id for j in judgments)
            judgment = next(j for j in judgments if j.contribution_id == contribution_id)
            assert judgment.decision == "decline"

    def test_scope_manager_raises_returns_500(self, client):
        """Scenario 7: scope-manager raises — 500 with error key, contribution still recorded."""
        scope_id = client.scope_id
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
        data = resp.json()
        # FastAPI wraps HTTPException detail in {"detail": ...}
        assert "scope_manager_failure" in str(data)

        # Summary file should NOT exist
        import pathlib

        summary_path = pathlib.Path(client.summaries_dir) / f"{scope_id}.md"
        assert not summary_path.exists()

        # Contribution IS in the record (appended before the judge call)
        with RecordStore(client.db_path) as rs:
            contributions = rs.list_contributions(scope_id=scope_id)
            assert len(contributions) == 1
            assert contributions[0].content == "contribution before crash"


class TestScopeRecord:
    """GET /scopes/{scope_id}/record"""

    def test_missing_scope_returns_404(self, client):
        """GET /scopes/{id}/record on missing scope — 404."""
        resp = client.get("/scopes/g_nonexistent/record")
        assert resp.status_code == 404

    def test_record_returns_contributions_and_judgments(self, client):
        """Scenario 10: after two contributions, lists have length 2 each."""
        scope_id = client.scope_id

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
        """Scenario 11: STRATA_DB_PATH env var flows through to settings."""
        expected = str(tmp_path / "override.db")
        monkeypatch.setenv("STRATA_DB_PATH", expected)

        # Clear the lru_cache so the env var is picked up
        _get_settings.cache_clear()
        try:
            s = _get_settings()
            assert s.db_path == expected
        finally:
            _get_settings.cache_clear()
