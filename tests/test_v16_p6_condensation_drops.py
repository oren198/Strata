"""v1.16 P6 part 1 — condensation_drops (ADR 0017, issue #202).

One row per accepted context contribution the engine mechanically finds
condensed away by a later amendment: present verbatim in the OLD context,
absent from the NEW one. Written at the #202 stamping site
(`strata.app._write_amendment`), state_at_drop derived from the record at
that moment, priority corroborated > correcting > raised > unexamined.

No judge input change: every fixture-pinned prompt/tool render is untouched
by this item (proven by the full suite's existing fixture tests passing
unmodified alongside this file).
"""

from __future__ import annotations

import logging
import textwrap
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager
from strata.migrator import run_migrations
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import ScopeManager, ScopeManagerJudgment
from strata.settings import Settings
from strata.summary_store import ScopeSummary, SummaryStore

_FLEET_YAML = textwrap.dedent("""
    strata:
      - id: L0
        name: Executive
        ordinal: 0

    scopes:
      - id: g_x
        name: X Scope
        stratum_id: L0
        status: active

    edges: []
""").strip()

_CONTRIBUTOR_BODY = {
    "scope_id": "g_x",
    "skill": "engineer",
    "session_id": "sess_x",
    "ts": "2026-09-26T09:00:00Z",
}


def _judgment(
    decision: str = "accept_as_context",
    context: str = "",
    reasoning: str = "test reasoning",
    outcome_disposition: str | None = None,
) -> ScopeManagerJudgment:
    return ScopeManagerJudgment(
        decision=decision,  # type: ignore[arg-type]
        reasoning=reasoning,
        new_summary=ScopeSummary(
            scope_id="g_x",
            directives=[],
            context=context,
            updated_at="2026-09-26T09:00:00+00:00",
        ),
        outcome_disposition=outcome_disposition,  # type: ignore[arg-type]
    )


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    summaries_dir = str(tmp_path / "summaries")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")
    run_migrations(db_path)
    (tmp_path / "fleet.yaml").write_text(_FLEET_YAML, encoding="utf-8")
    settings = Settings(
        db_path=db_path,
        summaries_dir=summaries_dir,
        fleet_yaml_path=fleet_yaml_path,
        manager_model="claude-haiku-4-5",
        anthropic_api_key="test-key",
    )
    application = create_app(settings=settings)
    mock_manager = MagicMock(spec=ScopeManager)
    application.dependency_overrides[get_scope_manager] = lambda: mock_manager

    with TestClient(application) as tc:
        tc.db_path = db_path  # type: ignore[attr-defined]
        tc.summaries_dir = summaries_dir  # type: ignore[attr-defined]
        tc.mock_manager = mock_manager  # type: ignore[attr-defined]
        yield tc


def _contribute(client, content: str, *, acted_on: str | None = None) -> str:
    resp = client.post(
        "/contribute",
        json={
            "scope_id": "g_x",
            "content": content,
            "proposed_classification": "context",
            "contributor": _CONTRIBUTOR_BODY,
            **({"acted_on": acted_on} if acted_on else {}),
        },
    )
    assert resp.status_code == 200
    return resp.json()["contribution_id"]


def test_drops_a_corroborated_and_an_unexamined_item(client) -> None:
    client.mock_manager.judge.return_value = _judgment(context="Widget uses HTTP for its API.")
    a_id = _contribute(client, "Widget uses HTTP for its API.")

    client.mock_manager.judge.return_value = _judgment(
        context="Widget uses HTTP for its API. Gadget uses gRPC internally."
    )
    b_id = _contribute(client, "Gadget uses gRPC internally.")

    # A held outcome on A — corroborates it. new_context is unchanged: the
    # outcome's own content never enters the context text at all.
    client.mock_manager.judge.return_value = _judgment(
        context="Widget uses HTTP for its API. Gadget uses gRPC internally.",
        outcome_disposition="held",
    )
    _contribute(client, "Confirmed: widget still uses HTTP.", acted_on=a_id)

    # An amendment that drops both A's and B's text entirely.
    client.mock_manager.judge.return_value = _judgment(context="Something entirely different.")
    _contribute(client, "A fresh, unrelated note.")

    store = RecordStore(client.db_path)
    drops = {d.contribution_id: d for d in store.list_condensation_drops(scope_id="g_x")}
    assert set(drops) == {a_id, b_id}
    assert drops[a_id].state_at_drop == "corroborated"
    assert drops[b_id].state_at_drop == "unexamined"
    assert drops[a_id].words_before == len(
        ["Widget", "uses", "HTTP", "for", "its", "API.", "Gadget", "uses", "gRPC", "internally."]
    )
    assert drops[a_id].words_after == len(["Something", "entirely", "different."])
    assert drops[a_id].summary_version == drops[b_id].summary_version


