"""``strata refresh [SCOPE | --all]`` — the operator's drain (ADR 0014 D6).

The queue is normally drained by the MCP server on bind or read; this exists
for the operator who wants to drain now, and for a fleet whose scopes nobody
has bound in a while.

It runs the SAME refresh `strata launch` runs (implementation pin 6) — one
mechanism, not two: the mechanical parent-directive splice where it changes
something (ADR 0011 D4), then the scope's pending input changes (ADR 0014 D6).
"""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from strata.migrator import run_migrations
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import ScopeManagerJudgment
from strata.settings import Settings, get_settings
from strata.summary_store import ScopeSummary, SummaryStore

FLEET_YAML = """
strata:
  - id: L0
    name: Root
    ordinal: 0
  - id: L1
    name: Child
    ordinal: 1
scopes:
  - id: g_root
    name: Root Scope
    stratum_id: L0
  - id: g_child
    name: Child Scope
    stratum_id: L1
edges:
  - from: g_child
    to: g_root
"""


def _emit(record_store: RecordStore, *, scope_id: str, change_id: str, item_id: str) -> None:
    """Write what a Phase B emitter writes: the notice, then its event row."""
    notice = record_store.append_contribution(
        scope_id=scope_id,
        content=f"[Input change {change_id}: item {item_id} was withdrawn.]",
        proposed_classification="context",
        subject="manager-refresh",
        supersedes=None,
        contributor=ContributorRef(
            scope_id=scope_id,
            skill="scope-manager",
            session_id="refresh",
            ts="2026-09-05T00:00:00+00:00",
        ),
    )
    record_store.append_change_event(
        change_id=change_id,
        contribution_id=notice.id,
        scope_id=scope_id,
        item_id=item_id,
        kind="withdrawn",
        before="Ship behind a flag.",
        after=None,
    )


@pytest.fixture
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    fleet_yaml = tmp_path / "fleet.yaml"
    fleet_yaml.write_text(textwrap.dedent(FLEET_YAML), encoding="utf-8")
    db_path = str(tmp_path / "test.db")
    run_migrations(db_path)
    summaries_dir = tmp_path / "summaries"

    store = SummaryStore(str(summaries_dir))
    for scope_id in ("g_root", "g_child"):
        store.write(
            scope_id,
            ScopeSummary(
                scope_id=scope_id,
                directives=[],
                context=f"{scope_id} context.",
                updated_at="2026-09-05T00:00:00+00:00",
                parent_version=1,
            ),
        )

    settings = Settings(
        db_path=db_path,
        summaries_dir=str(summaries_dir),
        fleet_yaml_path=str(fleet_yaml),
        anthropic_api_key="sk-test",
    )
    monkeypatch.setenv("STRATA_DB_PATH", db_path)
    monkeypatch.setenv("STRATA_SUMMARIES_DIR", str(summaries_dir))
    monkeypatch.setenv("STRATA_FLEET_CONFIG", str(fleet_yaml))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    get_settings.cache_clear()
    yield db_path, settings
    get_settings.cache_clear()


def _run(command_args: argparse.Namespace, settings: Settings) -> tuple[int, list[str]]:
    """Run ``strata refresh`` with a scripted judge; return (exit code, judged scopes)."""
    from strata.__main__ import cmd_refresh

    judged: list[str] = []

    def fake_judge(**kwargs: Any) -> ScopeManagerJudgment:
        scope_id = kwargs["scope"].id
        judged.append(scope_id)
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="Reconciled with the current inputs.",
            new_summary=ScopeSummary(
                scope_id=scope_id,
                directives=[],
                context=f"Refreshed {scope_id}.",
                updated_at="2026-09-05T01:00:00+00:00",
            ),
        )

    manager = MagicMock()
    manager.judge.side_effect = fake_judge
    manager.judge_batch.side_effect = AssertionError("one notice per scope here")

    import anthropic

    with (
        patch("strata.settings.get_settings", return_value=settings),
        patch("strata.scope_manager.ScopeManager", return_value=manager),
        patch("anthropic.Anthropic", return_value=MagicMock(spec=anthropic.Anthropic)),
    ):
        code = cmd_refresh(command_args)
    return code, judged


def test_refreshing_one_scope_drains_its_pending_changes(fleet_env) -> None:  # noqa: ANN001
    db_path, settings = fleet_env
    with RecordStore(db_path) as rs:
        _emit(rs, scope_id="g_child", change_id="chg_a", item_id="p_1")

    code, judged = _run(argparse.Namespace(scope="g_child", all=False), settings)

    assert code == 0
    assert judged == ["g_child"]
    with RecordStore(db_path) as rs:
        assert rs.list_change_events(scope_id="g_child", unprocessed_only=True) == []


def test_refreshing_a_scope_with_nothing_pending_calls_no_judge(fleet_env) -> None:  # noqa: ANN001
    _db_path, settings = fleet_env

    code, judged = _run(argparse.Namespace(scope="g_child", all=False), settings)

    assert code == 0
    assert judged == []


def test_refresh_all_walks_the_fleet_root_first(fleet_env) -> None:  # noqa: ANN001
    db_path, settings = fleet_env
    with RecordStore(db_path) as rs:
        _emit(rs, scope_id="g_child", change_id="chg_a", item_id="p_1")
        _emit(rs, scope_id="g_root", change_id="chg_b", item_id="p_2")

    code, judged = _run(argparse.Namespace(scope=None, all=True), settings)

    assert code == 0
    assert judged == ["g_root", "g_child"]
    with RecordStore(db_path) as rs:
        assert rs.list_change_events(scope_id="g_root", unprocessed_only=True) == []
        assert rs.list_change_events(scope_id="g_child", unprocessed_only=True) == []


def test_refreshing_an_unknown_scope_is_refused(fleet_env) -> None:  # noqa: ANN001
    _db_path, settings = fleet_env

    code, judged = _run(argparse.Namespace(scope="g_nope", all=False), settings)

    assert code == 1
    assert judged == []


def test_a_judge_outage_is_reported_without_losing_the_queue(fleet_env) -> None:  # noqa: ANN001
    """The operator's drain fails loudly — but the events stay, so it can retry."""
    from strata.__main__ import cmd_refresh

    db_path, settings = fleet_env
    with RecordStore(db_path) as rs:
        _emit(rs, scope_id="g_child", change_id="chg_a", item_id="p_1")

    manager = MagicMock()
    manager.judge.side_effect = RuntimeError("scope-manager is down")

    import anthropic

    with (
        patch("strata.settings.get_settings", return_value=settings),
        patch("strata.scope_manager.ScopeManager", return_value=manager),
        patch("anthropic.Anthropic", return_value=MagicMock(spec=anthropic.Anthropic)),
    ):
        code = cmd_refresh(argparse.Namespace(scope="g_child", all=False))

    assert code == 1
    with RecordStore(db_path) as rs:
        assert len(rs.list_change_events(scope_id="g_child", unprocessed_only=True)) == 1


def test_the_refresh_subcommand_is_wired_into_the_parser() -> None:
    from strata.__main__ import _build_parser as build_parser

    args = build_parser().parse_args(["refresh", "g_child"])
    assert args.scope == "g_child"
    assert args.all is False

    args = build_parser().parse_args(["refresh", "--all"])
    assert args.all is True
