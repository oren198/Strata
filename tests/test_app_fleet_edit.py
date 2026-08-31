"""API-level tests for the Console fleet-editing endpoints.

Covers ``GET /fleet`` and ``POST /fleet/validate`` — see
docs/plans/2026-08-26-console-fleet-edit.md, Task 1.

All scope-manager calls are mocked — no real Anthropic API calls are made.
Fleet configuration is backed by a real fleet.yaml on disk (tmp_path), never
a live store.
"""

from __future__ import annotations

import hashlib
import textwrap
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager
from strata.migrator import run_migrations
from strata.scope_manager import ScopeManager, ScopeManagerJudgment
from strata.settings import Settings
from strata.summary_store import Directive, ScopeSummary

# ---------------------------------------------------------------------------
# Fixtures / fleet fixtures
# ---------------------------------------------------------------------------

# A valid fleet with a leading comment and a blank line — both must survive a
# round trip byte-for-byte (D1: raw-text save, no YAML round-tripping).
_FLEET_YAML = (
    textwrap.dedent("""
    # Fleet definition — edited via the Console.

    strata:
      - id: L0
        name: Executive
        ordinal: 0
      - id: L1
        name: Function
        ordinal: 1

    scopes:
      - id: g_active
        name: Active Scope
        stratum_id: L1
        status: active
      - id: g_archived
        name: Archived Scope
        stratum_id: L1
        status: archived

    edges: []
    """).strip()
    + "\n"
)

_FLEET_YAML_BROKEN_SYNTAX = "strata: [\n  - this is not valid yaml: ["

_FLEET_YAML_DUPLICATE_SCOPE = textwrap.dedent("""
    strata:
      - id: L0
        name: Executive
        ordinal: 0

    scopes:
      - id: g_dup
        name: A
        stratum_id: L0
        status: active
      - id: g_dup
        name: B
        stratum_id: L0
        status: active

    edges: []
""").strip()


def _make_judgment(
    decision: str = "accept_as_directive",
    reasoning: str = "Test reasoning.",
    summary: ScopeSummary | None = None,
) -> ScopeManagerJudgment:
    return ScopeManagerJudgment(
        decision=decision,  # type: ignore[arg-type]
        reasoning=reasoning,
        new_summary=summary,
    )


def _make_summary(scope_id: str, decision: str) -> ScopeSummary:
    if decision == "accept_as_directive":
        return ScopeSummary(
            scope_id=scope_id,
            directives=[
                Directive(
                    id="c_000001",
                    content="use gRPC, not REST",
                    subject="rpc-protocol",
                    source_scope_id=scope_id,
                    source_skill="architect",
                    created_at="2026-05-23T20:00:00+00:00",
                )
            ],
            context="",
            updated_at="2026-05-23T20:00:01+00:00",
        )
    return ScopeSummary(
        scope_id=scope_id,
        directives=[],
        context="gRPC preferred for inter-service calls.",
        updated_at="2026-05-23T20:00:01+00:00",
    )


@pytest.fixture()
def client(tmp_path):
    """Yield a TestClient backed by a fresh DB + FleetConfig from a tmp fleet.yaml."""
    db_path = str(tmp_path / "test.db")
    summaries_dir = str(tmp_path / "summaries")
    fleet_yaml_path = tmp_path / "fleet.yaml"

    run_migrations(db_path)
    fleet_yaml_path.write_text(_FLEET_YAML, encoding="utf-8")

    settings = Settings(
        db_path=db_path,
        summaries_dir=summaries_dir,
        fleet_yaml_path=str(fleet_yaml_path),
        manager_model="claude-haiku-4-5",
        anthropic_api_key="test-key",
    )

    application = create_app(settings=settings)

    mock_manager = MagicMock(spec=ScopeManager)
    mock_manager.judge.return_value = _make_judgment(
        decision="accept_as_directive",
        summary=_make_summary("g_active", "accept_as_directive"),
    )

    application.dependency_overrides[get_scope_manager] = lambda: mock_manager

    with TestClient(application) as tc:
        tc.fleet_yaml_path = fleet_yaml_path  # type: ignore[attr-defined]
        yield tc


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Task 1: GET /fleet, POST /fleet/validate
# ---------------------------------------------------------------------------


class TestGetFleet:
    def test_returns_raw_text_and_matching_etag(self, client):
        resp = client.get("/fleet")
        assert resp.status_code == 200
        body = resp.json()
        assert body["yaml"] == _FLEET_YAML
        assert body["etag"] == _sha256_hex(_FLEET_YAML)
        assert body["path"].endswith("fleet.yaml")
        assert body["scopes"] == 2
        assert body["edges"] == 0

    def test_preserves_comments_and_blank_lines_verbatim(self, client):
        resp = client.get("/fleet")
        body = resp.json()
        assert "# Fleet definition — edited via the Console." in body["yaml"]
        # The blank line right after the comment must survive.
        assert body["yaml"].splitlines()[1] == ""


class TestValidateFleet:
    def test_valid_fleet_returns_ok_with_counts(self, client):
        before_mtime = client.fleet_yaml_path.stat().st_mtime_ns
        before_content = client.fleet_yaml_path.read_bytes()

        resp = client.post("/fleet/validate", json={"yaml": _FLEET_YAML})

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "scopes": 2, "edges": 0}
        assert client.fleet_yaml_path.stat().st_mtime_ns == before_mtime
        assert client.fleet_yaml_path.read_bytes() == before_content

    def test_broken_yaml_syntax_returns_422(self, client):
        before_content = client.fleet_yaml_path.read_bytes()

        resp = client.post("/fleet/validate", json={"yaml": _FLEET_YAML_BROKEN_SYNTAX})

        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"]["error"] == "invalid_fleet"
        assert body["detail"]["detail"]
        assert client.fleet_yaml_path.read_bytes() == before_content

    def test_duplicate_scope_id_returns_422(self, client):
        before_content = client.fleet_yaml_path.read_bytes()

        resp = client.post("/fleet/validate", json={"yaml": _FLEET_YAML_DUPLICATE_SCOPE})

        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"]["error"] == "invalid_fleet"
        assert body["detail"]["detail"]
        assert client.fleet_yaml_path.read_bytes() == before_content

    def test_never_writes_the_file(self, client):
        for payload in (_FLEET_YAML, _FLEET_YAML_BROKEN_SYNTAX, _FLEET_YAML_DUPLICATE_SCOPE):
            before = client.fleet_yaml_path.read_bytes()
            client.post("/fleet/validate", json={"yaml": payload})
            assert client.fleet_yaml_path.read_bytes() == before
        assert not (client.fleet_yaml_path.parent / "fleet.yaml.bak").exists()
