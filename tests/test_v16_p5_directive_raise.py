"""v1.16 P5 §B — the engine's own raise: a `failed` outcome against a directive,
whose issuer differs from the reporter, becomes an ordinary upward contribution at
the issuing scope, judged synchronously in the same request (ADR 0017 P5).

Follows test_p3_end_to_end.py's HTTP fixture pattern: a two-scope chain
(g_reporter -> g_source), a mocked scope-manager whose `judge` return value is
set per call via `side_effect`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager
from strata.migrator import run_migrations
from strata.record_store import RecordStore
from strata.scope_manager import ScopeManagerJudgment
from strata.settings import Settings
from strata.summary_store import ScopeSummary

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
    application.dependency_overrides[get_scope_manager] = lambda: mock_manager
    with TestClient(application) as tc:
        tc.mock_manager = mock_manager  # type: ignore[attr-defined]
        tc.db_path = db_path  # type: ignore[attr-defined]
        yield tc


def _post_directive(http_client, *, scope_id: str) -> str:
    http_client.mock_manager.judge.side_effect = None
    http_client.mock_manager.judge.return_value = _judgment(
        "accept_as_directive", summary=_summary(scope_id, "the directive")
    )
    resp = http_client.post(
        "/contribute",
        json={
            "scope_id": scope_id,
            "content": "All services must use TLS 1.3 or later.",
            "proposed_classification": "directive",
            "contributor": {**_CONTRIBUTOR_BODY, "scope_id": scope_id},
        },
    )
    assert resp.status_code == 200
    return resp.json()["contribution_id"]


def _post_outcome(http_client, *, scope_id: str, acted_on: str, reporter_judgment) -> dict:
    http_client.mock_manager.judge.side_effect = [reporter_judgment]
    resp = http_client.post(
        "/contribute",
        json={
            "scope_id": scope_id,
            "content": "Tried TLS 1.3; the peer only supports 1.2.",
            "proposed_classification": "context",
            "contributor": {**_CONTRIBUTOR_BODY, "scope_id": scope_id},
            "acted_on": acted_on,
        },
    )
    assert resp.status_code == 200
    return resp.json()


def test_a_failed_directive_outcome_raises_to_the_issuing_scope(http_client) -> None:
    directive_id = _post_directive(http_client, scope_id="g_source")

    http_client.mock_manager.judge.side_effect = [
        _judgment(
            "accept_as_context",
            summary=_summary("g_reporter", "TLS 1.3 unsupported by peer"),
            outcome_disposition="failed",
        ),
        _judgment("accept_as_context", summary=_summary("g_source", "revised")),
    ]
    resp = http_client.post(
        "/contribute",
        json={
            "scope_id": "g_reporter",
            "content": "Tried TLS 1.3; the peer only supports 1.2.",
            "proposed_classification": "context",
            "contributor": _CONTRIBUTOR_BODY,
            "acted_on": directive_id,
        },
    )
    assert resp.status_code == 200
    outcome_id = resp.json()["contribution_id"]

    store = RecordStore(http_client.db_path)
    raised = [c for c in store.list_contributions(scope_id="g_source") if c.raised_from is not None]
    assert len(raised) == 1
    raised_contribution = raised[0]
    assert raised_contribution.raised_from == outcome_id
    assert raised_contribution.acted_on == directive_id
    assert raised_contribution.contributor.scope_id == "g_reporter"
    assert f"Following {directive_id} went wrong" in raised_contribution.content
    assert "Tried TLS 1.3; the peer only supports 1.2." in raised_contribution.content

    judgment = store.get_judgment(raised_contribution.id)
    assert judgment is not None
    assert judgment.decision == "accept_as_context"
    # 1 call for the directive's own admission, then 2 for this outcome POST: the
    # reporter's own judgment, then the synchronous raise judgment at the issuer.
    assert http_client.mock_manager.judge.call_count == 3

    # Aron's ask: pin exactly what suppresses the acted_on path for the raised
    # contribution, so a later edit can't silently re-enable the directive-target
    # verdict set at the issuer (which would invite a re-raise). It's
    # `raised_from` being set — NOT "no acted_on" (the raised contribution DOES
    # carry acted_on = the directive as a record fact) — checked in
    # `_judge_and_record` (strata/app.py) before `ActedOnTarget` is ever built:
    # `if contribution.acted_on is not None and contribution.raised_from is None`.
    # The third call (the synchronous raise judgment) is the one this contract
    # is actually about.
    raise_call_kwargs = http_client.mock_manager.judge.call_args_list[2].kwargs
    assert "acted_on_target" not in raise_call_kwargs
    # Contrast: the reporter's OWN outcome call (acted_on set, raised_from unset)
    # DOES carry it — proving the suppression is specifically about raised_from,
    # not "acted_on is somehow never passed through this call site at all".
    reporter_call_kwargs = http_client.mock_manager.judge.call_args_list[1].kwargs
    assert reporter_call_kwargs.get("acted_on_target") is not None
    assert reporter_call_kwargs["acted_on_target"].is_directive


def test_held_is_never_raised(http_client) -> None:
    directive_id = _post_directive(http_client, scope_id="g_source")
    _post_outcome(
        http_client,
        scope_id="g_reporter",
        acted_on=directive_id,
        reporter_judgment=_judgment(
            "accept_as_context",
            summary=_summary("g_reporter", "held"),
            outcome_disposition="held",
        ),
    )
    store = RecordStore(http_client.db_path)
    raised = [c for c in store.list_contributions(scope_id="g_source") if c.raised_from is not None]
    assert raised == []
    # 1 for the directive's own admission, 1 for the outcome — no synchronous
    # second call, since a `held` outcome is never raised.
    assert http_client.mock_manager.judge.call_count == 2


def test_issuer_equal_reporter_raises_nothing(http_client) -> None:
    directive_id = _post_directive(http_client, scope_id="g_reporter")
    _post_outcome(
        http_client,
        scope_id="g_reporter",
        acted_on=directive_id,
        reporter_judgment=_judgment(
            "accept_as_context",
            summary=_summary("g_reporter", "revised locally"),
            outcome_disposition="failed",
        ),
    )
    store = RecordStore(http_client.db_path)
    raised = [
        c for c in store.list_contributions(scope_id="g_reporter") if c.raised_from is not None
    ]
    assert raised == []
    # 1 for the directive's own admission, 1 for the outcome — no synchronous
    # second call, since issuer == reporter raises nothing.
    assert http_client.mock_manager.judge.call_count == 2


def test_a_failed_synchronous_raise_judgment_never_fails_the_reporters_own_call(
    http_client,
) -> None:
    """The reporter's own outcome is already durably judged before the synchronous
    second call runs — a judge outage on the RAISED contribution must not turn into
    a 500 for the reporter's own request; it stays pending, retried later via the
    ordinary strata_rejudge path."""
    directive_id = _post_directive(http_client, scope_id="g_source")
    http_client.mock_manager.judge.side_effect = [
        _judgment(
            "accept_as_context",
            summary=_summary("g_reporter", "TLS 1.3 unsupported by peer"),
            outcome_disposition="failed",
        ),
        RuntimeError("judge endpoint down"),
    ]
    resp = http_client.post(
        "/contribute",
        json={
            "scope_id": "g_reporter",
            "content": "Tried TLS 1.3; the peer only supports 1.2.",
            "proposed_classification": "context",
            "contributor": _CONTRIBUTOR_BODY,
            "acted_on": directive_id,
        },
    )
    assert resp.status_code == 200
    outcome_id = resp.json()["contribution_id"]

    store = RecordStore(http_client.db_path)
    raised = [c for c in store.list_contributions(scope_id="g_source") if c.raised_from is not None]
    assert len(raised) == 1
    assert raised[0].raised_from == outcome_id
    assert store.get_judgment(raised[0].id) is None  # still pending, not fabricated
