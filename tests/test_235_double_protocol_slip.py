"""#235 — a SECOND protocol slip on the corrective re-ask must fail CLOSED.

Before this fix, `_call_with_correctives`'s retry attempt only caught
`_MissingReasoning` and `_MalformedDisposition` — every OTHER slip shape
(`_extract_tool_use_block` finding no tool_use block at all on the retry, or
`_parse_directive_ops`'s unpaired-``supersede`` `ValueError`, among others)
propagated straight out of `judge()`, which `strata.app._judge_and_record`
wraps into `JudgeUnavailable`, which `POST /contribute` turned into an
unconditional 500.

Reproduced and fixed with the REAL `ScopeManager`/`_call_with_correctives`/
`_parse_directive_ops` — only the underlying Anthropic client is mocked,
replaying the exact raw tool_use shape an unpaired-``supersede`` response
takes, modeled on `tests/test_v16_p5_operator_parse_bug.py`'s fixture.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager
from strata.migrator import run_migrations
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import ScopeManager
from strata.settings import Settings

_FLEET_YAML = """
strata:
  - id: L0
    name: Executive
    ordinal: 0

scopes:
  - id: g_active
    name: Active
    stratum_id: L0
    status: active

edges: []
"""

_CONTRIBUTOR_BODY = {
    "scope_id": "g_active",
    "skill": "on-call-engineer",
    "session_id": "sess-235",
    "ts": "2026-10-02T09:05:00Z",
}


def _tool_use_response(**payload) -> MagicMock:
    """The exact shape `ScopeManager.judge` reads off an Anthropic response:
    one tool_use content block carrying the raw fields the model returned."""
    block = MagicMock()
    block.type = "tool_use"
    block.id = "toolu_fake"
    block.input = payload
    resp = MagicMock()
    resp.content = [block]
    return resp


def _unpaired_supersede_response(directive_id: str, *, reasoning: str) -> MagicMock:
    """A response whose `directive_ops` carries a lone `supersede` — valid id,
    but no `append`/`publish` in the same amendment. `_parse_directive_ops`
    rejects this (scope_manager.py's unpaired-supersede `ValueError`, the
    shape #235 names) regardless of how well-formed the rest of the payload is."""
    return _tool_use_response(
        decision="accept_as_context",
        reasoning=reasoning,
        directive_ops=[{"op": "supersede", "id": directive_id}],
        new_context="Superseded.",
    )


@pytest.fixture()
def client(tmp_path):
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

    # A REAL ScopeManager — only its underlying Anthropic client is mocked, so
    # `_call_with_correctives`/`_parse_directive_ops` both run for real.
    mock_anthropic = MagicMock()
    real_manager = ScopeManager(client=mock_anthropic, model="claude-haiku-4-5")
    application.dependency_overrides[get_scope_manager] = lambda: real_manager

    with TestClient(application) as tc:
        tc.db_path = db_path  # type: ignore[attr-defined]
        tc.summaries_dir = summaries_dir  # type: ignore[attr-defined]
        tc.mock_anthropic = mock_anthropic  # type: ignore[attr-defined]
        yield tc


def _seed_directive(db_path: str) -> str:
    store = RecordStore(db_path)
    directive_id = store.append_contribution(
        scope_id="g_active",
        content="Page the sev-1 rotation through the primary pager.",
        proposed_classification="directive",
        subject="paging",
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_active", skill="eng-lead", session_id="s0", ts="2026-10-01T00:00:00Z"
        ),
    ).id
    store.record_judgment(
        contribution_id=directive_id, decision="accept_as_directive", judged_by="scope-manager"
    )
    return directive_id


def test_a_double_unpaired_supersede_slip_fails_closed_never_500s(client) -> None:
    directive_id = _seed_directive(client.db_path)

    # BOTH the first attempt AND the one corrective retry replay the same
    # unpaired-supersede shape, so the terminal fail-closed path is exercised
    # deterministically — the one-retry discipline leaves no third attempt.
    client.mock_anthropic.messages.create.side_effect = [
        _unpaired_supersede_response(directive_id, reasoning="first slip"),
        _unpaired_supersede_response(directive_id, reasoning="second slip, still unpaired"),
    ]

    resp = client.post(
        "/contribute",
        json={
            "scope_id": "g_active",
            "content": "The old paging note no longer holds.",
            "proposed_classification": "context",
            "contributor": _CONTRIBUTOR_BODY,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["judgment"]["decision"] == "decline"

    store = RecordStore(client.db_path)
    judgment_row = store.get_judgment(body["contribution_id"])
    assert judgment_row is not None
    assert "judge failure" in (judgment_row.notes or "")
    assert "second slip, still unpaired" in (judgment_row.notes or "")


def test_the_control_slip_then_valid_retry_still_gets_the_normal_verdict(client) -> None:
    """The control: a FIRST slip (same unpaired-supersede shape) gets its one
    corrective re-ask exactly as before, and a VALID second response still
    produces the ordinary accepted verdict — #235 only changes what happens
    when the retry ALSO slips."""
    directive_id = _seed_directive(client.db_path)

    client.mock_anthropic.messages.create.side_effect = [
        _unpaired_supersede_response(directive_id, reasoning="first slip"),
        _tool_use_response(
            decision="accept_as_context",
            reasoning="Corrected: a retire op removes it instead.",
            directive_ops=[{"op": "retire", "id": directive_id}],
            new_context="The old paging note no longer holds.",
        ),
    ]

    resp = client.post(
        "/contribute",
        json={
            "scope_id": "g_active",
            "content": "The old paging note no longer holds.",
            "proposed_classification": "context",
            "contributor": _CONTRIBUTOR_BODY,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["judgment"]["decision"] == "accept_as_context"

    store = RecordStore(client.db_path)
    judgment_row = store.get_judgment(body["contribution_id"])
    assert judgment_row is not None
    assert "judge failure" not in (judgment_row.notes or "")


def test_judge_unavailable_maps_to_503_not_500(client) -> None:
    """#235 optional: JudgeUnavailable is a judge failure, never a bug in the
    request — 503, not 500. (A genuinely unhandled error, e.g. no
    `parse_forced_decline` wired, still reaches here as `JudgeUnavailable`.)"""
    client.mock_anthropic.messages.create.side_effect = RuntimeError("endpoint unreachable")

    resp = client.post(
        "/contribute",
        json={
            "scope_id": "g_active",
            "content": "Some content.",
            "proposed_classification": "context",
            "contributor": _CONTRIBUTOR_BODY,
        },
    )

    assert resp.status_code == 503
    assert "scope_manager_failure" in str(resp.json())