def test_a_paraphrase_reads_as_dropped(client) -> None:
    """The known #202 limit, pinned: a substring test cannot tell a paraphrase
    from a deletion, and over-disclosure is the accepted direction."""
    client.mock_manager.judge.return_value = _judgment(context="The database listens on port 5432.")
    d_id = _contribute(client, "The database listens on port 5432.")

    client.mock_manager.judge.return_value = _judgment(
        context="The database now listens on a different port than before."
    )
    _contribute(client, "The port changed.")

    store = RecordStore(client.db_path)
    drops = store.list_condensation_drops(scope_id="g_x")
    assert len(drops) == 1
    assert drops[0].contribution_id == d_id
    assert drops[0].state_at_drop == "unexamined"


def test_no_condensation_writes_no_rows(client) -> None:
    client.mock_manager.judge.return_value = _judgment(context="F content here.")
    _contribute(client, "F content here.")

    client.mock_manager.judge.return_value = _judgment(
        context="F content here. Plus G content, newly added."
    )
    _contribute(client, "G content, newly added.")

    store = RecordStore(client.db_path)
    assert store.list_condensation_drops(scope_id="g_x") == []


def test_append_failure_is_logged_loudly_and_never_blocks_the_amendment(
    client, monkeypatch, caplog
) -> None:
    client.mock_manager.judge.return_value = _judgment(context="Doomed content here.")
    _contribute(client, "Doomed content here.")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated condensation_drops write failure")

    monkeypatch.setattr(RecordStore, "append_condensation_drops", _boom)

    client.mock_manager.judge.return_value = _judgment(context="Replacement content.")
    with caplog.at_level(logging.ERROR):
        _contribute(client, "A fresh note.")

    assert any("condensation_drops write failed" in r.message for r in caplog.records)
    # The summary write itself must have gone through untouched.
    data = client.get("/scopes/g_x/summary").json()
    assert data["context"] == "Replacement content."


def test_a_dropped_correcting_outcome_reads_correcting_and_its_target_gets_no_row(
    client,
) -> None:
    """Aron's note: claim_event_for is keyed by the OUTCOME's own contribution id
    (the event's source), not the corrected target — and the target itself never
    gets a row here (it was replaced, not condensed)."""
    client.mock_manager.judge.return_value = _judgment(context="Service listens on port 8080.")
    h_id = _contribute(client, "Service listens on port 8080.")

    # A failed_corrected outcome on H: I becomes the correcting content,
    # replacing H's claim in the SAME write. new_context is I's own content
    # verbatim, so I is actually PRESENT in context from this point on — the
    # thing a later amendment can drop.
    i_content = "Actually, the service listens on port 9090."
    client.mock_manager.judge.return_value = _judgment(
        context=i_content,
        outcome_disposition="failed_corrected",
    )
    i_id = _contribute(client, i_content, acted_on=h_id)

    store = RecordStore(client.db_path)
    # H excluded: replaced, not condensed.
    assert store.list_condensation_drops(scope_id="g_x") == []

    # A LATER, separate amendment drops I's own (correcting) content.
    client.mock_manager.judge.return_value = _judgment(context="Something else again.")
    _contribute(client, "Yet another note.")

    drops = store.list_condensation_drops(scope_id="g_x")
    assert len(drops) == 1
    assert drops[0].contribution_id == i_id
    assert drops[0].state_at_drop == "correcting"
    assert all(d.contribution_id != h_id for d in drops)


