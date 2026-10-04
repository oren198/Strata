"""End-to-end coverage for the restore act's OPERATOR path, through both
real surfaces — the CLI (`strata operator restore`) and the Console's HTTP
endpoint — not the library function directly (already covered by
tests/test_v17_219_restore.py).

Covers, per Aron's ask:
(a) CLI: `main(["operator", "restore", ...])` — original id, bytes
    identical to the original publish act.
(b) Console: `POST /scopes/{id}/correction-withdrawals/.../restore` through
    the real app — same assertions.
(c) For both: `claim_restored` delivered EXACTLY ONCE to EXACTLY the reader
    scopes that got the reversed `claim_corrected` notice, over a topology
    with 2 readers (a tracked relay and a chain-child reader) plus one
    non-reader scope that gets nothing. No judgment row is written.
(d) Override is by design: the operator path restores a genuine carrier of
    the refuted claim too — no mechanical refusal.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from strata import app as app_module
from strata.app import create_app, get_scope_manager
from strata.change_events import CLAIM_RESTORED
from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.publication import PublishedItem, _write_publication, propose_publish, read_publication
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import PublicationJudgment, ScopeManagerJudgment
from strata.settings import Settings
from strata.summary_store import ScopeSummary, SummaryStore

_FLEET_YAML_4SCOPE = textwrap.dedent("""
    strata:
      - id: L0
        name: Root
        ordinal: 0
      - id: L1
        name: Child
        ordinal: 1
      - id: L2
        name: Grandchild
        ordinal: 2
    scopes:
      - id: g_root
        name: Root
        stratum_id: L0
      - id: g_child
        name: Child
        stratum_id: L1
      - id: g_grandchild
        name: Grandchild
        stratum_id: L2
      - id: g_other
        name: Other (non-reader)
        stratum_id: L1
    edges:
      - from: g_child
        to: g_root
      - from: g_grandchild
        to: g_child
