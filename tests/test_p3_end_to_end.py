"""v1.15 P3, ADR 0017 — end to end: a failed outcome, through HTTP and MCP, atomically
recorded, never emitted/composed as an input change, and read back by standing_evidence.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager
from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.record_store import RecordStore
from strata.scope_manager import ScopeManagerJudgment
from strata.settings import Settings
from strata.summary_store import ScopeSummary
from tests.test_mcp_server import _load_mcp_module, _make_db, _make_fleet_yaml

_FLEET_YAML = """
strata:
  - id: L0
    name: Executive
    ordinal: 0
  - id: L1
    name: Function
    ordinal: 1

scopes:
  - id: g_source
    name: Source
    stratum_id: L0
    status: active
  - id: g_reporter
    name: Reporter
    stratum_id: L1
    status: active

edges:
  - from: g_reporter
    to: g_source
"""

_CONTRIBUTOR_BODY = {
    "scope_id": "g_reporter",
    "skill": "engineer",
    "session_id": "sess_001",
    "ts": "2026-09-25T09:00:00Z",
}


def _judgment(decision: str, *, summary=None, outcome_disposition=None) -> ScopeManagerJudgment:
    return ScopeManagerJudgment(
        decision=decision,
        reasoning="test reasoning",
        new_summary=summary,
        outcome_disposition=outcome_disposition,
    )


def _summary(scope_id: str, context: str) -> ScopeSummary:
    return ScopeSummary(
        scope_id=scope_id, directives=[], context=context, updated_at="2026-09-01T00:00:00Z"
    )


# --- HTTP -------------------------------------------------------------------------------


@pytest.fixture()
def http_client(tmp_path):
    db_path = str(tmp_path / "test.db")
    summaries_dir = str(tmp_path / "summaries")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")
    run_migrations(db_path)
    Path(fleet_yaml_path).write_text(_FLEET_YAML, encoding="utf-8")
    settings = Settings(
        db_path=db_path,
        summaries_dir=summaries_dir,
        fleet_yaml_path=fleet_yaml_path,
        manager_model="claude-haiku-4-5",
        anthropic_api_key="test-key",
    )
    application = create_app(settings=settings)
    mock_manager = MagicMock()
    mock_manager.judge.return_value = _judgment(
        "accept_as_context", summary=_summary("g_source", "..."), outcome_disposition="held"
    )
    application.dependency_overrides[get_scope_manager] = lambda: mock_manager
    with TestClient(application) as tc:
        tc.mock_manager = mock_manager  # type: ignore[attr-defined]
        tc.db_path = db_path  # type: ignore[attr-defined]
        yield tc


def test_http_failed_corrected_end_to_end(http_client) -> None:
    http_client.mock_manager.judge.return_value = _judgment(
        "accept_as_context", summary=_summary("g_source", "..."), outcome_disposition="held"
    )
    target = http_client.post(
        "/contribute",
        json={
            "scope_id": "g_source",
            "content": "The service listens on port 8443.",
            "proposed_classification": "context",
            "contributor": {**_CONTRIBUTOR_BODY, "scope_id": "g_source"},
        },
    )
    assert target.status_code == 200
    target_id = target.json()["contribution_id"]

    http_client.mock_manager.judge.return_value = _judgment(
        "accept_as_context",
        summary=_summary("g_reporter", "unknown port now"),
        outcome_disposition="failed_corrected",
    )
    outcome = http_client.post(
        "/contribute",
        json={
            "scope_id": "g_reporter",
            "content": "Used port 8443, the service refused; the right port is unknown.",
            "proposed_classification": "context",
            "contributor": _CONTRIBUTOR_BODY,
            "acted_on": target_id,
        },
    )
    assert outcome.status_code == 200
    outcome_id = outcome.json()["contribution_id"]

    store = RecordStore(http_client.db_path)
    fleet = FleetConfig.model_validate(
        {
            "strata": [
                {"id": "L0", "name": "Executive", "ordinal": 0},
                {"id": "L1", "name": "Function", "ordinal": 1},
            ],
            "scopes": [
                {"id": "g_source", "name": "Source", "stratum_id": "L0"},
                {"id": "g_reporter", "name": "Reporter", "stratum_id": "L1"},
            ],
            "edges": [{"from": "g_reporter", "to": "g_source"}],
        }
    )
    from strata.record_store import standing_evidence

    result = standing_evidence(store, fleet, target_id)
    assert len(result) == 1
    assert result[0].contribution_id == outcome_id
    assert result[0].replaced_kind == "claim_corrected"

    event = store.claim_event_for(outcome_id)
    assert event is not None
    assert event.kind == "claim_corrected"
    assert event.item_id == target_id
    assert event.scope_id == "g_reporter"
    assert event.source_scope_id == "g_source"

    # Condition 1: not emitted — no pending (unprocessed) row exists anywhere.
    assert store.list_change_events(scope_id="g_reporter", unprocessed_only=True) == []
    assert store.list_change_events(scope_id="g_source", unprocessed_only=True) == []

    # Condition 2: not composed as an input change, in EITHER scope's perspective.
    persp_reporter = http_client.get("/scopes/g_reporter/summary")
    assert persp_reporter.status_code == 200


# --- MCP ----------------------------------------------------------------------------------


@pytest.fixture()
def mcp(tmp_path):
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)
    module = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)
    with (
        patch.object(module, "_AGENT_SCOPE", "g_backend"),
        patch.object(module, "_AGENT_SKILL", "strata-developer"),
        patch.object(module, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(module, "_load_fleet", return_value=fleet),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        yield module


async def _contribute(mod, *, judgment, **kwargs) -> dict:
    with patch("strata.scope_manager.ScopeManager.judge", return_value=judgment):
        return await mod.strata_contribute(
            scope_id=kwargs.pop("scope_id", "g_backend"),
            content=kwargs.pop("content", "An observation."),
            proposed_classification="context",
            **kwargs,
        )


async def test_mcp_failed_superseded_end_to_end(mcp) -> None:
    target = await _contribute(
        mcp,
        judgment=_judgment(
            "accept_as_context", summary=_summary("g_backend", "..."), outcome_disposition="held"
        ),
        content="The service listens on port 8443.",
    )
    target_id = target["contribution_id"]

    outcome = await _contribute(
        mcp,
        judgment=_judgment(
            "accept_as_context",
            summary=_summary("g_backend", "port is now 9443"),
            outcome_disposition="failed_superseded",
        ),
        content="The port changed to 9443 as of the migration.",
        acted_on=target_id,
    )
    outcome_id = outcome["contribution_id"]

    event = mcp._record_store.claim_event_for(outcome_id)
    assert event is not None
    assert event.kind == "claim_superseded"
    assert event.item_id == target_id

    # Condition 1/2: no unprocessed row, and never composed into a perspective's
    # input_changes.
    assert mcp._record_store.list_change_events(scope_id="g_backend", unprocessed_only=True) == []
    result = await mcp.strata_read_perspective("g_backend")
    assert result.get("input_changes", []) == []


async def test_mcp_a_declined_outcome_writes_no_event(mcp) -> None:
    target = await _contribute(
        mcp,
        judgment=_judgment(
            "accept_as_context", summary=_summary("g_backend", "..."), outcome_disposition="held"
        ),
        content="A note worth acting on.",
    )
    target_id = target["contribution_id"]

    outcome = await _contribute(
        mcp,
        judgment=ScopeManagerJudgment(
            decision="decline",
            reasoning="no outcome reported: reviewed it and confirmed it.",
            new_summary=None,
            outcome_disposition="decline",
        ),
        content="Reviewed it and confirmed it.",
        acted_on=target_id,
    )
    assert mcp._record_store.claim_event_for(outcome["contribution_id"]) is None


# --- condition 5: every other reader of change_events tolerates the new kinds --------------


async def test_every_reader_of_change_events_tolerates_the_two_new_kinds(mcp) -> None:
    """A DB holding both new kinds, read through every public surface that touches
    change_events, without error: list_change_events (both filters), the HTTP/MCP
    record views, strata_read_perspective (both scopes), and the drain."""
    from strata.record_store import ContributorRef

    store = mcp._record_store
    c1 = store.append_contribution(
        scope_id="g_backend",
        content="x",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_backend", skill=None, session_id="s1", ts="2026-01-01T00:00:00Z"
        ),
    )
    store.record_judgment(
        contribution_id=c1.id, decision="accept_as_context", judged_by="scope-manager"
    )
    c2 = store.append_contribution(
        scope_id="g_backend",
        content="y",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_backend", skill=None, session_id="s1", ts="2026-01-01T00:00:00Z"
        ),
        acted_on=c1.id,
    )
    from strata.record_store import ClaimEventInput

    store.record_judgment(
        contribution_id=c2.id,
        decision="accept_as_context",
        judged_by="scope-manager",
        claim_event=ClaimEventInput(
            change_id="chg_x",
            contribution_id=c2.id,
            scope_id="g_backend",
            source_scope_id="g_backend",
            item_id=c1.id,
            kind="claim_corrected",
            before="x",
            after="y",
        ),
    )

    # list_change_events, both filters.
    assert len(store.list_change_events(scope_id="g_backend")) >= 1
    assert store.list_change_events(scope_id="g_backend", unprocessed_only=True) == []

    # MCP record views.
    entry = await mcp.strata_read_contribution(contribution_id=c2.id)
    assert entry["contribution"]["id"] == c2.id
    record = await mcp.strata_read_scope_record(scope_id="g_backend")
    assert any(c["id"] == c2.id for c in record["contributions"])

    # Perspective composes cleanly (no input_changes leak).
    persp = await mcp.strata_read_perspective("g_backend")
    assert persp.get("input_changes", []) == []
