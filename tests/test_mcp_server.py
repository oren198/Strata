"""Tests for the Strata MCP server tool functions — embedded mode.

The MCP server now operates directly on RecordStore and SummaryStore in-process
(ADR 0004 Decision 1).  No HTTP backend is required.

Tests:
1. strata_contribute writes a row to RecordStore without any HTTP server.
2. strata_read_scope_summary reads from SummaryStore (file on disk) directly.
3. strata_read_perspective returns the scope summary + _v1_limitation note.
4. strata_list_scopes reads fleet.yaml fresh on each call; second call reflects
   a change made between the two calls.
5. strata_read_scope_record reads contributions and judgments from RecordStore
   directly (no fleet info needed, no HTTP).
6. strata_contribute raises RuntimeError when scope is not in fleet config.
7. WAL mode: after RecordStore init, PRAGMA journal_mode returns 'wal'.

The MCP protocol layer (FastMCP, stdio transport) is not tested here — that is
the SDK's responsibility.  Only the tool wrappers are exercised.

Vocabulary follows CONTEXT.md: scope, stratum, directive, context,
contribution, scope summary, perspective, record, provenance.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Make strata importable when running from the repo root.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.fleet_config import FleetConfig  # noqa: E402
from strata.migrator import run_migrations  # noqa: E402
from strata.record_store import ContributorRef, RecordStore  # noqa: E402
from strata.summary_store import ScopeSummary, SummaryStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> str:
    """Apply migrations to a fresh DB and return the path string."""
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    return db_path


def _make_fleet_yaml(tmp_path: Path) -> Path:
    """Write a minimal fleet.yaml and return its path."""
    fleet = {
        "strata": [
            {"id": "L0", "name": "executive", "ordinal": 0},
            {"id": "L1", "name": "team", "ordinal": 1},
        ],
        "scopes": [
            {"id": "g_arch", "name": "Architecture", "stratum_id": "L0"},
            {"id": "g_backend", "name": "Backend Dev", "stratum_id": "L1"},
        ],
        "edges": [
            {"from": "g_arch", "to": "g_backend"},
        ],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return fleet_path


def _make_summary(scope_id: str, context: str = "some context") -> ScopeSummary:
    """Build a minimal ScopeSummary for seeding tests."""
    return ScopeSummary(
        scope_id=scope_id,
        directives=[],
        context=context,
        updated_at="2026-05-30T00:00:00+00:00",
    )


def _make_contributor() -> ContributorRef:
    return ContributorRef(
        scope_id="g_backend",
        skill="strata-developer",
        session_id="sess_test",
        ts="2026-05-30T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Import helper — reload strata_mcp with patched settings pointing to tmp dirs
# ---------------------------------------------------------------------------


def _load_mcp_module(db_path: str, summaries_dir: str, fleet_yaml_path: str):
    """Import (or reload) strata_mcp with settings wired to *tmp_path*."""
    import importlib

    # Remove any prior import so the module-level singletons re-initialise.
    for key in list(sys.modules.keys()):
        if "strata_mcp" in key:
            del sys.modules[key]

    from strata.settings import Settings, get_settings

    get_settings.cache_clear()

    fake_settings = Settings(
        db_path=db_path,
        summaries_dir=summaries_dir,
        fleet_yaml_path=fleet_yaml_path,
        manager_model="claude-haiku-4-5",
        anthropic_api_key=None,
    )

    with patch("strata.settings.get_settings", return_value=fake_settings):
        import mcp_server.strata_mcp as mod

        importlib.reload(mod)

    # Patch module-level singletons to use our tmp-path instances.
    mod._settings = fake_settings
    mod._record_store = RecordStore(db_path)
    mod._summary_store = SummaryStore(summaries_dir)

    return mod


# ---------------------------------------------------------------------------
# Test 1: strata_contribute writes to RecordStore without HTTP server
# ---------------------------------------------------------------------------


def test_contribute_writes_to_record_store_without_http(tmp_path: Path) -> None:
    """strata_contribute must append a contribution to RecordStore in-process."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    # Patch _load_fleet to return our test fleet directly (avoids disk path issues).
    fleet = FleetConfig.load(fleet_path)

    # We mock the scope-manager so we don't need a real Anthropic key.
    fake_judgment = MagicMock()
    fake_judgment.decision = "accept_as_context"
    fake_judgment.reasoning = "Valid observation."
    fake_judgment.new_summary = _make_summary("g_arch", "updated context")

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_AGENT_SKILL", "strata-developer"),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=fake_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        result = mod.strata_contribute(
            scope_id="g_arch",
            content="All services should use structured logging.",
            proposed_classification="context",
            subject="logging-standard",
            supersedes=None,
        )

    # Result shape matches the existing contract.
    assert "contribution_id" in result
    assert result["judgment"]["decision"] == "accept_as_context"
    assert result["judgment"]["summary_updated"] is True

    # The contribution must be in the RecordStore.
    with RecordStore(db_path) as rs:
        contributions = rs.list_contributions(scope_id="g_arch")
    assert len(contributions) == 1
    assert contributions[0].content == "All services should use structured logging."
    assert contributions[0].contributor.skill == "strata-developer"


