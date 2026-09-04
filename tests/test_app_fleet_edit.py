"""API-level tests for the Console fleet-editing endpoints.

Covers ``GET /fleet``, ``POST /fleet/validate``, and ``PUT /fleet`` — see
docs/plans/2026-08-26-console-fleet-edit.md, Tasks 1 and 2.

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

# A second valid fleet (different scope set) used to prove a save actually
# changes what GET /scopes reports.
_FLEET_YAML_V2 = (
    textwrap.dedent("""
    strata:
      - id: L0
        name: Executive
        ordinal: 0

    scopes:
      - id: g_new_scope
        name: New Scope
        stratum_id: L0
        status: active

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

# A scope missing its required `name` field — a pydantic schema-shape error,
# not one of the engine's own invariant checks (issue #182).
_FLEET_YAML_MISSING_FIELD = textwrap.dedent("""
    strata:
      - id: L0
        name: Executive
        ordinal: 0

    scopes:
      - id: g_boss
        stratum_id: L0

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

    def test_schema_shape_error_is_plain_language_returns_422(self, client):
        """A schema-validation failure (missing required field) names the
        offending scope and field, and never leaks pydantic's class name,
        dotted field path, type tag, or documentation URL (issue #182) — the
        same quality bar the YAML-syntax path already meets.
        """
        before_content = client.fleet_yaml_path.read_bytes()

        resp = client.post("/fleet/validate", json={"yaml": _FLEET_YAML_MISSING_FIELD})

        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"]["error"] == "invalid_fleet"
        detail = body["detail"]["detail"]
        assert "g_boss" in detail
        assert "name" in detail
        assert "FleetConfig" not in detail
        assert "pydantic" not in detail.lower()
        assert "errors.pydantic.dev" not in detail
        assert "type=" not in detail
        assert "input_value" not in detail
        assert client.fleet_yaml_path.read_bytes() == before_content

    def test_never_writes_the_file(self, client):
        for payload in (_FLEET_YAML, _FLEET_YAML_BROKEN_SYNTAX, _FLEET_YAML_DUPLICATE_SCOPE):
            before = client.fleet_yaml_path.read_bytes()
            client.post("/fleet/validate", json={"yaml": payload})
            assert client.fleet_yaml_path.read_bytes() == before
        assert not (client.fleet_yaml_path.parent / "fleet.yaml.bak").exists()


# ---------------------------------------------------------------------------
# Task 2: PUT /fleet — save, backup, atomic write, conflict guard
# ---------------------------------------------------------------------------


class TestSaveFleet:
    def test_happy_path_saves_backs_up_and_hot_swaps(self, client):
        get_resp = client.get("/fleet")
        etag = get_resp.json()["etag"]
        previous_content = client.fleet_yaml_path.read_bytes()

        resp = client.put("/fleet", json={"yaml": _FLEET_YAML_V2, "etag": etag})

        assert resp.status_code == 200
        body = resp.json()
        assert body["saved"] is True
        assert body["scopes"] == 1
        assert body["edges"] == 0
        assert "restart" in body["note"]

        # File content is byte-exact with what was submitted.
        assert client.fleet_yaml_path.read_bytes() == _FLEET_YAML_V2.encode("utf-8")

        # The backup holds the PREVIOUS content.
        backup_path = client.fleet_yaml_path.parent / (client.fleet_yaml_path.name + ".bak")
        assert backup_path.read_bytes() == previous_content
        assert str(backup_path) == body["backup"]

        # GET /scopes immediately reflects the new fleet — no restart needed.
        scopes_resp = client.get("/scopes")
        scope_ids = {s["id"] for s in scopes_resp.json()["scopes"]}
        assert scope_ids == {"g_new_scope"}

    def test_invalid_fleet_returns_422_and_leaves_file_and_backup_untouched(self, client):
        get_resp = client.get("/fleet")
        etag = get_resp.json()["etag"]
        before_content = client.fleet_yaml_path.read_bytes()
        backup_path = client.fleet_yaml_path.parent / (client.fleet_yaml_path.name + ".bak")

        resp = client.put("/fleet", json={"yaml": _FLEET_YAML_DUPLICATE_SCOPE, "etag": etag})

        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"]["error"] == "invalid_fleet"
        assert body["detail"]["detail"]
        assert client.fleet_yaml_path.read_bytes() == before_content
        assert not backup_path.exists()

    def test_stale_etag_returns_409_and_leaves_file_untouched(self, client):
        before_content = client.fleet_yaml_path.read_bytes()

        resp = client.put("/fleet", json={"yaml": _FLEET_YAML_V2, "etag": "not-the-real-etag"})

        assert resp.status_code == 409
        body = resp.json()
        assert body["detail"]["error"] == "fleet_changed"
        assert body["detail"]["detail"]
        assert client.fleet_yaml_path.read_bytes() == before_content

    def test_fresh_etag_after_stale_rejection_saves_cleanly(self, client):
        stale_resp = client.put("/fleet", json={"yaml": _FLEET_YAML_V2, "etag": "bogus"})
        assert stale_resp.status_code == 409

        fresh_etag = client.get("/fleet").json()["etag"]
        resp = client.put("/fleet", json={"yaml": _FLEET_YAML_V2, "etag": fresh_etag})

        assert resp.status_code == 200
        assert resp.json()["saved"] is True
        assert client.fleet_yaml_path.read_bytes() == _FLEET_YAML_V2.encode("utf-8")

    def test_comments_and_blank_lines_survive_byte_for_byte(self, client):
        etag = client.get("/fleet").json()["etag"]

        resp = client.put("/fleet", json={"yaml": _FLEET_YAML, "etag": etag})

        assert resp.status_code == 200
        assert client.fleet_yaml_path.read_bytes() == _FLEET_YAML.encode("utf-8")
        saved_text = client.fleet_yaml_path.read_text(encoding="utf-8")
        assert "# Fleet definition — edited via the Console." in saved_text
        assert saved_text.splitlines()[1] == ""
