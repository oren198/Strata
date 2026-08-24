"""Console operator supersede/retire routes (P5) — same call path as the CLI.

Both routes delegate straight to ``strata.operator.operator_supersede`` /
``operator_retire``, which take ``scope_lock`` (ADR 0012's cross-process
flock) themselves as their first act. The route takes no lock of its own —
see ``strata/app.py``'s ``supersede_directive`` / ``retire_directive``
docstrings.
"""

import pathlib
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager
from strata.migrator import run_migrations
from strata.scope_manager import ScopeManager
from strata.settings import Settings
from strata.summary_store import Directive, ScopeSummary, SummaryStore
from tests.test_app import _FLEET_YAML_SIMPLE, _make_judgment, _make_summary


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


def _seed_directive(client):
    """Seed a native directive as if a normal contribution had been accepted.

    A real contribution row is required, not just a summary entry: the
    ``contributions.supersedes`` column is a foreign key onto
    ``contributions.id`` (``src/strata/_migrations/0001_initial.sql``), and
    ``operator_supersede`` writes ``supersedes=directive_id`` — so the
    directive being corrected must already exist as a contribution row,
    exactly as :func:`tests.test_operator._seed_directive` does it.
    """
    from strata.record_store import ContributorRef, RecordStore

    with RecordStore(client.db_path) as store:
        contribution = store.append_contribution(
            scope_id="g_active",
            content="use REST",
            proposed_classification="directive",
            subject="rpc-protocol",
            supersedes=None,
            contributor=ContributorRef(
                scope_id="g_active",
                skill="architect",
                session_id="s1",
                ts="2026-08-01T12:00:00+00:00",
            ),
        )
        store.record_judgment(
            contribution_id=contribution.id,
            decision="accept_as_directive",
            judged_by="scope-manager",
        )

    SummaryStore(client.summaries_dir).write(
        "g_active",
        ScopeSummary(
            scope_id="g_active",
            directives=[
                Directive(
                    id=contribution.id,
                    content="use REST",
                    subject="rpc-protocol",
                    source_scope_id="g_active",
                    source_skill="architect",
                    created_at=contribution.created_at,
                )
            ],
            context="",
            updated_at=contribution.created_at,
            version=1,
            exists=True,
        ),
    )
    return contribution.id


def test_supersede_replaces_the_directive_in_the_summary(client):
    did = _seed_directive(client)
    resp = client.post(
        f"/scopes/g_active/directives/{did}/supersede",
        json={"content": "Use gRPC for all inter-service calls.", "subject": "rpc-protocol"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["superseded_directive_id"] == did
    new_id = body["directive"]["id"]
    assert new_id != did
    assert body["directive"]["source_scope_id"] == "operator"

    summary = SummaryStore(client.summaries_dir).read("g_active")
    ids = [d.id for d in summary.directives]
    assert did not in ids
    assert new_id in ids
    assert next(d for d in summary.directives if d.id == new_id).content == (
        "Use gRPC for all inter-service calls."
    )


def test_supersede_writes_operator_provenance_into_the_record(client):
    from strata.record_store import RecordStore

    did = _seed_directive(client)
    new_id = client.post(
        f"/scopes/g_active/directives/{did}/supersede",
        json={"content": "Use gRPC.", "subject": None},
    ).json()["directive"]["id"]

    with RecordStore(client.db_path) as store:
        entry = store.get_record_entry(new_id)
        assert entry is not None
        assert entry.contribution.contributor.scope_id == "operator"
        assert entry.contribution.supersedes == did
        assert entry.judgment.judged_by == "operator"
        assert entry.judgment.decision == "accept_as_directive"


def test_retire_removes_the_directive_and_appends_a_retirement(client):
    from strata.record_store import RecordStore

    did = _seed_directive(client)
    resp = client.post(
        f"/scopes/g_active/directives/{did}/retire",
        json={"reason": "The REST gateway is gone."},
    )
    assert resp.status_code == 200
    assert resp.json()["retirement"]["directive_id"] == did
    assert resp.json()["retirement"]["retired_by"] == "operator"
    assert resp.json()["retirement"]["reason"] == "The REST gateway is gone."

    summary = SummaryStore(client.summaries_dir).read("g_active")
    assert [d.id for d in summary.directives] == []

    with RecordStore(client.db_path) as store:
        retirements = store.list_retirements(scope_id="g_active")
        assert [r.directive_id for r in retirements] == [did]


def test_retire_without_a_reason_is_allowed(client):
    did = _seed_directive(client)
    resp = client.post(f"/scopes/g_active/directives/{did}/retire", json={"reason": None})
    assert resp.status_code == 200
    assert resp.json()["retirement"]["reason"] is None


def test_unknown_scope_is_404(client):
    resp = client.post(
        "/scopes/g_nope/directives/c_x/supersede", json={"content": "x", "subject": None}
    )
    assert resp.status_code == 404


def test_directive_not_in_current_summary_is_404(client):
    _seed_directive(client)
    resp = client.post(
        "/scopes/g_active/directives/c_notthere/supersede", json={"content": "x", "subject": None}
    )
    assert resp.status_code == 404
    assert "c_notthere" in resp.json()["detail"]


def test_operator_stratum_ids_are_refused(client):
    resp = client.post("/scopes/g_active/directives/op_abc/retire", json={"reason": None})
    assert resp.status_code == 422
    assert "command line" in str(resp.json()["detail"])


def test_blank_content_is_refused(client):
    did = _seed_directive(client)
    resp = client.post(
        f"/scopes/g_active/directives/{did}/supersede", json={"content": "   ", "subject": None}
    )
    assert resp.status_code == 422


def test_the_scope_lock_file_is_created_by_the_endpoint(client):
    """The endpoint must go through scope_lock (ADR 0012) — the library takes it."""
    did = _seed_directive(client)
    client.post(f"/scopes/g_active/directives/{did}/retire", json={"reason": None})
    locks_dir = pathlib.Path(client.db_path).parent / ".locks"
    assert locks_dir.is_dir()
    assert any("g_active" in p.name for p in locks_dir.iterdir())


def test_summary_lists_retirements(client):
    did = _seed_directive(client)
    client.post(f"/scopes/g_active/directives/{did}/retire", json={"reason": "Gone."})
    body = client.get("/scopes/g_active/summary").json()
    assert [r["directive_id"] for r in body["retirements"]] == [did]
    assert body["retirements"][0]["reason"] == "Gone."
