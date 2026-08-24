"""API-level tests for GET /scopes/{scope_id}/perspective — the P4 proof-surface
view-as endpoint.

All scope-manager calls are mocked — no real Anthropic API calls are made. The
record store and summary store use real tmp paths. Fleet configuration is
backed by a real fleet.yaml on disk. The ``client`` fixture is copied verbatim
from ``tests/test_app.py:129-161``, but writes ``_FLEET_YAML_CHAIN`` instead
of ``_FLEET_YAML_SIMPLE`` since this endpoint needs an ancestor chain and a
referenced peer to exercise every layer relation.
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
from strata.summary_store import ScopeSummary

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FLEET_YAML_CHAIN = textwrap.dedent("""
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
      - id: g_peer
        name: Peer Scope
        stratum_id: L1
        status: active

    edges:
      - from: g_child
        to: g_root
      - from: g_child
        to: g_peer
        kind: reference
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
    return ScopeSummary(
        scope_id=scope_id,
        directives=[],
        context="",
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
    (tmp_path / "fleet.yaml").write_text(_FLEET_YAML_CHAIN, encoding="utf-8")

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
        summary=_make_summary("g_child", "accept_as_directive"),
    )

    application.dependency_overrides[get_scope_manager] = lambda: mock_manager

    with TestClient(application) as tc:
        tc.scope_id = "g_child"  # type: ignore[attr-defined]
        tc.mock_manager = mock_manager  # type: ignore[attr-defined]
        tc.summaries_dir = summaries_dir  # type: ignore[attr-defined]
        tc.db_path = db_path  # type: ignore[attr-defined]
        yield tc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_perspective_returns_chain_root_first_with_relations(client):
    body = client.get("/scopes/g_child/perspective").json()
    assert body["scope_id"] == "g_child"
    relations = [(layer["scope_id"], layer["relation"]) for layer in body["layers"]]
    assert relations[0] == ("g_root", "ancestor")
    assert ("g_child", "self") in relations
    assert ("g_peer", "peer_reference") in relations


def test_peer_layers_carry_publication_not_summary(client):
    body = client.get("/scopes/g_child/perspective").json()
    peer = next(layer for layer in body["layers"] if layer["relation"] == "peer_reference")
    assert "publication" in peer
    assert "summary" not in peer
    assert peer["binding"] is False


def test_every_layer_carries_a_token_estimate_and_they_sum(client):
    body = client.get("/scopes/g_child/perspective").json()
    assert all(isinstance(layer["token_estimate"], int) for layer in body["layers"])
    assert body["token_estimate_total"] == sum(layer["token_estimate"] for layer in body["layers"])
    assert "estimate" in body["token_estimate_method"]


def test_token_estimate_grows_with_content(client):
    from strata.summary_store import ScopeSummary, SummaryStore

    store = SummaryStore(client.summaries_dir)
    before = client.get("/scopes/g_child/perspective").json()
    self_before = next(layer for layer in before["layers"] if layer["relation"] == "self")
    store.write(
        "g_child",
        ScopeSummary(
            scope_id="g_child",
            directives=[],
            context="x" * 4000,
            updated_at="2026-08-24T09:00:00+00:00",
            version=1,
            exists=True,
        ),
    )
    after = client.get("/scopes/g_child/perspective").json()
    self_after = next(layer for layer in after["layers"] if layer["relation"] == "self")
    assert self_after["token_estimate"] >= self_before["token_estimate"] + 900


def test_perspective_unknown_scope_is_404(client):
    assert client.get("/scopes/g_nope/perspective").status_code == 404


def test_perspective_does_not_attach_a_session_nudge(client):
    body = client.get("/scopes/g_child/perspective").json()
    assert "nudge" not in body
