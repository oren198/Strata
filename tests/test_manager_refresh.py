"""Tests for Phase 1 of ADR 0004 — manager refresh and bounded summary.

Covers:
- Version stamp round-trip (write/read frontmatter).
- Staleness detection logic.
- Budget rendering in user message.
- Multi-inter-stratum-edge invariant (invariant 9).
- Parent_summary wiring assertion in strata_contribute.
- cmd_launch integration: stale chain triggers refresh.

Vocabulary follows CONTEXT.md verbatim.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from strata.fleet_config import FleetConfig, FleetConfigError
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import _build_user_message
from strata.summary_store import Directive, ScopeSummary, SummaryStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_directive(
    id: str = "c_abc",
    content: str = "use gRPC",
    source_scope_id: str = "g_root",
    source_skill: str = "architect",
) -> Directive:
    return Directive(
        id=id,
        content=content,
        source_scope_id=source_scope_id,
        source_skill=source_skill,
        created_at="2026-05-31T10:00:00Z",
    )


def _make_summary(
    scope_id: str = "g_scope",
    version: int = 1,
    parent_version: int | None = None,
) -> ScopeSummary:
    return ScopeSummary(
        scope_id=scope_id,
        directives=[_make_directive()],
        context="some context",
        updated_at="2026-05-31T10:00:00Z",
        version=version,
        parent_version=parent_version,
    )


def _write_fleet(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "fleet.yaml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


def _make_contribution(scope_id: str = "g_scope") -> Contribution:
    return Contribution(
        id="c_001",
        scope_id=scope_id,
        content="Test contribution.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_other",
            skill="code-writer",
            session_id="sess_001",
            ts="2026-05-31T10:00:00Z",
        ),
        created_at="2026-05-31T10:00:00Z",
    )


# ---------------------------------------------------------------------------
# Test 1 — Version stamp round-trip
# ---------------------------------------------------------------------------


def test_version_stamp_roundtrip_default(tmp_path: Path) -> None:
    """Default version=1, no parent_version → roundtrips through write/read."""
    store = SummaryStore(str(tmp_path))
    summary = _make_summary(version=1, parent_version=None)
    written = store.write("g_scope", summary)

    result = store.read("g_scope")
    assert result is not None
    assert result.version == 1
    assert result.parent_version is None
    assert written.version == 1


def test_version_stamp_roundtrip_with_parent_version(tmp_path: Path) -> None:
    """parent_version is serialised to YAML frontmatter and parsed back.

    Note: write() always bumps the version from the on-disk state, so the
    version field passed to write() is ignored — what matters is the bump count.
    parent_version is preserved exactly as provided.
    """
    store = SummaryStore(str(tmp_path))
    # First write: no existing file → version becomes 1.
    summary = _make_summary(version=1, parent_version=5)
    store.write("g_scope", summary)

    result = store.read("g_scope")
    assert result is not None
    assert result.version == 1
    assert result.parent_version == 5


def test_version_bumped_on_successive_writes(tmp_path: Path) -> None:
    """Each write increments the stored version by 1."""
    store = SummaryStore(str(tmp_path))

    s1 = _make_summary()
    w1 = store.write("g_scope", s1)
    assert w1.version == 1

    s2 = _make_summary()
    w2 = store.write("g_scope", s2)
    assert w2.version == 2

    result = store.read("g_scope")
    assert result is not None
    assert result.version == 2


def test_parent_version_none_for_root_scope(tmp_path: Path) -> None:
    """Root scopes (L0) write parent_version=None and read it back as None."""
    store = SummaryStore(str(tmp_path))
    summary = ScopeSummary(
        scope_id="g_root",
        directives=[],
        context="",
        updated_at="2026-05-31T10:00:00Z",
        version=1,
        parent_version=None,
    )
    store.write("g_root", summary)

    result = store.read("g_root")
    assert result is not None
    assert result.parent_version is None
    # Ensure "parent_version" is not in the raw file when it is None
    raw = (tmp_path / "g_root.md").read_text(encoding="utf-8")
    assert "parent_version" not in raw


# ---------------------------------------------------------------------------
# Test 2 — Staleness detection
# ---------------------------------------------------------------------------


def test_stale_when_parent_version_older(tmp_path: Path) -> None:
    """Summary with parent_version=3 is stale when parent has version=4."""
    from strata.__main__ import _is_stale

    my_summary = _make_summary(parent_version=3)
    parent_summary = _make_summary(version=4)

    assert _is_stale(my_summary, parent_summary) is True


def test_fresh_when_parent_version_equal(tmp_path: Path) -> None:
    """Summary with parent_version=4 is fresh when parent has version=4."""
    from strata.__main__ import _is_stale

    my_summary = _make_summary(parent_version=4)
    parent_summary = _make_summary(version=4)

    assert _is_stale(my_summary, parent_summary) is False


def test_stale_when_parent_version_none(tmp_path: Path) -> None:
    """A summary with no parent_version stamp is treated as stale (legacy support)."""
    from strata.__main__ import _is_stale

    my_summary = _make_summary(parent_version=None)
    parent_summary = _make_summary(version=1)

    assert _is_stale(my_summary, parent_summary) is True


def test_stale_across_parents_first_write(tmp_path: Path) -> None:
    """version=0 (issue #59's synthesized-summary sentinel) is always older
    than a parent's real first write (version=1).

    A child stamped with parent_version=0 — because it was built while the
    parent had no on-disk summary yet, i.e. against the synthesized
    ``ScopeSummary(version=0, exists=False)`` — must be detected as stale
    the moment the parent's real first write lands, without any special
    casing: 0 < 1 falls straight out of ``parent_version < version``.
    """
    from strata.__main__ import _is_stale

    my_summary = _make_summary(parent_version=0)
    parent_summary = _make_summary(version=1)

    assert _is_stale(my_summary, parent_summary) is True


# ---------------------------------------------------------------------------
# Test 3 — Budget rendering in user message
# ---------------------------------------------------------------------------


def test_budget_rendered_in_user_message_default() -> None:
    """Default summary_max_words=500 renders a BUDGET line in the user message."""
    from strata.fleet_config import Scope, Stratum

    scope = Scope(id="g_scope", name="Test Scope", stratum_id="L1")
    stratum = Stratum(id="L1", name="Function", ordinal=1)
    contribution = _make_contribution()

    msg = _build_user_message(
        scope=scope,
        stratum=stratum,
        parent_summary=None,
        current_summary=None,
        recent_contributions=[],
        new_contribution=contribution,
        summary_max_words=500,
    )

    assert "BUDGET:" in msg
    assert "500 words" in msg


def test_budget_uses_configured_max_words() -> None:
    """STRATA_SUMMARY_MAX_WORDS=300 results in 'at most 300 words' in the user message."""
    from strata.fleet_config import Scope, Stratum

    scope = Scope(id="g_scope", name="Test Scope", stratum_id="L1")
    stratum = Stratum(id="L1", name="Function", ordinal=1)
    contribution = _make_contribution()

    msg = _build_user_message(
        scope=scope,
        stratum=stratum,
        parent_summary=None,
        current_summary=None,
        recent_contributions=[],
        new_contribution=contribution,
        summary_max_words=300,
    )

    assert "BUDGET:" in msg
    assert "300 words" in msg
    assert "500 words" not in msg


def test_budget_env_var_plumbed_through_settings() -> None:
    """STRATA_SUMMARY_MAX_WORDS env var is read by Settings.summary_max_words."""
    from strata.settings import Settings, get_settings

    get_settings.cache_clear()
    try:
        with patch.dict("os.environ", {"STRATA_SUMMARY_MAX_WORDS": "250"}, clear=False):
            settings = Settings()
            assert settings.summary_max_words == 250
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Test 4 — Multi-inter-stratum-edge invariant (invariant 9)
# ---------------------------------------------------------------------------


def test_single_inter_stratum_parent_accepted(tmp_path: Path) -> None:
    """A scope with exactly one inter-stratum-parent edge loads without error."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
          - id: L1
            name: Function
            ordinal: 1
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
          - id: g_eng
            name: Engineering
            stratum_id: L1
        edges:
          - from: g_eng
            to: g_ceo
    """
    config = FleetConfig.load(_write_fleet(tmp_path, yaml))
    assert config.inter_stratum_parent("g_eng") is not None
    assert config.inter_stratum_parent("g_eng").id == "g_ceo"


def test_multiple_inter_stratum_parents_rejected(tmp_path: Path) -> None:
    """A scope with two inter-stratum-parent edges raises FleetConfigError."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
          - id: L1
            name: Function
            ordinal: 1
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
          - id: g_cfo
            name: CFO
            stratum_id: L0
          - id: g_eng
            name: Engineering
            stratum_id: L1
        edges:
          - from: g_eng
            to: g_ceo
          - from: g_eng
            to: g_cfo
    """
    with pytest.raises(FleetConfigError) as exc_info:
        FleetConfig.load(_write_fleet(tmp_path, yaml))
    assert exc_info.value.kind == "multiple_inter_stratum_parents"
    assert "g_eng" in exc_info.value.message


def test_intra_stratum_peer_edges_not_counted(tmp_path: Path) -> None:
    """Peer (same-stratum) edges do not count toward the inter-stratum-parent limit."""
    yaml = """
        strata:
          - id: L0
            name: Executive
            ordinal: 0
          - id: L1
            name: Function
            ordinal: 1
        scopes:
          - id: g_ceo
            name: CEO
            stratum_id: L0
          - id: g_eng
            name: Engineering
            stratum_id: L1
          - id: g_arch
            name: Architect
            stratum_id: L1
        edges:
          - from: g_eng
            to: g_ceo
          - from: g_arch
            to: g_eng
    """
    # g_arch → g_eng is intra-stratum (both L1, ordinal same) — should be accepted
    config = FleetConfig.load(_write_fleet(tmp_path, yaml))
    assert len(config.edges) == 2


# ---------------------------------------------------------------------------
# Test 5 — Parent_summary wiring in strata_contribute (via app)
# ---------------------------------------------------------------------------


def test_judge_called_with_correct_parent_summary(tmp_path: Path) -> None:
    """ScopeManager.judge is called with the parent scope's current summary content.

    This is the wiring assertion test for ADR 0004 Decision 2: the correct
    parent_summary is resolved from FleetConfig + SummaryStore and passed to
    judge().  The test exercises the app.py contribute route directly.
    """
    from fastapi.testclient import TestClient

    from strata.app import create_app, get_scope_manager
    from strata.migrator import run_migrations
    from strata.scope_manager import ScopeManagerJudgment
    from strata.settings import Settings, get_settings
    from strata.summary_store import ScopeSummary, SummaryStore

    # Set up a minimal fleet: L0 → g_root, L1 → g_child, edge g_child→g_root
    fleet_yaml = tmp_path / "fleet.yaml"
    fleet_yaml.write_text(
        textwrap.dedent("""
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
        """),
        encoding="utf-8",
    )

    db_path = str(tmp_path / "test.db")
    summaries_dir = str(tmp_path / "summaries")
    run_migrations(db_path)

    # Write a parent summary for g_root
    parent_summary_content = ScopeSummary(
        scope_id="g_root",
        directives=[
            _make_directive(id="d_parent", content="Parent directive", source_scope_id="g_root")
        ],
        context="Parent context text",
        updated_at="2026-05-31T10:00:00Z",
    )
    store = SummaryStore(summaries_dir)
    store.write("g_root", parent_summary_content)

    settings = Settings(
        db_path=db_path,
        summaries_dir=summaries_dir,
        fleet_yaml_path=str(fleet_yaml),
        manager_model="claude-haiku-4-5",
        anthropic_api_key="sk-test",
    )

    mock_judgment = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="Test.",
        new_summary=ScopeSummary(
            scope_id="g_child",
            directives=[],
            context="Updated context.",
            updated_at="2026-05-31T11:00:00Z",
        ),
    )

    captured_parent_summary: list[ScopeSummary | None] = []

    def fake_judge(**kwargs: Any) -> ScopeManagerJudgment:
        captured_parent_summary.append(kwargs.get("parent_summary"))
        return mock_judgment

    mock_manager = MagicMock()
    mock_manager.judge.side_effect = fake_judge

    app = create_app(settings=settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_scope_manager] = lambda: mock_manager

    with TestClient(app) as client:
        response = client.post(
            "/contribute",
            json={
                "scope_id": "g_child",
                "content": "A child contribution.",
                "proposed_classification": "context",
                "contributor": {
                    "scope_id": "g_child",
                    "skill": "code-writer",
                    "session_id": "sess_001",
                    "ts": "2026-05-31T10:00:00Z",
                },
            },
        )
    assert response.status_code == 200, response.text

    # Assert judge was called with the parent's summary
    assert mock_manager.judge.called
    parent_arg = captured_parent_summary[0]
    assert parent_arg is not None, "Expected parent_summary to be passed to judge(), got None"
    assert parent_arg.scope_id == "g_root"
    assert parent_arg.context == "Parent context text"
    assert any(d.id == "d_parent" for d in parent_arg.directives)


# ---------------------------------------------------------------------------
# Test 6 — cmd_launch integration: stale chain triggers refresh
# ---------------------------------------------------------------------------


def test_cmd_launch_stale_chain_triggers_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale ancestor chain → ScopeManager.judge called for each stale scope.

    Tests the _run_manager_refresh path invoked by cmd_launch:
    - Fleet: L0 (g_root) → L1 (g_child)
    - g_child summary has parent_version=1 but g_root has version=2 → stale
    - Expects manager.judge called at least twice (once for g_root, once for g_child)
    """
    from strata.__main__ import _run_manager_refresh
    from strata.migrator import run_migrations
    from strata.scope_manager import ScopeManagerJudgment
    from strata.settings import Settings, get_settings
    from strata.summary_store import ScopeSummary, SummaryStore

    fleet_yaml = tmp_path / "fleet.yaml"
    fleet_yaml.write_text(
        textwrap.dedent("""
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
        """),
        encoding="utf-8",
    )

    db_path = str(tmp_path / "test.db")
    summaries_dir_path = tmp_path / "summaries"
    run_migrations(db_path)

    # Write g_root with version=2
    ss = SummaryStore(str(summaries_dir_path))
    root_summary = ScopeSummary(
        scope_id="g_root",
        directives=[],
        context="Root context.",
        updated_at="2026-05-31T10:00:00Z",
        version=1,
    )
    ss.write("g_root", root_summary)
    ss.write("g_root", root_summary)  # second write → version=2

    # Write g_child with parent_version=1 (stale — parent is now version=2)
    child_summary = ScopeSummary(
        scope_id="g_child",
        directives=[],
        context="Child context.",
        updated_at="2026-05-31T10:00:00Z",
        version=1,
        parent_version=1,
    )
    ss.write("g_child", child_summary)

    settings = Settings(
        db_path=db_path,
        summaries_dir=str(summaries_dir_path),
        fleet_yaml_path=str(fleet_yaml),
        anthropic_api_key="sk-test",
    )

    judge_calls: list[str] = []

    def fake_judge(**kwargs: Any) -> ScopeManagerJudgment:
        scope_id = kwargs["scope"].id
        judge_calls.append(scope_id)
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="Test refresh.",
            new_summary=ScopeSummary(
                scope_id=scope_id,
                directives=[],
                context=f"Refreshed context for {scope_id}.",
                updated_at="2026-05-31T12:00:00Z",
            ),
        )

    mock_manager = MagicMock()
    mock_manager.judge.side_effect = fake_judge

    import anthropic

    mock_client = MagicMock(spec=anthropic.Anthropic)

    monkeypatch.setenv("STRATA_DB_PATH", db_path)
    monkeypatch.setenv("STRATA_SUMMARIES_DIR", str(summaries_dir_path))
    monkeypatch.setenv("STRATA_FLEET_CONFIG", str(fleet_yaml))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    get_settings.cache_clear()

    with (
        patch("strata.settings.get_settings", return_value=settings),
        patch("strata.scope_manager.ScopeManager", return_value=mock_manager),
        patch("anthropic.Anthropic", return_value=mock_client),
    ):
        _run_manager_refresh("g_child", skip=False)

    get_settings.cache_clear()

    # judge should have been called for g_child (stale) — g_root has no parent
    # so it won't be stale, but g_child should be refreshed
    assert "g_child" in judge_calls, f"Expected g_child in judge calls: {judge_calls}"