# ---------------------------------------------------------------------------
# Test 2: strata_read_scope_summary reads from SummaryStore (file on disk)
# ---------------------------------------------------------------------------


def test_read_scope_summary_reads_from_summary_store(tmp_path: Path) -> None:
    """strata_read_scope_summary must read the ScopeSummary from disk directly."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    # Seed a summary on disk.
    summary = _make_summary("g_arch", "arch context from disk")
    ss = SummaryStore(summaries_dir)
    ss.write("g_arch", summary)

    fleet = FleetConfig.load(fleet_path)

    with patch.object(mod, "_load_fleet", return_value=fleet):
        mod._summary_store = ss
        result = mod.strata_read_scope_summary("g_arch")

    assert result["scope_id"] == "g_arch"
    assert result["context"] == "arch context from disk"
    assert result["directives"] == []
    assert "updated_at" in result


# ---------------------------------------------------------------------------
# Test 3: strata_read_perspective returns summary + _v1_limitation
# ---------------------------------------------------------------------------


def test_read_perspective_returns_summary_plus_limitation_note(tmp_path: Path) -> None:
    """strata_read_perspective must return the scope summary with a _v1_limitation key."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    summary = _make_summary("g_arch", "arch context")
    ss = SummaryStore(summaries_dir)
    ss.write("g_arch", summary)

    fleet = FleetConfig.load(fleet_path)

    with patch.object(mod, "_load_fleet", return_value=fleet):
        mod._summary_store = ss
        result = mod.strata_read_perspective("g_arch")

    assert result["scope_id"] == "g_arch"
    assert result["context"] == "arch context"
    assert "_v1_limitation" in result
    assert "post-V1" in result["_v1_limitation"]


# ---------------------------------------------------------------------------
# Test 4: strata_list_scopes re-reads fleet.yaml on each call
# ---------------------------------------------------------------------------


def test_list_scopes_re_reads_fleet_yaml_each_call(tmp_path: Path) -> None:
    """strata_list_scopes must reflect changes to fleet.yaml between calls."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    # First call — two scopes.
    result1 = mod.strata_list_scopes()
    scope_ids_1 = {s["id"] for s in result1["scopes"]}
    assert "g_arch" in scope_ids_1
    assert "g_backend" in scope_ids_1

    # Mutate fleet.yaml: add a new scope.
    raw = yaml.safe_load(fleet_path.read_text(encoding="utf-8"))
    raw["scopes"].append({"id": "g_frontend", "name": "Frontend Dev", "stratum_id": "L1"})
    fleet_path.write_text(yaml.dump(raw, default_flow_style=False), encoding="utf-8")

    # Second call — must reflect the addition without a restart.
    result2 = mod.strata_list_scopes()
    scope_ids_2 = {s["id"] for s in result2["scopes"]}
    assert "g_frontend" in scope_ids_2, (
        "strata_list_scopes did not pick up fleet.yaml change between calls"
    )


# ---------------------------------------------------------------------------
# Test 5: strata_read_scope_record reads directly from RecordStore
# ---------------------------------------------------------------------------


def test_read_scope_record_reads_from_record_store(tmp_path: Path) -> None:
    """strata_read_scope_record must return contributions and judgments from RecordStore."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    # Seed a contribution and judgment directly.
    contributor = _make_contributor()
    with RecordStore(db_path) as rs:
        c = rs.append_contribution(
            scope_id="g_arch",
            content="Use WAL mode for SQLite.",
            proposed_classification="directive",
            subject="wal-mode",
            supersedes=None,
            contributor=contributor,
        )
        rs.record_judgment(
            contribution_id=c.id,
            decision="accept_as_directive",
            judged_by="scope-manager",
            notes="Good call.",
        )

    mod._record_store = RecordStore(db_path)
    result = mod.strata_read_scope_record("g_arch")

    assert len(result["contributions"]) == 1
    assert result["contributions"][0]["content"] == "Use WAL mode for SQLite."
    assert len(result["judgments"]) == 1
    assert result["judgments"][0]["decision"] == "accept_as_directive"


# ---------------------------------------------------------------------------
# Test 6: strata_contribute raises RuntimeError for unknown scope
# ---------------------------------------------------------------------------


def test_contribute_raises_for_unknown_scope(tmp_path: Path) -> None:
    """strata_contribute must raise RuntimeError when the scope is not in fleet config."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)

    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        pytest.raises(RuntimeError, match="Scope not found"),
    ):
        mod.strata_contribute(
            scope_id="g_nonexistent",
            content="This should fail.",
            proposed_classification="context",
        )


# ---------------------------------------------------------------------------
# Test 7: WAL mode — PRAGMA journal_mode returns 'wal' after RecordStore init
# ---------------------------------------------------------------------------


def test_wal_mode_enabled_after_record_store_init(tmp_path: Path) -> None:
    """RecordStore must enable WAL journal mode on every connection open."""
    db_path = _make_db(tmp_path)

    with RecordStore(db_path) as rs:
        row = rs._conn.execute("PRAGMA journal_mode;").fetchone()
        assert row is not None
        journal_mode = row[0]

    assert journal_mode == "wal", (
        f"Expected journal_mode='wal', got {journal_mode!r}. "
        "Check that RecordStore.__init__ issues PRAGMA journal_mode=WAL."
    )
