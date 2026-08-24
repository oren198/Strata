"""API-level tests for GET /staleness — the P2 proof-surface staleness view.

All scope-manager calls are mocked — no real Anthropic API calls are made.
The record store and summary store use real tmp paths. Fleet configuration is
backed by a real fleet.yaml on disk. The ``client`` fixture is copied verbatim
from ``tests/test_app.py:129-161``.
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def test_staleness_counts_sessions_reading_since_last_accepted(client):
    from strata.record_store import ContributorRef, RecordStore
    from strata.session_state import SessionStateStore, sessions_dir_for

    with RecordStore(client.db_path) as store:
        c = store.append_contribution(
            scope_id="g_active", content="use gRPC",
            proposed_classification="directive", subject=None, supersedes=None,
            contributor=ContributorRef(scope_id="g_active", skill="architect",
                                       session_id="s0", ts="2026-08-01T12:00:00+00:00"),
        )
        store.record_judgment(contribution_id=c.id, decision="accept_as_directive",
                              judged_by="scope-manager", notes="ok")

    sessions = SessionStateStore(sessions_dir_for(client.summaries_dir))
    sessions.record_read("s1", "g_active")
    sessions.record_read("s2", "g_active")
    sessions.record_read("s3", "g_other_unknown")

    body = client.get("/staleness").json()
    by_id = {s["scope_id"]: s for s in body["scopes"]}
    assert by_id["g_active"]["reads_since_last_contribution"] == 2
    assert by_id["g_active"]["last_accepted_contribution_at"] == c.created_at
    assert by_id["g_active"]["name"] == "Active Scope"
    assert by_id["g_active"]["stratum_id"] == "L1"
    assert body["window_days"] == 30


def test_staleness_lists_only_active_scopes(client):
    body = client.get("/staleness").json()
    ids = {s["scope_id"] for s in body["scopes"]}
    assert "g_active" in ids
    assert "g_archived" not in ids


def test_staleness_sorted_worst_first(client):
    from strata.session_state import SessionStateStore, sessions_dir_for
    sessions = SessionStateStore(sessions_dir_for(client.summaries_dir))
    for i in range(3):
        sessions.record_read(f"s{i}", "g_active")
    body = client.get("/staleness").json()
    counts = [s["reads_since_last_contribution"] for s in body["scopes"]]
    assert counts == sorted(counts, reverse=True)


def test_state_is_no_memory_when_the_scope_has_no_summary(client):
    body = client.get("/staleness").json()
    row = {s["scope_id"]: s for s in body["scopes"]}["g_active"]
    assert row["summary_version"] == 0
    assert row["summary_updated_at"] is None
    assert row["state"] == "no_memory"


def test_state_is_fresh_then_stale_once_a_session_reads(client):
    from strata.session_state import SessionStateStore, sessions_dir_for
    from strata.summary_store import ScopeSummary, SummaryStore

    # SummaryStore.write() derives ``version`` from the on-disk state
    # (existing.version + 1, issue #59) rather than trusting the value
    # passed in, so two writes are needed to land at version=2.
    _store = SummaryStore(client.summaries_dir)
    for _ in range(2):
        _store.write("g_active", ScopeSummary(
            scope_id="g_active", directives=[], context="something",
            updated_at="2026-08-01T12:00:00+00:00", version=2, exists=True,
        ))
    row = {s["scope_id"]: s for s in client.get("/staleness").json()["scopes"]}["g_active"]
    assert row["summary_version"] == 2
    assert row["state"] == "fresh"

    SessionStateStore(sessions_dir_for(client.summaries_dir)).record_read("s1", "g_active")
    row = {s["scope_id"]: s for s in client.get("/staleness").json()["scopes"]}["g_active"]
    assert row["state"] == "stale"


def test_session_outcomes_buckets_are_disjoint(client):
    from strata.session_state import SessionStateStore, sessions_dir_for
    sessions = SessionStateStore(sessions_dir_for(client.summaries_dir))

    sessions.record_read("s_contrib", "g_active")
    sessions.record_contribution("s_contrib")

    sessions.record_read("s_closeout", "g_active")
    sessions.record_decline("s_closeout")

    sessions.record_read("s_silent", "g_active")

    outcomes = client.get("/staleness").json()["session_outcomes"]
    assert outcomes == {"contributions": 1, "closeouts": 1, "silent_readers": 1}


def test_session_outcomes_is_all_zero_with_no_sessions(client):
    assert client.get("/staleness").json()["session_outcomes"] == {
        "contributions": 0, "closeouts": 0, "silent_readers": 0,
    }


def test_staleness_honours_window_days(client):
    resp = client.get("/staleness", params={"window_days": 7})
    assert resp.status_code == 200
    assert resp.json()["window_days"] == 7


def test_staleness_rejects_window_days_below_one(client):
    assert client.get("/staleness", params={"window_days": 0}).status_code == 422


def test_staleness_single_scope_filter(client):
    body = client.get("/staleness", params={"scope_id": "g_active"}).json()
    assert [s["scope_id"] for s in body["scopes"]] == ["g_active"]
    assert client.get("/staleness", params={"scope_id": "g_nope"}).status_code == 404