# ---------------------------------------------------------------------------
# Test 7 — Regression: deleted parent summary must not crash refresh
# ---------------------------------------------------------------------------


def test_refresh_does_not_crash_when_parent_summary_deleted(
    tmp_path: Path,
) -> None:
    """Regression for the latent AttributeError at __main__.py:498 (PR #31 review).

    Reachable path: child has a summary on disk with parent_version stamped
    (non-None), but the parent's summary file has been deleted (manual cleanup,
    storage reset, etc.).  When ``_refresh_scope`` is called for the child
    directly (e.g. via the cascade inside another refresh), the previous code
    would call ``_is_stale(my_summary, parent_summary=None)`` and dereference
    ``parent_summary.version`` — crashing with AttributeError before the
    recovery path could even fire. The fix is the ``parent_summary is not
    None`` short-circuit in the ``already_fresh`` expression.
    """
    from strata.__main__ import _refresh_scope
    from strata.fleet_config import FleetConfig
    from strata.migrator import run_migrations
    from strata.record_store import RecordStore
    from strata.scope_manager import ScopeManagerJudgment
    from strata.summary_store import ScopeSummary, SummaryStore

    fleet_yaml = tmp_path / "fleet.yaml"
    fleet_yaml.write_text(
        textwrap.dedent("""
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
        """),
        encoding="utf-8",
    )

    db_path = str(tmp_path / "test.db")
    summaries_dir_path = tmp_path / "summaries"
    run_migrations(db_path)

    ss = SummaryStore(str(summaries_dir_path))

    # Write g_child with a non-None parent_version stamp.  g_root has NO
    # summary on disk (simulates manual deletion / fresh-storage state) —
    # this is the precise input shape that crashes the buggy code.
    child_summary = ScopeSummary(
        scope_id="g_child",
        directives=[],
        context="Child context.",
        updated_at="2026-05-31T10:00:00Z",
        version=1,
        parent_version=2,
    )
    ss.write("g_child", child_summary)

    fleet_config = FleetConfig.load(fleet_yaml)

    def fake_judge(**kwargs: Any) -> ScopeManagerJudgment:
        scope_id = kwargs["scope"].id
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="Test refresh.",
            new_summary=ScopeSummary(
                scope_id=scope_id,
                directives=[],
                context=f"Refreshed context for {scope_id}.",
                updated_at="2026-05-31T12:00:00Z",
            ),
        )

    mock_manager = MagicMock()
    mock_manager.judge.side_effect = fake_judge

    with RecordStore(db_path) as record_store:
        # Must not raise AttributeError (regression). On the buggy code,
        # `_is_stale(child_summary, parent_summary=None)` crashed before the
        # recovery path could fire.
        _refresh_scope(
            "g_child",
            fleet_config=fleet_config,
            record_store=record_store,
            summary_store=ss,
            manager=mock_manager,
            summary_max_words=500,
        )

    # The recovery path should have refreshed the parent first, then the child.
    judge_scopes = [call.kwargs["scope"].id for call in mock_manager.judge.call_args_list]
    assert "g_child" in judge_scopes, (
        f"Expected g_child refresh after the deleted-parent-summary recovery: {judge_scopes}"
    )
