"""Regression tests for the V1.3.1 hardening pass.

Covers the review findings fixed alongside issues #39/#44/#45/#46/#47/#50:

- record store: recency window semantics, collision-resistant IDs
- summary store: multi-line directive round-trip, header-lookalike context
- HTTP contribute: parent_version stamping, bad supersedes → 422
- scope-manager: actionable missing-API-key error
- register: never destroys an unparseable settings.json; exact gitignore marker
- storage-path resolver: project config wins, env fallback
- launch refresh: summary rewrites leave a record trail

Vocabulary follows CONTEXT.md.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager
from strata.migrator import run_migrations
from strata.project_config import resolve_storage_paths
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import ScopeManager
from strata.settings import Settings
from strata.summary_store import Directive, ScopeSummary, SummaryStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FLEET_PARENT_CHILD = """\
strata:
  - id: L0
    name: executive
    ordinal: 0
  - id: L1
    name: team
    ordinal: 1
scopes:
  - id: g_parent
    name: Parent
    stratum_id: L0
  - id: g_child
    name: Child
    stratum_id: L1
edges:
  - from: g_child
    to: g_parent
"""


def _contributor(scope_id: str = "g_child") -> ContributorRef:
    return ContributorRef(
        scope_id=scope_id,
        skill="tester",
        session_id="sess_test",
        ts="2026-07-04T12:00:00+00:00",
    )


def _summary(scope_id: str, context: str = "ctx") -> ScopeSummary:
    return ScopeSummary(
        scope_id=scope_id,
        directives=[],
        context=context,
        updated_at="2026-07-04T12:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Record store — recency window + ID width
# ---------------------------------------------------------------------------


def test_list_contributions_limit_returns_newest_window(tmp_path: Path) -> None:
    """limit=N returns the NEWEST N contributions, ordered oldest-first."""
    db_path = str(tmp_path / "s.db")
    run_migrations(db_path)
    with RecordStore(db_path) as rs:
        for i in range(30):
            rs.append_contribution(
                scope_id="g_child",
                content=f"item {i}",
                proposed_classification="context",
                subject=None,
                supersedes=None,
                contributor=_contributor(),
            )
            if i in (9, 19):
                # created_at has second granularity; force distinct seconds
                # across the window boundary so ordering is unambiguous.
                time.sleep(1.1)
        window = rs.list_contributions(scope_id="g_child", limit=10)
    contents = [c.content for c in window]
    assert contents == [f"item {i}" for i in range(20, 30)], (
        "the recency window must contain the newest 10 items, oldest-first"
    )


def test_contribution_ids_are_collision_resistant(tmp_path: Path) -> None:
    """IDs use 8 random bytes (16 hex chars) — token_hex(3) collided by ~5k rows."""
    db_path = str(tmp_path / "s.db")
    run_migrations(db_path)
    with RecordStore(db_path) as rs:
        c = rs.append_contribution(
            scope_id="g_child",
            content="x",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=_contributor(),
        )
    assert c.id.startswith("c_") and len(c.id) == 2 + 16


# ---------------------------------------------------------------------------
# Summary store — round-trip integrity
# ---------------------------------------------------------------------------


def test_multiline_directive_round_trips(tmp_path: Path) -> None:
    """A directive whose content spans lines must survive write → read."""
    store = SummaryStore(str(tmp_path / "summaries"))
    content = "line one\nline two\nline three"
    summary = ScopeSummary(
        scope_id="g_child",
        directives=[
            Directive(
                id="c_1",
                content=content,
                subject="multi",
                source_scope_id="g_child",
                source_skill="tester",
                created_at="2026-07-04T12:00:00+00:00",
            )
        ],
        context="",
        updated_at="2026-07-04T12:00:00+00:00",
    )
    store.write("g_child", summary)
    read_back = store.read("g_child")
    assert read_back is not None
    assert read_back.directives[0].content == content


def test_context_quoting_section_header_round_trips(tmp_path: Path) -> None:
    """Context text containing '## Directives' must not corrupt the parse."""
    store = SummaryStore(str(tmp_path / "summaries"))
    context = "para one\n\n## Directives\nmore text after the lookalike header"
    store.write("g_child", _summary("g_child", context))
    read_back = store.read("g_child")
    assert read_back is not None
    assert read_back.context == context
    assert read_back.directives == []


# ---------------------------------------------------------------------------
# HTTP contribute — parent_version stamp + supersedes validation
# ---------------------------------------------------------------------------


@pytest.fixture()
def parent_child_client(tmp_path: Path):
    """TestClient over a parent/child fleet with a versioned parent summary."""
    db_path = str(tmp_path / "t.db")
    summaries_dir = str(tmp_path / "summaries")
    fleet_yaml_path = str(tmp_path / "fleet.yaml")
    run_migrations(db_path)
    (tmp_path / "fleet.yaml").write_text(_FLEET_PARENT_CHILD, encoding="utf-8")

    # Parent summary at version 3 (write bumps version each time).
    store = SummaryStore(summaries_dir)
    for _ in range(3):
        store.write("g_parent", _summary("g_parent", "parent ctx"))

    settings = Settings(
        db_path=db_path,
        summaries_dir=summaries_dir,
        fleet_yaml_path=fleet_yaml_path,
        anthropic_api_key="test-key",
    )
    application = create_app(settings=settings)

    judgment = MagicMock()
    judgment.decision = "accept_as_context"
    judgment.reasoning = "fine"
    judgment.new_summary = _summary("g_child", "child ctx")
    mock_manager = MagicMock(spec=ScopeManager)
    mock_manager.judge.return_value = judgment
    application.dependency_overrides[get_scope_manager] = lambda: mock_manager

    with TestClient(application) as tc:
        tc.summaries_dir = summaries_dir  # type: ignore[attr-defined]
        yield tc


def _contribute_body(**overrides) -> dict:
    body = {
        "scope_id": "g_child",
        "content": "an observation",
        "proposed_classification": "context",
        "subject": None,
        "supersedes": None,
        "contributor": {
            "scope_id": "g_child",
            "skill": "tester",
            "session_id": "sess_test",
            "ts": "2026-07-04T12:00:00+00:00",
        },
    }
    body.update(overrides)
    return body


def test_http_contribute_stamps_parent_version(parent_child_client) -> None:
    """The written summary records the parent version it was judged against."""
    resp = parent_child_client.post("/contribute", json=_contribute_body())
    assert resp.status_code == 200, resp.text
    written = SummaryStore(parent_child_client.summaries_dir).read("g_child")
    assert written is not None
    assert written.parent_version == 3, (
        "ADR 0004 D4: contribute-time writes must stamp parent_version; "
        "None would mark the summary permanently stale"
    )


def test_http_contribute_bad_supersedes_is_422(parent_child_client) -> None:
    """A nonexistent supersedes reference is client error, not a 500."""
    resp = parent_child_client.post(
        "/contribute", json=_contribute_body(supersedes="c_doesnotexist")
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "supersedes_not_found"


# ---------------------------------------------------------------------------
# Scope-manager — missing API key
# ---------------------------------------------------------------------------


def test_judge_without_api_key_names_the_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """judge() must say 'ANTHROPIC_API_KEY' instead of the raw SDK auth error."""
    import anthropic

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("STRATA_ANTHROPIC_API_KEY", raising=False)
    manager = ScopeManager(client=anthropic.Anthropic(api_key=None))
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        manager.judge(
            scope=MagicMock(),
            stratum=MagicMock(),
            parent_summary=None,
            current_summary=None,
            recent_contributions=[],
            new_contribution=MagicMock(),
        )


# ---------------------------------------------------------------------------
# Register safety — settings.json + gitignore marker
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    return root


def test_register_never_destroys_invalid_settings_json(tmp_path: Path) -> None:
    """Invalid settings.json → merge skipped, original bytes preserved, rc != 0."""
    from strata.__main__ import main

    root = _make_project(tmp_path)
    claude_dir = root / ".claude"
    claude_dir.mkdir()
    original = '{"permissions": {"allow": ["Bash"]}, oops-not-json'
    (claude_dir / "settings.json").write_text(original, encoding="utf-8")

    rc = main(["register", str(root)])

    assert rc == 1
    assert (claude_dir / "settings.json").read_text(encoding="utf-8") == original, (
        "register must NEVER rewrite a settings.json it could not parse"
    )


def test_register_gitignore_comment_is_not_the_marker(tmp_path: Path) -> None:
    """A user comment containing '# Strata' must not suppress the ignore block."""
    from strata.__main__ import main

    root = _make_project(tmp_path)
    (root / ".gitignore").write_text("# Strata console output\n*.log\n", encoding="utf-8")

    rc = main(["register", str(root)])

    assert rc == 0
    content = (root / ".gitignore").read_text(encoding="utf-8")
    assert ".strata/strata.db*" in content, "managed block must still be appended"
    assert "*.log" in content, "user content must be preserved"


def test_register_then_reregister_gitignore_idempotent(tmp_path: Path) -> None:
    """Re-running register must not append the managed block twice."""
    from strata.__main__ import main

    root = _make_project(tmp_path)
    assert main(["register", str(root)]) == 0
    once = (root / ".gitignore").read_text(encoding="utf-8")
    assert main(["register", str(root)]) == 0
    twice = (root / ".gitignore").read_text(encoding="utf-8")
    assert once == twice


# ---------------------------------------------------------------------------
# Storage-path resolver — single source of truth (#44)
# ---------------------------------------------------------------------------


def test_resolver_project_config_wins(tmp_path: Path) -> None:
    """.strata/config.toml beats env-var settings for all three paths."""
    strata_dir = tmp_path / ".strata"
    strata_dir.mkdir()
    (strata_dir / "config.toml").write_text(
        'db = ".strata/strata.db"\n'
        'fleet_yaml = ".strata/fleet.yaml"\n'
        'summaries_dir = ".strata/summaries"\n',
        encoding="utf-8",
    )
    settings = Settings(
        db_path="/env/db.sqlite", summaries_dir="/env/sums", fleet_yaml_path="/env/fleet.yaml"
    )
    paths = resolve_storage_paths(settings, start=tmp_path)
    assert paths.source == "project"
    assert paths.db_path == str(strata_dir / "strata.db")
    assert paths.fleet_yaml_path == str(strata_dir / "fleet.yaml")
    assert paths.project_root == tmp_path.resolve()


def test_resolver_env_fallback(tmp_path: Path) -> None:
    """No project config anywhere up the tree → env settings used."""
    settings = Settings(
        db_path="/env/db.sqlite", summaries_dir="/env/sums", fleet_yaml_path="/env/fleet.yaml"
    )
    paths = resolve_storage_paths(settings, start=tmp_path)
    assert paths.source == "env"
    assert paths.db_path == "/env/db.sqlite"
    assert paths.project_root is None


# ---------------------------------------------------------------------------
# Launch refresh — record trail (the record never lies)
# ---------------------------------------------------------------------------


def test_refresh_scope_writes_record_trail(tmp_path: Path) -> None:
    """_refresh_scope must append its contribution AND judgment to the record."""
    from strata.__main__ import _refresh_scope
    from strata.fleet_config import FleetConfig

    db_path = str(tmp_path / "r.db")
    run_migrations(db_path)
    (tmp_path / "fleet.yaml").write_text(
        "strata:\n  - id: L0\n    name: root\n    ordinal: 0\n"
        "scopes:\n  - id: g_root\n    name: Root\n    stratum_id: L0\n"
        "edges: []\n",
        encoding="utf-8",
    )
    fleet = FleetConfig.load(tmp_path / "fleet.yaml")

    judgment = MagicMock()
    judgment.decision = "accept_as_context"
    judgment.reasoning = "refresh"
    judgment.new_summary = _summary("g_root", "refreshed")
    manager = MagicMock()
    manager.judge.return_value = judgment

    with RecordStore(db_path) as rs:
        summary_store = SummaryStore(str(tmp_path / "summaries"))
        _refresh_scope(
            "g_root",
            fleet_config=fleet,
            record_store=rs,
            summary_store=summary_store,
            manager=manager,
            summary_max_words=500,
        )
        contributions = rs.list_contributions(scope_id="g_root")
        judgments = rs.list_judgments(scope_id="g_root")

    assert len(contributions) == 1, "the refresh event must be appended to the record"
    assert contributions[0].subject == "manager-refresh"
    assert len(judgments) == 1, "the refresh judgment must be recorded"
    assert judgments[0].contribution_id == contributions[0].id
    assert summary_store.read("g_root") is not None


# ---------------------------------------------------------------------------
# register --diff writes nothing (guard for the settings-json handling)
# ---------------------------------------------------------------------------


def test_register_diff_mode_writes_nothing(tmp_path: Path) -> None:
    """--diff on a fresh project reports without creating any files."""
    from strata.__main__ import main

    root = _make_project(tmp_path)
    rc = main(["register", str(root), "--diff"])
    assert rc == 0
    assert not (root / ".strata").exists()
    assert not (root / ".claude" / "settings.json").exists()


def test_register_valid_settings_json_merges_additively(tmp_path: Path) -> None:
    """Existing valid settings keys survive the strata merge untouched."""
    from strata.__main__ import main

    root = _make_project(tmp_path)
    claude_dir = root / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash"]}}), encoding="utf-8"
    )

    rc = main(["register", str(root)])
    assert rc == 0
    data = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    assert data["permissions"] == {"allow": ["Bash"]}
    assert data["mcpServers"]["strata"]["command"] == "strata-mcp"
