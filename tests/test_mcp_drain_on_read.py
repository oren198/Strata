"""Nobody reads a scope without first bringing it up to date (ADR 0014 D6).

``strata_bind`` and ``strata_read_perspective`` drain the bound scope's pending
input changes BEFORE composing, so the system is correct for a user who never
runs a CLI command at all. Two rules the drain hook lives under:

- **The read never fails** (implementation pin 5). A judge outage during the
  drain leaves the events unprocessed and the read returns anyway; the count
  comes back as ``refresh_pending`` so the state stays visible rather than
  silently absent.
- **No deadlock.** The read path holds no scope lock of its own, and the drain
  takes ``scope_lock`` (ADR 0012) exactly as any summary write does.

The change events are written directly through the record store here: Phase B
owns emission, so these fabricate what a writer would have emitted.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import ScopeManagerJudgment
from strata.summary_store import ScopeSummary, SummaryStore


def _fleet_yaml(tmp_path: Path) -> Path:
    fleet = {
        "strata": [
            {"id": "L0", "name": "executive", "ordinal": 0},
            {"id": "L1", "name": "team", "ordinal": 1},
        ],
        "scopes": [
            {"id": "g_root", "name": "Root", "stratum_id": "L0"},
            {"id": "g_team", "name": "Team", "stratum_id": "L1"},
        ],
        "edges": [{"from": "g_team", "to": "g_root"}],
    }
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return path


def _emit(record_store: RecordStore, *, change_id: str, item_id: str, scope_id: str) -> None:
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


def _setup(tmp_path: Path):  # noqa: ANN201
    from test_mcp_server import _load_mcp_module  # noqa: PLC0415

    db_path = str(tmp_path / "test.db")
    run_migrations(db_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _fleet_yaml(tmp_path)

    summary_store = SummaryStore(summaries_dir)
    summary_store.write(
        "g_team",
        ScopeSummary(
            scope_id="g_team",
            directives=[],
            context="what the team believed before",
            updated_at="2026-09-05T00:00:00+00:00",
        ),
    )

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    # The drain skips outright when no judge is configured (a keyless MCP user
    # must not collect an attempt row per pending event per read), so these
    # tests configure one — the judge itself is patched at every call site.
    mod._settings = mod._settings.model_copy(update={"judge_api_key": "test-key"})
    return mod, db_path, FleetConfig.load(fleet_path)


def _accepting_judgment() -> ScopeManagerJudgment:
    return ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="reconciled with the current inputs",
        new_summary=ScopeSummary(
            scope_id="g_team",
            directives=[],
            context="reconciled",
            updated_at="2026-09-05T01:00:00+00:00",
        ),
        new_context="reconciled",
    )


# ---------------------------------------------------------------------------
# strata_read_perspective
# ---------------------------------------------------------------------------


async def test_reading_a_perspective_drains_the_scope_first(tmp_path: Path) -> None:
    mod, db_path, fleet = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        _emit(rs, change_id="chg_a", item_id="p_1", scope_id="g_team")

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
        patch.object(mod, "_AGENT_SKILL", None),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=_accepting_judgment()),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        result = await mod.strata_read_perspective()

    # `refresh_pending` is what is still OWED after the attempt — a drained
    # scope owes nothing, and the judgment row proves the refresh ran.
    assert result.get("refresh_pending", 0) == 0
    with RecordStore(db_path) as rs:
        assert rs.list_change_events(scope_id="g_team", unprocessed_only=True) == []
        assert len(rs.list_judgments(scope_id="g_team")) == 1


async def test_a_judge_outage_during_the_drain_never_fails_the_read(tmp_path: Path) -> None:
    """Pin 5: the read comes back, and the events are still owed."""
    mod, db_path, fleet = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        _emit(rs, change_id="chg_a", item_id="p_1", scope_id="g_team")

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
        patch.object(mod, "_AGENT_SKILL", None),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch(
            "strata.scope_manager.ScopeManager.judge",
            side_effect=RuntimeError("scope-manager is down"),
        ),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        result = await mod.strata_read_perspective()

    assert result["layers"]
    assert result["refresh_pending"] == 1
    with RecordStore(db_path) as rs:
        assert len(rs.list_change_events(scope_id="g_team", unprocessed_only=True)) == 1


async def test_a_read_with_nothing_pending_reports_zero_and_calls_no_judge(
    tmp_path: Path,
) -> None:
    mod, _db_path, fleet = _setup(tmp_path)

    judge = MagicMock()
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
        patch.object(mod, "_AGENT_SKILL", None),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", judge),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        result = await mod.strata_read_perspective()

    assert result.get("refresh_pending", 0) == 0
    assert judge.call_count == 0


# ---------------------------------------------------------------------------
# strata_bind
# ---------------------------------------------------------------------------


async def test_binding_a_scope_drains_it(tmp_path: Path) -> None:
    mod, db_path, fleet = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        _emit(rs, change_id="chg_a", item_id="p_1", scope_id="g_team")

    with (
        patch.object(mod, "_AGENT_SCOPE", None),
        patch.object(mod, "_AGENT_SKILL", None),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=_accepting_judgment()),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        result = await mod.strata_bind(scope_id="g_team")

    assert result["scope_id"] == "g_team"
    assert result.get("refresh_pending", 0) == 0
    with RecordStore(db_path) as rs:
        assert rs.list_change_events(scope_id="g_team", unprocessed_only=True) == []


async def test_an_unconfirmed_switch_drains_nothing(tmp_path: Path) -> None:
    """A switch that did not happen must not refresh the scope it did not bind."""
    mod, db_path, fleet = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        _emit(rs, change_id="chg_a", item_id="p_1", scope_id="g_team")

    judge = MagicMock()
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_root"),
        patch.object(mod, "_AGENT_SKILL", None),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", judge),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        result = await mod.strata_bind(scope_id="g_team")

    assert result.get("switch_pending") is True
    assert judge.call_count == 0
    with RecordStore(db_path) as rs:
        assert len(rs.list_change_events(scope_id="g_team", unprocessed_only=True)) == 1


async def test_a_judge_outage_during_a_bind_drain_never_fails_the_bind(tmp_path: Path) -> None:
    mod, db_path, fleet = _setup(tmp_path)
    with RecordStore(db_path) as rs:
        _emit(rs, change_id="chg_a", item_id="p_1", scope_id="g_team")

    with (
        patch.object(mod, "_AGENT_SCOPE", None),
        patch.object(mod, "_AGENT_SKILL", None),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch(
            "strata.scope_manager.ScopeManager.judge",
            side_effect=RuntimeError("scope-manager is down"),
        ),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        result = await mod.strata_bind(scope_id="g_team")

    assert result["scope_id"] == "g_team"
    assert result["refresh_pending"] == 1


async def test_a_keyless_server_skips_the_drain_and_still_reports_what_is_owed(
    tmp_path: Path,
) -> None:
    """No judge configured: skip rather than write an attempt row per read.

    The same soft skip `strata launch`'s refresh makes. The events stay, so the
    refresh is still owed and the read says so.
    """
    mod, db_path, fleet = _setup(tmp_path)
    mod._settings = mod._settings.model_copy(
        update={"judge_api_key": None, "anthropic_api_key": None}
    )
    with RecordStore(db_path) as rs:
        _emit(rs, change_id="chg_a", item_id="p_1", scope_id="g_team")

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
        patch.object(mod, "_AGENT_SKILL", None),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        result = await mod.strata_read_perspective()

    assert result["refresh_pending"] == 1
    with RecordStore(db_path) as rs:
        assert rs.list_judgment_attempts(scope_id="g_team") == []
