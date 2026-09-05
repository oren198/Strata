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
        ancestor_directives=None,
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
        ancestor_directives=None,
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


def test_publication_max_words_default_and_env_var() -> None:
    """Settings.publication_max_words defaults to 500 and honors STRATA_PUBLICATION_MAX_WORDS."""
    from strata.settings import Settings, get_settings

    get_settings.cache_clear()
    try:
        assert Settings().publication_max_words == 500
        with patch.dict("os.environ", {"STRATA_PUBLICATION_MAX_WORDS": "120"}, clear=False):
            settings = Settings()
            assert settings.publication_max_words == 120
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


def test_judge_called_with_the_ancestor_walk(tmp_path: Path) -> None:
    """ScopeManager.judge is called with the scope's inter-stratum ancestor walk.

    The wiring assertion for ADR 0004 Decision 2, re-pointed by ADR 0015 D2:
    what the caller resolves and hands the judge is the root-first ancestor
    walk — the same one composition reads — not the parent's whole summary.
    The test exercises the app.py contribute route directly.
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

    captured_walk: list[object] = []

    def fake_judge(**kwargs: Any) -> ScopeManagerJudgment:
        captured_walk.append(kwargs.get("ancestor_directives"))
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
    walk = captured_walk[0]
    assert walk is not None, "Expected ancestor_directives to be passed to judge(), got None"
    assert [scope_id for scope_id, _ in walk] == ["g_root"]
    assert any(d.id == "d_parent" for _, directives in walk for d in directives)


# ---------------------------------------------------------------------------
# Test 6 — cmd_launch integration: stale chain triggers refresh
# ---------------------------------------------------------------------------


def test_cmd_launch_refreshes_a_scope_with_pending_input_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending input change is what triggers a launch refresh (ADR 0014 D6).

    Version-comparison staleness is gone (implementation pin 6): the child here
    is NOT version-stale — its ``parent_version`` matches the parent's current
    version — and it is refreshed anyway, because a change event says an input
    it rests on moved. One mechanism, and it is the change event.
    """
    from strata.__main__ import _run_manager_refresh
    from strata.migrator import run_migrations
    from strata.record_store import RecordStore
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

    ss = SummaryStore(str(summaries_dir_path))
    ss.write(
        "g_root",
        ScopeSummary(
            scope_id="g_root",
            directives=[],
            context="Root context.",
            updated_at="2026-05-31T10:00:00Z",
        ),
    )
    root_version = ss.read("g_root").version
    ss.write(
        "g_child",
        ScopeSummary(
            scope_id="g_child",
            directives=[],
            context="Child context.",
            updated_at="2026-05-31T10:00:00Z",
            parent_version=root_version,
        ),
    )

    # The notice and its event row — what a Phase B emitter writes.
    with RecordStore(db_path) as rs:
        notice = rs.append_contribution(
            scope_id="g_child",
            content="[Input change chg_a: item p_1 was withdrawn.]",
            proposed_classification="context",
            subject="manager-refresh",
            supersedes=None,
            contributor=ContributorRef(
                scope_id="g_child",
                skill="scope-manager",
                session_id="refresh",
                ts="2026-09-05T00:00:00+00:00",
            ),
        )
        rs.append_change_event(
            change_id="chg_a",
            contribution_id=notice.id,
            scope_id="g_child",
            item_id="p_1",
            kind="withdrawn",
            before="Ship behind a flag.",
            after=None,
        )

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

    # Only the scope that owed a refresh was judged: g_root has nothing
    # pending and no parent to inherit from, so it costs no judge call.
    assert judge_calls == ["g_child"]
    with RecordStore(db_path) as rs:
        assert rs.list_change_events(scope_id="g_child", unprocessed_only=True) == []


# ---------------------------------------------------------------------------
# Test 7 — Regression: deleted parent summary must not crash refresh
# ---------------------------------------------------------------------------


