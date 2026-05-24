"""End-to-end smoke test for the Strata V1 backend.

Exercises the full vertical slice in a single coherent narrative:
  bootstrap → contribute → judge → summary write

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

from scripts.run_migrations import run_migrations
from strata.app import create_app, get_scope_manager
from strata.bootstrap import apply_fleet_config, load_fleet_config
from strata.record_store import RecordStore
from strata.scope_manager import ScopeManager, ScopeManagerJudgment
from strata.settings import Settings
from strata.summary_store import Directive, ScopeSummary, SummaryStore

# ---------------------------------------------------------------------------
# Fleet config path (the canonical example bundled in the repo root)
# ---------------------------------------------------------------------------

_FLEET_YAML = Path(__file__).parent.parent / "fleet.example.yaml"

# ---------------------------------------------------------------------------
# Contributor stub — used in every POST /contribute request body
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
    """Construct a :class:`ScopeManagerJudgment` for use in mock return values."""
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
    """Construct a :class:`ScopeSummary` with an up-to-date ``updated_at``."""
    return ScopeSummary(
        scope_id=scope_id,
        directives=directives,
        context=context,
        updated_at=datetime.now(tz=UTC).isoformat(),
    )


def _directive(content: str, subject: str | None = None, id: str = "d_smoke01") -> Directive:
    """Build a minimal :class:`Directive` for summary construction."""
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
    """Full vertical-slice smoke: bootstrap → contribute → judge → summary write.

    Exercises the complete V1 wiring in one coherent narrative sequence.
    The scope-manager boundary is mocked; all storage paths are real on-disk
    tmp files cleaned up automatically by pytest.
    """
    db_path = str(tmp_path / "smoke.db")
    summaries_dir = str(tmp_path / "summaries")

    # ------------------------------------------------------------------
    # Step 1: Bootstrap — apply fleet.example.yaml to a fresh DB.
    #
    # Asserts: 3 strata created, 4 scopes created, 4 edges created.
    # Asserts: canonical IDs from the YAML (g_arch, g_eng, …) are present —
    #          proves the create_scope ``id=`` parameter is wired correctly.
    # ------------------------------------------------------------------
    run_migrations(db_path)
    config = load_fleet_config(_FLEET_YAML)

    with RecordStore(db_path) as rs:
        result = apply_fleet_config(rs, config)

    assert len(result.strata_created) == 3, "Expected 3 strata from fleet.example.yaml"
    assert len(result.scopes_created) == 4, "Expected 4 scopes from fleet.example.yaml"
    assert len(result.edges_created) == 4, "Expected 4 edges from fleet.example.yaml"

    # Canonical scope IDs pinned in the YAML must be present in the DB.
    with RecordStore(db_path) as rs:
        scope_ids = {s.id for s in rs.list_scopes()}

    assert "g_arch" in scope_ids, "g_arch must be created by bootstrap"
    assert "g_eng" in scope_ids, "g_eng must be created by bootstrap"
    assert "g_ceo" in scope_ids, "g_ceo must be created by bootstrap"
    assert "g_backend" in scope_ids, "g_backend must be created by bootstrap"

    # ------------------------------------------------------------------
    # Step 2: Build the app with the bootstrapped DB.
    #
    # Override get_scope_manager with a MagicMock so no Anthropic API call
    # is made.  The mock's return value is configured per contribution below.
    # ------------------------------------------------------------------
    settings = Settings(
        db_path=db_path,
        summaries_dir=summaries_dir,
        manager_model="claude-haiku-4-5",
        anthropic_api_key="test-key-smoke",
    )
    app = create_app(settings=settings)
    mock_manager = MagicMock(spec=ScopeManager)
    app.dependency_overrides[get_scope_manager] = lambda: mock_manager

    with TestClient(app) as client:
        # ------------------------------------------------------------------
        # Step 3: GET /scopes — verify the bootstrapped fleet is visible.
        #
        # Asserts: 3 strata, 4 scopes, 4 edges in the response payload.
        # Asserts: g_arch appears in the scope list by ID.
        # ------------------------------------------------------------------
        resp = client.get("/scopes")
        assert resp.status_code == 200
        fleet = resp.json()
        assert len(fleet["strata"]) == 3, "GET /scopes must return 3 strata"
        assert len(fleet["scopes"]) == 4, "GET /scopes must return 4 scopes"
        assert len(fleet["edges"]) == 4, "GET /scopes must return 4 edges"
        assert any(s["id"] == "g_arch" for s in fleet["scopes"]), (
            "g_arch must appear in GET /scopes"
        )

        # ------------------------------------------------------------------
        # Step 4: First contribution — accepted as directive.
        #
        # The mock scope-manager returns accept_as_directive with a summary
        # containing exactly one directive: "all RPCs use gRPC".
        #
        # Asserts: 200, summary_updated=true.
        # Asserts: contribution appears in RecordStore.list_contributions.
        # Asserts: judgment with decision=accept_as_directive is in the record.
        # Asserts: summary markdown file exists on disk.
        # Asserts: GET /scopes/g_arch/summary returns directives list with
        #          the expected content.
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

        # Contribution is in the immutable record.
        with RecordStore(db_path) as rs:
            contribs = rs.list_contributions(scope_id="g_arch")
            assert any(c.id == contribution_1_id for c in contribs), (
                "First contribution must appear in record"
            )

            judgments = rs.list_judgments(scope_id="g_arch")
            assert any(
                j.contribution_id == contribution_1_id and j.decision == "accept_as_directive"
                for j in judgments
            ), "Judgment with accept_as_directive must be in record"

        # Summary markdown file written to disk.
        summary_path = Path(summaries_dir) / "g_arch.md"
        assert summary_path.exists(), "Summary file g_arch.md must exist after accepted directive"

        # GET /scopes/g_arch/summary reflects the accepted directive.
        resp = client.get("/scopes/g_arch/summary")
        assert resp.status_code == 200
        summary_payload = resp.json()
        assert summary_payload["scope_id"] == "g_arch"
        directives = summary_payload["directives"]
        assert len(directives) == 1, (
            "Summary must contain exactly one directive after first contribution"
        )
        assert directives[0]["content"] == grpc_v1_content

        # ------------------------------------------------------------------
        # Step 5: Second contribution — supersession.
        #
        # A new directive replaces the first one (same subject, updated spec).
        # The mock returns accept_as_directive with a summary that contains
        # only the new directive — the old one is removed (superseded).
        #
        # Asserts: 200, summary_updated=true.
        # Asserts: summary now has ONE directive — the new content.
        # Asserts: the old directive content no longer appears in the summary.
        # Asserts: RecordStore has TWO contributions and TWO judgments for g_arch.
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
        resp_data = resp.json()
        assert resp_data["judgment"]["summary_updated"] is True

        # Summary now shows only the new (superseding) directive.
        resp = client.get("/scopes/g_arch/summary")
        assert resp.status_code == 200
        summary_payload = resp.json()
        directives = summary_payload["directives"]
        assert len(directives) == 1, (
            "After supersession, summary must contain exactly one directive"
        )
        assert directives[0]["content"] == grpc_v2_content, (
            "Superseding directive content must appear"
        )
        # Old directive content must no longer be in the summary.
        assert all(d["content"] != grpc_v1_content for d in directives), (
            "Superseded directive must not appear in summary"
        )

        # Record has accumulated two contributions and two judgments.
        with RecordStore(db_path) as rs:
            contribs = rs.list_contributions(scope_id="g_arch")
            assert len(contribs) == 2, "g_arch record must have 2 contributions after two POSTs"
            judgments = rs.list_judgments(scope_id="g_arch")
            assert len(judgments) == 2, "g_arch record must have 2 judgments after two POSTs"

        # ------------------------------------------------------------------
        # Step 6: Third contribution — declined.
        #
        # The mock returns decline with new_summary=None.
        #
        # Asserts: 200, summary_updated=false.
        # Asserts: contribution is in the record (appended pre-judgment).
        # Asserts: a judgment with decision=decline is in the record.
        # Asserts: summary file is unchanged — still contains the v1.60+ directive.
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

        # Contribution is in the record even though it was declined.
        with RecordStore(db_path) as rs:
            contribs = rs.list_contributions(scope_id="g_arch")
            assert any(c.id == contribution_3_id for c in contribs), (
                "Declined contribution must still appear in record"
            )

            judgments = rs.list_judgments(scope_id="g_arch")
            assert any(
                j.contribution_id == contribution_3_id and j.decision == "decline"
                for j in judgments
            ), "Decline judgment must be in record"

        # Summary is unchanged — still has the v1.60+ directive, not the random thought.
        ss = SummaryStore(summaries_dir)
        stored = ss.read("g_arch")
        assert stored is not None, "Summary file must still exist after a declined contribution"
        assert len(stored.directives) == 1, "Summary must still have exactly one directive"
        assert stored.directives[0].content == grpc_v2_content, (
            "Summary must still reflect the superseding directive after a decline"
        )

        # ------------------------------------------------------------------
        # Step 7: GET /scopes/g_arch/record — 3 contributions, 3 judgments.
        # ------------------------------------------------------------------
        resp = client.get("/scopes/g_arch/record")
        assert resp.status_code == 200
        record_payload = resp.json()
        assert len(record_payload["contributions"]) == 3, (
            "Record endpoint must return 3 contributions"
        )
        assert len(record_payload["judgments"]) == 3, "Record endpoint must return 3 judgments"

        # ------------------------------------------------------------------
        # Step 8: Final invariants.
        #
        # All four scopes from the YAML are still visible via GET /scopes,
        # confirming no structural mutation occurred during the contribution
        # cycle.
        # ------------------------------------------------------------------
        resp = client.get("/scopes")
        assert resp.status_code == 200
        final_fleet = resp.json()
        final_scope_ids = {s["id"] for s in final_fleet["scopes"]}
        for expected_id in ("g_ceo", "g_eng", "g_arch", "g_backend"):
            assert expected_id in final_scope_ids, (
                f"Scope {expected_id!r} must still appear in final GET /scopes"
            )


# ---------------------------------------------------------------------------
# Companion test: idempotent bootstrap via app lifecycle
# ---------------------------------------------------------------------------


def test_e2e_idempotent_bootstrap(tmp_path):
    """Applying fleet.example.yaml twice must be idempotent.

    The second ``apply_fleet_config`` call must report zero creations and all
    entities as existing — proving no startup-time or double-apply conflict.
    This is the bootstrap idempotency guarantee exercised through a freshly
    created app lifecycle (not just the inner function).
    """
    db_path = str(tmp_path / "idempotent.db")
    summaries_dir = str(tmp_path / "summaries")

    # First application — creates everything.
    run_migrations(db_path)
    config = load_fleet_config(_FLEET_YAML)

    with RecordStore(db_path) as rs:
        first = apply_fleet_config(rs, config)

    assert len(first.strata_created) == 3
    assert len(first.scopes_created) == 4
    assert len(first.edges_created) == 4

    # Build an app so the lifespan (run_migrations, SummaryStore init) fires.
    settings = Settings(
        db_path=db_path,
        summaries_dir=summaries_dir,
        manager_model="claude-haiku-4-5",
        anthropic_api_key="test-key-idempotent",
    )
    app = create_app(settings=settings)
    app.dependency_overrides[get_scope_manager] = lambda: MagicMock(spec=ScopeManager)

    with TestClient(app):
        pass  # trigger lifespan startup/shutdown

    # Second application — must be entirely idempotent.
    with RecordStore(db_path) as rs:
        second = apply_fleet_config(rs, config)

    assert second.strata_created == [], "Second bootstrap must create no new strata"
    assert second.scopes_created == [], "Second bootstrap must create no new scopes"
    assert second.edges_created == [], "Second bootstrap must create no new edges"

    assert len(second.strata_existing) == 3, "Second bootstrap must report 3 existing strata"
    assert len(second.scopes_existing) == 4, "Second bootstrap must report 4 existing scopes"
    assert len(second.edges_existing) == 4, "Second bootstrap must report 4 existing edges"
