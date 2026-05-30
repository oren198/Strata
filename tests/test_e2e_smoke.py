"""End-to-end smoke test for the Strata V1.2 backend.

Exercises the full vertical slice in a single coherent narrative:
  fleet config load → contribute → judge → summary write

The scope-manager's ``judge`` call is intercepted via
``app.dependency_overrides[get_scope_manager]``.  No real Anthropic API calls
are made.  All storage (SQLite record store + markdown summary files) uses real
tmp paths created by pytest's ``tmp_path`` fixture.

Vocabulary follows CONTEXT.md verbatim: contribution, directive, context,
scope summary, record, supersession, stratum.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager
from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.record_store import RecordStore
from strata.scope_manager import ScopeManager, ScopeManagerJudgment
from strata.settings import Settings
from strata.summary_store import Directive, ScopeSummary, SummaryStore

# ---------------------------------------------------------------------------
# Fleet config (uses the example in the repo root, updated for V1.2 schema)
# ---------------------------------------------------------------------------

_FLEET_YAML = Path(__file__).parent.parent / "fleet.example.yaml"

# ---------------------------------------------------------------------------
# Contributor stub
# ---------------------------------------------------------------------------

_CONTRIBUTOR = {
    "scope_id": "g_arch",
    "skill": "architect",
    "session_id": "sess_smoke_001",
    "ts": "2026-05-23T10:00:00Z",
}

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _judgment(
    decision: str,
    *,
    summary: ScopeSummary | None,
    reasoning: str = "test",
) -> ScopeManagerJudgment:
    return ScopeManagerJudgment(
        decision=decision,  # type: ignore[arg-type]
        reasoning=reasoning,
        new_summary=summary,
    )


def _make_summary(
    scope_id: str,
    directives: list[Directive],
    context: str = "",
) -> ScopeSummary:
    return ScopeSummary(
        scope_id=scope_id,
        directives=directives,
        context=context,
        updated_at=datetime.now(tz=UTC).isoformat(),
    )


def _directive(content: str, subject: str | None = None, id: str = "d_smoke01") -> Directive:
    return Directive(
        id=id,
        content=content,
        subject=subject,
        source_scope_id="g_arch",
        source_skill="architect",
        created_at=datetime.now(tz=UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# Narrative smoke test
# ---------------------------------------------------------------------------


def test_e2e_full_loop(tmp_path):
    """Full vertical-slice smoke: fleet config load → contribute → judge → summary write.

    Exercises the complete V1.2 wiring in one coherent narrative sequence.
    The scope-manager boundary is mocked; all storage paths are real on-disk
    tmp files cleaned up automatically by pytest.
    """
    db_path = str(tmp_path / "smoke.db")
    summaries_dir = str(tmp_path / "summaries")
    fleet_yaml_path = str(_FLEET_YAML)

    # ------------------------------------------------------------------
    # Step 1: Validate fleet config loads cleanly.
    # ------------------------------------------------------------------
    run_migrations(db_path)
    config = FleetConfig.load(_FLEET_YAML)

    assert len(config.strata) >= 3
    assert len(config.scopes) >= 4
    assert any(s.id == "g_arch" for s in config.scopes)

    # ------------------------------------------------------------------
    # Step 2: Build the app with the fleet config.
    # ------------------------------------------------------------------
    settings = Settings(
        db_path=db_path,
        summaries_dir=summaries_dir,
        fleet_yaml_path=fleet_yaml_path,
        manager_model="claude-haiku-4-5",
        anthropic_api_key="test-key-smoke",
    )
    app = create_app(settings=settings)
    mock_manager = MagicMock(spec=ScopeManager)
    app.dependency_overrides[get_scope_manager] = lambda: mock_manager

    with TestClient(app) as client:
        # ------------------------------------------------------------------
        # Step 3: GET /scopes — verify the fleet is visible (active scopes only).
        # ------------------------------------------------------------------
        resp = client.get("/scopes")
        assert resp.status_code == 200
        fleet = resp.json()
        assert len(fleet["strata"]) >= 3
        active_ids = {s["id"] for s in fleet["scopes"]}
        assert "g_arch" in active_ids

        # ------------------------------------------------------------------
        # Step 4: First contribution — accepted as directive.
        # ------------------------------------------------------------------
        grpc_v1_content = "all RPCs use gRPC"
        grpc_v1_subject = "rpc-protocol"
        grpc_v1_summary = _make_summary(
            "g_arch",
            directives=[_directive(grpc_v1_content, subject=grpc_v1_subject, id="d_smoke01")],
        )
        mock_manager.judge.return_value = _judgment("accept_as_directive", summary=grpc_v1_summary)

        resp = client.post(
            "/contribute",
            json={
                "scope_id": "g_arch",
                "content": grpc_v1_content,
                "proposed_classification": "directive",
                "subject": grpc_v1_subject,
                "supersedes": None,
                "contributor": _CONTRIBUTOR,
            },
        )
        assert resp.status_code == 200
        resp_data = resp.json()
        assert resp_data["judgment"]["summary_updated"] is True
        assert resp_data["judgment"]["decision"] == "accept_as_directive"

        contribution_1_id = resp_data["contribution_id"]

        with RecordStore(db_path) as rs:
            contribs = rs.list_contributions(scope_id="g_arch")
            assert any(c.id == contribution_1_id for c in contribs)

            judgments = rs.list_judgments(scope_id="g_arch")
            assert any(
                j.contribution_id == contribution_1_id and j.decision == "accept_as_directive"
                for j in judgments
            )

        summary_path = Path(summaries_dir) / "g_arch.md"
        assert summary_path.exists()

        resp = client.get("/scopes/g_arch/summary")
        assert resp.status_code == 200
        summary_payload = resp.json()
        assert summary_payload["scope_id"] == "g_arch"
        directives = summary_payload["directives"]
        assert len(directives) == 1
        assert directives[0]["content"] == grpc_v1_content

        # ------------------------------------------------------------------
        # Step 5: Second contribution — supersession.
        # ------------------------------------------------------------------
        grpc_v2_content = "all RPCs use gRPC v1.60+"
        grpc_v2_summary = _make_summary(
            "g_arch",
            directives=[_directive(grpc_v2_content, subject=grpc_v1_subject, id="d_smoke02")],
        )
        mock_manager.judge.return_value = _judgment("accept_as_directive", summary=grpc_v2_summary)

        resp = client.post(
            "/contribute",
            json={
                "scope_id": "g_arch",
                "content": grpc_v2_content,
                "proposed_classification": "directive",
                "subject": grpc_v1_subject,
                "supersedes": contribution_1_id,
                "contributor": _CONTRIBUTOR,
            },
        )
        assert resp.status_code == 200

        resp = client.get("/scopes/g_arch/summary")
        assert resp.status_code == 200
        summary_payload = resp.json()
        directives = summary_payload["directives"]
        assert len(directives) == 1
        assert directives[0]["content"] == grpc_v2_content
        assert all(d["content"] != grpc_v1_content for d in directives)

        with RecordStore(db_path) as rs:
            contribs = rs.list_contributions(scope_id="g_arch")
            assert len(contribs) == 2
            judgments = rs.list_judgments(scope_id="g_arch")
            assert len(judgments) == 2

        # ------------------------------------------------------------------
        # Step 6: Third contribution — declined.
        # ------------------------------------------------------------------
        mock_manager.judge.return_value = _judgment("decline", summary=None)

        resp = client.post(
            "/contribute",
            json={
                "scope_id": "g_arch",
                "content": "random thought, please ignore",
                "proposed_classification": "context",
                "contributor": _CONTRIBUTOR,
            },
        )
        assert resp.status_code == 200
        resp_data = resp.json()
        assert resp_data["judgment"]["summary_updated"] is False
        assert resp_data["judgment"]["decision"] == "decline"

        contribution_3_id = resp_data["contribution_id"]

        with RecordStore(db_path) as rs:
            contribs = rs.list_contributions(scope_id="g_arch")
            assert any(c.id == contribution_3_id for c in contribs)

            judgments = rs.list_judgments(scope_id="g_arch")
            assert any(
                j.contribution_id == contribution_3_id and j.decision == "decline"
                for j in judgments
            )

        ss = SummaryStore(summaries_dir)
        stored = ss.read("g_arch")
        assert stored is not None
        assert len(stored.directives) == 1
        assert stored.directives[0].content == grpc_v2_content

        # ------------------------------------------------------------------
        # Step 7: GET /scopes/g_arch/record — 3 contributions, 3 judgments.
        # ------------------------------------------------------------------
        resp = client.get("/scopes/g_arch/record")
        assert resp.status_code == 200
        record_payload = resp.json()
        assert len(record_payload["contributions"]) == 3
        assert len(record_payload["judgments"]) == 3

        # ------------------------------------------------------------------
        # Step 8: Final invariants — active scopes still visible.
        # ------------------------------------------------------------------
        resp = client.get("/scopes")
        assert resp.status_code == 200
        final_fleet = resp.json()
        final_scope_ids = {s["id"] for s in final_fleet["scopes"]}
        for expected_id in ("g_arch",):
            assert expected_id in final_scope_ids

        # ------------------------------------------------------------------
        # Step 9: Non-existent scope → 404 scope_not_found.
        # ------------------------------------------------------------------
        resp = client.post(
            "/contribute",
            json={
                "scope_id": "g_does_not_exist",
                "content": "x",
                "proposed_classification": "directive",
                "contributor": _CONTRIBUTOR,
            },
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "scope_not_found"


# ---------------------------------------------------------------------------
# Companion test: FleetConfig loads cleanly from fleet.example.yaml
# ---------------------------------------------------------------------------


def test_e2e_fleet_config_loads(tmp_path):
    """fleet.example.yaml must load and validate without error via FleetConfig."""
    config = FleetConfig.load(_FLEET_YAML)
    assert len(config.strata) >= 3
    assert len(config.scopes) >= 4
    assert len(config.edges) >= 3
    assert all(s.status in ("active", "archived") for s in config.scopes)