def test_refresh_does_not_crash_when_parent_summary_deleted(
    tmp_path: Path,
) -> None:
    """Regression for the latent AttributeError at __main__.py:498 (PR #31 review).

    Reachable path: the child has a summary on disk with a ``parent_version``
    stamp, but the parent's summary file has been deleted (manual cleanup,
    storage reset). The original defect dereferenced the absent parent summary
    while comparing versions; that comparison is gone (ADR 0014
    implementation pin 6), but the input shape is still real and a refresh
    must still survive it — and still drain what the scope owes.
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
        notice = record_store.append_contribution(
            scope_id="g_child",
            content="[Input change chg_a: item p_1 was withdrawn.]",
            proposed_classification="context",
            subject="manager-refresh",
            supersedes=None,
            contributor=ContributorRef(
                scope_id="g_child",
                skill="scope-manager",
                session_id="refresh",
                ts="2026-09-05T00:00:00+00:00",
            ),
        )
        record_store.append_change_event(
            change_id="chg_a",
            contribution_id=notice.id,
            scope_id="g_child",
            item_id="p_1",
            kind="withdrawn",
        )
        # Must not raise: the parent summary the child's stamp refers to is
        # simply not there.
        _refresh_scope(
            "g_child",
            fleet_config=fleet_config,
            record_store=record_store,
            summary_store=ss,
            manager=mock_manager,
            summary_max_words=500,
        )

    judge_scopes = [call.kwargs["scope"].id for call in mock_manager.judge.call_args_list]
    assert judge_scopes == ["g_child"], (
        f"Expected the child's own drain to run with the parent summary gone: {judge_scopes}"
    )


# ---------------------------------------------------------------------------
# Test 8 — ADR 0011 D4: parent directives are spliced in mechanically and the
# refresh amendment is context-only
# ---------------------------------------------------------------------------


def _two_stratum_fleet(tmp_path: Path) -> Path:
    return _write_fleet(
        tmp_path,
        """
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
        """,
    )


def _refresh_child(tmp_path: Path, *, child_summary: ScopeSummary | None) -> tuple[Any, Any]:
    """Run ``_refresh_scope`` for g_child against a parent holding one directive.

    Returns ``(written_child_summary, judge_call_kwargs)``.
    """
    from strata.__main__ import _refresh_scope
    from strata.fleet_config import FleetConfig
    from strata.migrator import run_migrations
    from strata.record_store import RecordStore
    from strata.scope_manager import ScopeManagerJudgment

    fleet_yaml = _two_stratum_fleet(tmp_path)
    db_path = str(tmp_path / "test.db")
    run_migrations(db_path)
    store = SummaryStore(str(tmp_path / "summaries"))

    parent_directive = Directive(
        id="c_parent_rule",
        content="All services must ship behind a flag.\nNo exceptions.",
        subject="rollout",
        source_scope_id="g_root",
        source_skill="scope-manager",
        created_at="2026-05-31T09:00:00Z",
    )
    store.write(
        "g_root",
        ScopeSummary(
            scope_id="g_root",
            directives=[parent_directive],
            context="Root context.",
            updated_at="2026-05-31T10:00:00Z",
        ),
    )
    if child_summary is not None:
        store.write("g_child", child_summary)

    def fake_judge(**kwargs: Any) -> ScopeManagerJudgment:
        # Stand in for the engine's mechanical apply: an amendment that only
        # rewrites the context leaves the directives list exactly as the judge
        # received it.
        received = kwargs["current_summary"]
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="Reconciled the digest with the refreshed parent state.",
            new_summary=received.model_copy(update={"context": "Reconciled context."}),
            new_context="Reconciled context.",
        )

    mock_manager = MagicMock()
    mock_manager.judge.side_effect = fake_judge

    with RecordStore(db_path) as record_store:
        _refresh_scope(
            "g_child",
            fleet_config=FleetConfig.load(fleet_yaml),
            record_store=record_store,
            summary_store=store,
            manager=mock_manager,
            summary_max_words=500,
        )

    written = store.read("g_child")
    call = next(c for c in mock_manager.judge.call_args_list if c.kwargs["scope"].id == "g_child")
    return written, call.kwargs


def test_refresh_splices_parent_directive_byte_exactly(tmp_path: Path) -> None:
    """A stale child picks up the parent's directive verbatim — ids, provenance, bytes."""
    stale_child = ScopeSummary(
        scope_id="g_child",
        directives=[
            _make_directive(id="c_local", content="Local rule.", source_scope_id="g_child")
        ],
        context="Child context.",
        updated_at="2026-05-31T10:00:00Z",
        parent_version=0,
    )

    written, judge_kwargs = _refresh_child(tmp_path, child_summary=stale_child)

    assert written is not None
    spliced = next(d for d in written.directives if d.id == "c_parent_rule")
    assert spliced.content == "All services must ship behind a flag.\nNo exceptions."
    assert spliced.subject == "rollout"
    assert spliced.source_scope_id == "g_root"
    assert spliced.source_skill == "scope-manager"
    assert spliced.created_at == "2026-05-31T09:00:00Z"
    # The child's own directive survives untouched.
    assert any(d.id == "c_local" for d in written.directives)
    # The judge saw the already-spliced summary — it had nothing to copy.
    assert any(d.id == "c_parent_rule" for d in judge_kwargs["current_summary"].directives)


def test_refresh_judge_call_is_context_only(tmp_path: Path) -> None:
    """ADR 0011 D4: the refresh amendment may carry only context and lifecycle ops."""
    stale_child = ScopeSummary(
        scope_id="g_child",
        directives=[],
        context="Child context.",
        updated_at="2026-05-31T10:00:00Z",
        parent_version=0,
    )

    _written, judge_kwargs = _refresh_child(tmp_path, child_summary=stale_child)

    assert judge_kwargs["mode"] == "splice_refresh"


def test_refresh_splices_into_a_child_with_no_summary_yet(tmp_path: Path) -> None:
    """A child that has never been written still inherits the parent's directives."""
    written, _judge_kwargs = _refresh_child(tmp_path, child_summary=None)

    assert written is not None
    assert [d.id for d in written.directives] == ["c_parent_rule"]
    assert written.context == "Reconciled context."


def test_refresh_records_the_judgment_trail(tmp_path: Path) -> None:
    """The refresh contribution and its judgment still land in the record."""
    from strata.record_store import RecordStore

    _written, _judge_kwargs = _refresh_child(
        tmp_path,
        child_summary=ScopeSummary(
            scope_id="g_child",
            directives=[],
            context="Child context.",
            updated_at="2026-05-31T10:00:00Z",
            parent_version=0,
        ),
    )

    with RecordStore(str(tmp_path / "test.db")) as rs:
        contributions = rs.list_contributions(scope_id="g_child")
        assert [c.subject for c in contributions] == ["manager-refresh"]
        judgments = rs.list_judgments(scope_id="g_child")
        assert len(judgments) == 1
        assert judgments[0].judged_by == "scope-manager"