def test_a_dropped_raised_item_reads_raised(client) -> None:
    """P5: an accepted contribution with `raised_from` set is EXAMINED — the
    philosopher's ruling that the order follows the item's ground, not its
    location."""
    store = RecordStore(client.db_path)
    outcome_id = store.append_contribution(
        scope_id="g_x",
        content="an outcome that triggered a raise",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_x", skill="engineer", session_id="sess_x", ts="2026-09-26T09:00:00Z"
        ),
    ).id
    raised_id = store.append_contribution(
        scope_id="g_x",
        content="A raised consequence accepted at this scope.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_x", skill="engineer", session_id="sess_x", ts="2026-09-26T09:00:00Z"
        ),
        raised_from=outcome_id,
    ).id
    store.record_judgment(
        contribution_id=raised_id, decision="accept_as_context", judged_by="scope-manager"
    )
    SummaryStore(client.summaries_dir).write(
        "g_x",
        ScopeSummary(
            scope_id="g_x",
            directives=[],
            context="A raised consequence accepted at this scope.",
            updated_at="2026-09-26T09:00:00+00:00",
        ),
    )

    client.mock_manager.judge.return_value = _judgment(context="Something unrelated entirely.")
    _contribute(client, "An ordinary new note.")

    drops = store.list_condensation_drops(scope_id="g_x")
    assert len(drops) == 1
    assert drops[0].contribution_id == raised_id
    assert drops[0].state_at_drop == "raised"


# --- surfacing: HTTP record read, MCP record read, `strata record` CLI -------------


def test_http_scope_record_carries_condensation_drops(client) -> None:
    client.mock_manager.judge.return_value = _judgment(context="Alpha beta gamma.")
    a_id = _contribute(client, "Alpha beta gamma.")
    client.mock_manager.judge.return_value = _judgment(context="Something else.")
    _contribute(client, "A fresh note.")

    body = client.get("/scopes/g_x/record").json()
    assert "condensation_drops" in body
    assert len(body["condensation_drops"]) == 1
    assert body["condensation_drops"][0]["contribution_id"] == a_id
    assert body["condensation_drops"][0]["state_at_drop"] == "unexamined"


async def test_strata_read_scope_record_carries_condensation_drops(tmp_path) -> None:
    from unittest.mock import patch as _patch

    from tests.test_mcp_server import _load_mcp_module, _make_fleet_yaml

    fleet_path = _make_fleet_yaml(tmp_path)  # g_backend -> g_arch
    db_path = str(tmp_path / "test.db")
    summaries_dir = str(tmp_path / "summaries")
    run_migrations(db_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    store = RecordStore(db_path)
    a_id = store.append_contribution(
        scope_id="g_backend",
        content="A dropped item.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_backend", skill="e", session_id="s1", ts="2026-09-26T00:00:00Z"
        ),
    ).id
    store.record_judgment(
        contribution_id=a_id, decision="accept_as_context", judged_by="scope-manager"
    )
    store.append_condensation_drops(
        scope_id="g_backend",
        summary_version=2,
        budget=500,
        drops=[(a_id, "unexamined", 5, 2)],
    )

    from strata.fleet_config import FleetConfig

    fleet = FleetConfig.load(fleet_path)
    with (
        _patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        _patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        result = await mod.strata_read_scope_record(scope_id="g_backend")

    assert "condensation_drops" in result
    assert len(result["condensation_drops"]) == 1
    assert result["condensation_drops"][0]["contribution_id"] == a_id


def test_cli_record_prints_the_condensation_drop_line(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from strata.__main__ import main
    from strata.settings import get_settings

    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(_FLEET_YAML, encoding="utf-8")
    db_path = tmp_path / "test.db"
    summaries_dir = tmp_path / "summaries"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRATA_FLEET_CONFIG", str(fleet_path))
    monkeypatch.setenv("STRATA_DB_PATH", str(db_path))
    monkeypatch.setenv("STRATA_SUMMARIES_DIR", str(summaries_dir))
    get_settings.cache_clear()
    run_migrations(str(db_path))

    store = RecordStore(str(db_path))
    a_id = store.append_contribution(
        scope_id="g_x",
        content="A dropped item.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_x", skill="e", session_id="s1", ts="2026-09-26T00:00:00Z"
        ),
    ).id
    store.record_judgment(
        contribution_id=a_id, decision="accept_as_context", judged_by="scope-manager"
    )
    store.append_condensation_drops(
        scope_id="g_x",
        summary_version=2,
        budget=500,
        drops=[(a_id, "corroborated", 10, 4)],
    )

    rc = main(["record", "g_x"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "condensed away at v2 (corroborated, 10→4 words, budget 500)" in out
    get_settings.cache_clear()