""").strip()

_CLAIM = "The service listens on port 8443."
_CORRECTION = "Used port 8443, the service refused; the right port is unknown."


def _proposer(scope_id: str = "g_root") -> ContributorRef:
    return ContributorRef(
        scope_id=scope_id, skill="engineer", session_id="s1", ts="2026-01-01T00:00:00Z"
    )


class _AcceptingPublicationManager:
    def judge_publication(self, **kwargs: Any) -> PublicationJudgment:  # noqa: ARG002
        return PublicationJudgment(decision="accept", reasoning="Worth relaying.")


def _seed_4scope_correction(tmp_path: Path) -> tuple[str, Path, Path, str, str]:
    """g_root publishes the claim; g_child relays it (tracked); g_grandchild is
    a mere chain-child reader of g_child; g_other has no edge to anything.
    Corrects the claim at g_root (same-scope outcome) -- the cascade withdraws
    g_root's item and g_child's relay copy, and g_grandchild gets a
    claim_corrected notice as g_child's own reader. g_other gets nothing.

    Returns (db_path, fleet_yaml_path, summaries_dir, root_item_id, relay_item_id).
    """
    db_path = str(tmp_path / "test.db")
    fleet_yaml_path = tmp_path / "fleet.yaml"
    fleet_yaml_path.write_text(_FLEET_YAML_4SCOPE, encoding="utf-8")
    summaries_dir = tmp_path / "summaries"
    run_migrations(db_path)
    fleet = FleetConfig.load(fleet_yaml_path)
    summary_store = SummaryStore(str(summaries_dir))

    with RecordStore(db_path) as store:
        target = store.append_contribution(
            scope_id="g_root",
            content=_CLAIM,
            proposed_classification="context",
            subject="service-port",
            supersedes=None,
            contributor=_proposer(),
        )
        store.record_judgment(
            contribution_id=target.id, decision="accept_as_context", judged_by="scope-manager"
        )
        root_act = store.append_publication_act(
            scope_id="g_root",
            act="publish",
            kind="context",
            content=_CLAIM,
            subject="service-port",
            anchors=["subject:service-port"],
            withdraws=None,
            trigger=None,
            proposer=_proposer(),
        )
        store.record_publication_judgment(
            act_id=root_act.id, decision="accept", judged_by="scope-manager"
        )
        _write_publication(
            "g_root",
            [
                PublishedItem(
                    id=root_act.id,
                    kind="context",
                    content=_CLAIM,
                    subject="service-port",
                    anchors=["subject:service-port"],
                    published_at="2026-01-01T00:00:00Z",
                )
            ],
            summaries_dir=str(summaries_dir),
        )
        summary_store.write(
            "g_root",
            ScopeSummary(
                scope_id="g_root", directives=[], context=_CLAIM, updated_at="2026-01-01T00:00:00Z"
            ),
        )

        relay_outcome = propose_publish(
            "g_child",
            _CLAIM,
            "context",
            "service-port",
            ["subject:service-port"],
            _proposer("g_child"),
            fleet=fleet,
            record_store=store,
            summary_store=summary_store,
            scope_manager=_AcceptingPublicationManager(),
            relay_source_scope_id="g_root",
            relay_source_item_id=root_act.id,
        )
        assert relay_outcome.decision == "accept"
        relay_item_id = relay_outcome.act_id

        outcome_judgment = ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="Confirmed by observation.",
            new_summary=ScopeSummary(
                scope_id="g_root",
                directives=[],
                context="The service's port is under investigation.",
                updated_at="2026-01-01T00:01:00Z",
            ),
            outcome_disposition="failed_corrected",
        )
        mock_manager = MagicMock()
        mock_manager.judge.return_value = outcome_judgment

        outcome = store.append_contribution(
            scope_id="g_root",
            content=_CORRECTION,
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_proposer(),
            acted_on=target.id,
        )
        fleet_ = FleetConfig.load(fleet_yaml_path)
        summary_store_ = SummaryStore(str(summaries_dir))
        app_module._judge_and_record(
            contribution=outcome,
            scope=fleet_.get_scope("g_root"),
            stratum=fleet_.strata[0],
            fleet=fleet_,
            record_store=store,
            summary_store=summary_store_,
            scope_manager=mock_manager,
            summary_max_words=500,
        )

    assert read_publication("g_root", summaries_dir=str(summaries_dir)) == []
    assert read_publication("g_child", summaries_dir=str(summaries_dir)) == []
    return db_path, fleet_yaml_path, summaries_dir, root_act.id, relay_item_id


def _assert_restored_and_notified(
    db_path: str, summaries_dir: Path, root_item_id: str, relay_item_id: str
) -> None:
    root_items = read_publication("g_root", summaries_dir=str(summaries_dir))
    assert [i.id for i in root_items] == [root_item_id]
    assert root_items[0].content == _CLAIM  # bytes identical to the original publish act

    child_items = read_publication("g_child", summaries_dir=str(summaries_dir))
    assert [i.id for i in child_items] == [relay_item_id]
    assert child_items[0].content == _CLAIM

    with RecordStore(db_path) as store:
        # No judgment row for the restore act (unjudged operator path).
        root_restore_act = next(
            a
            for a in store.list_publication_acts(scope_id="g_root")
            if a.act == "restore" and a.restores == root_item_id
        )
        judgment_row = store._conn.execute(  # noqa: SLF001 — asserting absence, not a public read
            "SELECT 1 FROM publication_judgments WHERE act_id = ?", (root_restore_act.id,)
        ).fetchone()
        assert judgment_row is None

        # claim_restored EXACTLY ONCE per (scope, item) pair that got the
        # corresponding reversed claim_corrected notice:
        #   g_root        self-notice of its own item X restored    -> 1 (item=root_item_id)
        #   g_child       ordinary reader notice of X restored      -> 1 (item=root_item_id)
        #                 self-notice of its OWN relay copy restored -> 1 (item=relay_item_id)
        #   g_grandchild  ordinary reader notice of the relay copy  -> 1 (item=relay_item_id)
        #   g_other       no edge to anything                       -> 0
        expected_pairs = {
            ("g_root", root_item_id): 1,
            ("g_child", root_item_id): 1,
            ("g_child", relay_item_id): 1,
            ("g_grandchild", relay_item_id): 1,
        }
        for (scope_id, item_id), expected_count in expected_pairs.items():
            restored_events = [
                e
                for e in store.list_change_events(scope_id=scope_id)
                if e.kind == CLAIM_RESTORED and e.item_id == item_id
            ]
            assert len(restored_events) == expected_count, (
                f"{scope_id}/{item_id}: expected {expected_count} claim_restored, "
                f"got {len(restored_events)}"
            )
        other_restored = [
            e for e in store.list_change_events(scope_id="g_other") if e.kind == CLAIM_RESTORED
        ]
        assert other_restored == []


# ---------------------------------------------------------------------------
# (a) CLI
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from strata.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cli_operator_restore_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from strata.__main__ import main

    db_path, fleet_yaml_path, summaries_dir, root_item_id, relay_item_id = _seed_4scope_correction(
        tmp_path
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRATA_FLEET_CONFIG", str(fleet_yaml_path))
    monkeypatch.setenv("STRATA_DB_PATH", db_path)
    monkeypatch.setenv("STRATA_SUMMARIES_DIR", str(summaries_dir))
    from strata.settings import get_settings

    get_settings.cache_clear()

    rc = main(["operator", "restore", "g_root", root_item_id])
    assert rc == 0

    _assert_restored_and_notified(db_path, summaries_dir, root_item_id, relay_item_id)


# ---------------------------------------------------------------------------
# (b) Console / HTTP
# ---------------------------------------------------------------------------


@pytest.fixture()
def http_client(tmp_path):
    db_path, fleet_yaml_path, summaries_dir, root_item_id, relay_item_id = _seed_4scope_correction(
        tmp_path
    )
    settings = Settings(
        db_path=db_path,
        summaries_dir=str(summaries_dir),
        fleet_yaml_path=str(fleet_yaml_path),
        manager_model="claude-haiku-4-5",
        anthropic_api_key="test-key",
    )
    application = create_app(settings=settings)
    application.dependency_overrides[get_scope_manager] = lambda: MagicMock()
    with TestClient(application) as tc:
        tc.db_path = db_path  # type: ignore[attr-defined]
        tc.summaries_dir = summaries_dir  # type: ignore[attr-defined]
        tc.root_item_id = root_item_id  # type: ignore[attr-defined]
        tc.relay_item_id = relay_item_id  # type: ignore[attr-defined]
        yield tc


def test_console_endpoint_operator_restore_end_to_end(http_client) -> None:
    resp = http_client.post(
        f"/scopes/g_root/correction-withdrawals/{http_client.root_item_id}/restore", json={}
    )
    assert resp.status_code == 200
    assert resp.json()["decision"] == "accept"

    _assert_restored_and_notified(
        http_client.db_path,
        http_client.summaries_dir,
        http_client.root_item_id,
        http_client.relay_item_id,
    )


# ---------------------------------------------------------------------------
# (d) Override by design: a genuine carrier is restored anyway, unjudged.
# ---------------------------------------------------------------------------


def test_operator_path_restores_a_genuine_carrier_too(tmp_path: Path) -> None:
    """The owner-judge path would decline a genuine carrier (see
    test_v17_219_restore.py::test_owner_path_decline_changes_nothing). The
    operator path has no judge call at all, so it restores the SAME item
    regardless — no mechanical refusal."""
    from strata.publication import operator_restore

    from .test_v17_219_restore import _setup_same_scope_correction

    db_path, fleet_yaml, summaries_dir, _summary_store, item_id = _setup_same_scope_correction(
        tmp_path
    )
    fleet = FleetConfig.load(fleet_yaml)

    # This item still asserts the refuted claim verbatim (it's the SAME
    # content the sweep withdrew it for) — a judged owner-path restore would
    # decline it (the test above proves that). The operator path restores it
    # anyway: the ground is the operator's own in-person review, not a judge
    # call that could ever refuse.
    with RecordStore(db_path) as store:
        outcome = operator_restore(
            "g_root",
            item_id,
            "operator override: restoring despite still carrying the claim",
            fleet=fleet,
            record_store=store,
            summaries_dir=str(summaries_dir),
        )
    assert outcome.decision == "accept"
    restored = read_publication("g_root", summaries_dir=str(summaries_dir))
    assert [i.id for i in restored] == [item_id]
    assert restored[0].content == _CLAIM
