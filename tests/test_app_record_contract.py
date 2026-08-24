"""Pins the /record response shape the Console's record trail renders.

UI-only guard (constraint G1): the endpoint is not new, but the Console now
depends on these exact keys, so a rename here must break a test, not a screen.

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


def _seed(client, n):
    from strata.record_store import ContributorRef, RecordStore

    ids = []
    with RecordStore(client.db_path) as store:
        for i in range(n):
            c = store.append_contribution(
                scope_id="g_active",
                content=f"contribution {i}",
                proposed_classification="directive",
                subject=None,
                supersedes=None,
                contributor=ContributorRef(
                    scope_id="g_active",
                    skill="architect",
                    session_id="s",
                    ts="2026-08-20T10:00:00+00:00",
                ),
            )
            ids.append(c.id)
        store.record_judgment(
            contribution_id=ids[0], decision="decline", judged_by="scope-manager", notes="No."
        )
    return ids


def test_record_page_carries_every_key_the_trail_renders(client):
    _seed(client, 3)
    body = client.get("/scopes/g_active/record").json()
    assert set(body) >= {
        "contributions",
        "judgments",
        "judgment_attempts",
        "contribution_states",
        "page",
    }
    assert set(body["page"]) == {"limit", "total", "next_before_id"}
    c0 = body["contributions"][0]
    assert set(c0) >= {
        "id",
        "scope_id",
        "content",
        "proposed_classification",
        "subject",
        "supersedes",
        "contributor",
        "created_at",
    }
    assert set(c0["contributor"]) == {"scope_id", "skill", "session_id", "ts"}
    st = body["contribution_states"][0]
    assert "contribution_id" in st and "state" in st


def test_record_page_walks_back_with_next_before_id(client):
    _seed(client, 5)
    first = client.get("/scopes/g_active/record", params={"limit": 2}).json()
    assert len(first["contributions"]) == 2
    assert first["page"]["total"] == 5
    cursor = first["page"]["next_before_id"]
    assert cursor is not None
    second = client.get("/scopes/g_active/record", params={"limit": 2, "before_id": cursor}).json()
    seen = {c["id"] for c in first["contributions"]}
    assert not seen & {c["id"] for c in second["contributions"]}


def test_record_entry_by_id_carries_judgment_notes(client):
    ids = _seed(client, 1)
    body = client.get(f"/scopes/g_active/record/{ids[0]}").json()
    assert body["judgment"]["notes"] == "No."
    assert body["contribution"]["id"] == ids[0]
    assert "state" in body and "judgment_attempts" in body
