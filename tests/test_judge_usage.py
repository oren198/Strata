"""Judge usage metering (#246).

Every judge call records one row at the choke point. Tokens are the
response's usage. The daily cap refuses a new judgment the way an
unreachable judge does, and does not interrupt a judgment already started.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import strata.judge_usage as judge_usage
import strata.scope_manager as scope_manager
from strata.app import create_app, get_scope_manager
from strata.freshness import _default_draft_fn
from strata.judge_usage import (
    JudgeDailyCapReached,
    bind_usage_db,
    call_kind_from_request,
    cap_status_text,
    format_judge_usage_report,
    judge_usage_report,
    kind_is_gated,
    metered_messages_create,
    unbind_usage_db,
)
from strata.migrator import _default_migrations_dir, run_migrations
from strata.scope_manager import ScopeManager
from strata.settings import Settings

from .test_scope_manager import (
    CURRENT_SUMMARY,
    NEW_CONTRIBUTION,
    RECENT_ROW,
    SCOPE,
    STRATUM,
    _accept_context_input,
    _fake_response,
)

_JUDGE_BUILDER_SHA256 = "b703d4582bfd827cf8ba2a9ca6e90a8279ee771d24034b47be771a66d70c7a7e"

_FLEET = """\
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


def _db(tmp_path: Path) -> str:
    path = str(tmp_path / "strata.db")
    run_migrations(path)
    return path


def _bound(db_path: str):
    previous = bind_usage_db(db_path)
    return previous


def _usage(input_tokens: int, output_tokens: int, *, model: str = "claude-haiku-4-5"):
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return usage


def _response(tool_input: dict | None, input_tokens: int, output_tokens: int):
    if tool_input is None:
        response = MagicMock()
        response.content = []
    else:
        response = _fake_response(tool_input)
    response.usage = _usage(input_tokens, output_tokens)
    response.model = "claude-haiku-4-5"
    return response


def _rows(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM judge_usage ORDER BY rowid").fetchall())
    finally:
        conn.close()


def _judge(manager: ScopeManager) -> None:
    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[RECENT_ROW],
        new_contribution=NEW_CONTRIBUTION,
    )


def test_judge_message_builders_are_byte_identical() -> None:
    """Part 3 does not change what the judge is shown."""
    digest = hashlib.sha256()
    for name in ("_build_judge_preamble", "_build_user_message", "_build_batch_user_message"):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(inspect.getsource(getattr(scope_manager, name)).encode())
    assert digest.hexdigest() == _JUDGE_BUILDER_SHA256


def test_call_kind_comes_from_the_request() -> None:
    first = {
        "tool_choice": {"type": "tool", "name": "submit_judgment"},
        "messages": [{"role": "user", "content": "SCOPE: A (id=g_a)\n"}],
    }
    assert call_kind_from_request(first) == "judgment"
    assert kind_is_gated("judgment") is True
    follow = {
        "tool_choice": {"type": "tool", "name": "submit_judgment"},
        "messages": [
            {"role": "user", "content": "SCOPE: A (id=g_a)\n"},
            {"role": "assistant", "content": []},
            {"role": "user", "content": "Call submit_judgment again."},
        ],
    }
    assert call_kind_from_request(follow) == "corrective_reask"
    assert kind_is_gated("corrective_reask") is False
    overflow = {
        "tool_choice": {"type": "tool", "name": "submit_judgment"},
        "messages": [
            *follow["messages"][:2],
            {"role": "user", "content": "this summary is 900 words — over the BUDGET of 500"},
        ],
    }
    assert call_kind_from_request(overflow) == "condensation"
    assert kind_is_gated("condensation") is False
    assert kind_is_gated("carrier_check") is True
    assert kind_is_gated("drafter") is True
    assert kind_is_gated("doctor_probe") is True
    assert kind_is_gated("targeted_reask") is False
    inherited = {
        "tool_choice": {"type": "tool", "name": "classify_inherited_relation"},
        "messages": [{"role": "user", "content": "SCOPE: A (id=g_a)\n"}],
    }
    assert call_kind_from_request(inherited) == "inherited_relation_recheck"
    assert kind_is_gated("inherited_relation_recheck") is False
    unknown = {
        "tool_choice": {"type": "tool", "name": "some_new_reask"},
        "messages": [{"role": "user", "content": "SCOPE: A (id=g_a)\n"}],
    }
    assert call_kind_from_request(unknown) == "some_new_reask"
    assert kind_is_gated("some_new_reask") is False
    source = Path(scope_manager.__file__).read_text(encoding="utf-8")
    assert "over the BUDGET" in source


def test_every_scope_manager_tool_is_mapped() -> None:
    """A tool constant the meter does not know would be ungated, and this fails."""
    tools = [
        scope_manager.JUDGE_TOOL,
        scope_manager.JUDGE_BATCH_TOOL,
        scope_manager.PUBLICATION_JUDGE_TOOL,
        scope_manager.BOOTSTRAP_JUDGE_TOOL,
        scope_manager.CLAIM_CARRIER_TOOL,
        scope_manager.INTERIOR_ASSERTION_TOOL,
        scope_manager.ATTRIBUTION_RECHECK_TOOL,
        scope_manager.RELATION_RECHECK_TOOL,
        scope_manager.CLASSIFY_INHERITED_RELATION_TOOL,
    ]
    expected = {
        "submit_judgment": "judgment",
        "submit_batch_judgment": "batch_judgment",
        "submit_publication_judgment": "publication",
        "submit_bootstrap_publication": "bootstrap",
        "classify_claim_carriers": "carrier_check",
        "classify_interior_assertion": "targeted_reask",
        "recheck_attribution": "attribution_recheck",
        "recheck_relation": "relation_recheck",
        "classify_inherited_relation": "inherited_relation_recheck",
    }
    seen: set[str] = set()
    for tool in tools:
        name = tool["name"]
        seen.add(name)
        kind = call_kind_from_request(
            {
                "tool_choice": {"type": "tool", "name": name},
                "messages": [{"role": "user", "content": "ping"}],
            }
        )
        assert kind == expected[name]
        assert kind != name
        if name == "classify_inherited_relation":
            assert kind_is_gated(kind) is False
    assert seen == set(expected)


def test_messages_create_text_lives_only_in_judge_usage() -> None:
    root = Path(judge_usage.__file__).resolve().parent
    needle = ".messages.create("
    hits = [
        str(path.relative_to(root))
        for path in sorted(root.rglob("*.py"))
        if path.name != "judge_usage.py" and needle in path.read_text(encoding="utf-8")
    ]
    assert hits == []
    assert needle in (root / "judge_usage.py").read_text(encoding="utf-8")


def test_one_row_uses_response_tokens_not_an_estimate(tmp_path: Path, monkeypatch) -> None:
    db_path = _db(tmp_path)
    previous = _bound(db_path)
    monkeypatch.delenv("STRATA_JUDGE_DAILY_TOKEN_CAP", raising=False)
    monkeypatch.delenv("STRATA_JUDGE_PRICE_TABLE", raising=False)
    client = MagicMock()
    client.messages.create.return_value = _response(_accept_context_input(), 11, 7)
    try:
        _judge(ScopeManager(client=client))
    finally:
        unbind_usage_db(previous)
    rows = _rows(db_path)
    assert len(rows) == 1
    assert rows[0]["call_kind"] == "judgment"
    assert rows[0]["input_tokens"] == 11
    assert rows[0]["output_tokens"] == 7
    assert rows[0]["scope_id"] == SCOPE.id
    assert rows[0]["contribution_id"] == NEW_CONTRIBUTION.id
    assert rows[0]["model"] == "claude-haiku-4-5"
    assert rows[0]["provider"] is None
    sent = client.messages.create.call_args.kwargs
    assert "usage" not in sent
    assert "extra_body" not in sent


def test_recording_failure_does_not_block_the_judgment(tmp_path: Path, monkeypatch) -> None:
    db_path = _db(tmp_path)
    previous = _bound(db_path)
    monkeypatch.delenv("STRATA_JUDGE_DAILY_TOKEN_CAP", raising=False)

    def _boom(*_args, **_kwargs) -> None:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(judge_usage, "_insert", _boom)
    client = MagicMock()
    client.messages.create.return_value = _response(_accept_context_input(), 3, 1)
    try:
        _judge(ScopeManager(client=client))
    finally:
        unbind_usage_db(previous)
    assert client.messages.create.call_count == 1
    assert _rows(db_path) == []


def test_reask_still_runs_after_the_cap_is_crossed(tmp_path: Path, monkeypatch) -> None:
    db_path = _db(tmp_path)
    previous = _bound(db_path)
    monkeypatch.setenv("STRATA_JUDGE_DAILY_TOKEN_CAP", "10")
    client = MagicMock()
    client.messages.create.side_effect = [
        _response(None, 80, 30),
        _response(_accept_context_input(), 5, 5),
    ]
    try:
        _judge(ScopeManager(client=client))
    finally:
        unbind_usage_db(previous)
    assert client.messages.create.call_count == 2
    rows = _rows(db_path)
    assert [row["call_kind"] for row in rows] == ["judgment", "corrective_reask"]
    assert rows[0]["input_tokens"] == 80
    assert rows[1]["input_tokens"] == 5


def test_cap_blocks_the_first_call(tmp_path: Path, monkeypatch) -> None:
    db_path = _db(tmp_path)
    previous = _bound(db_path)
    monkeypatch.setenv("STRATA_JUDGE_DAILY_TOKEN_CAP", "0")
    client = MagicMock()
    client.messages.create.return_value = _response(_accept_context_input(), 1, 1)
    try:
        try:
            _judge(ScopeManager(client=client))
        except JudgeDailyCapReached:
            pass
        else:
            raise AssertionError("the first call should have been refused")
    finally:
        unbind_usage_db(previous)
    assert client.messages.create.call_count == 0
    assert _rows(db_path) == []


def test_drafter_and_probe_record_their_own_kinds(tmp_path: Path, monkeypatch) -> None:
    db_path = _db(tmp_path)
    previous = _bound(db_path)
    monkeypatch.delenv("STRATA_JUDGE_DAILY_TOKEN_CAP", raising=False)
    client = MagicMock()
    client.messages.create.return_value = _response(None, 4, 1)
    monkeypatch.setattr("strata.settings.construct_judge_client", lambda **_kwargs: client)
    try:
        assert (
            _default_draft_fn(
                "a session decided to keep the old port",
                api_key="test-key",
                model="claude-haiku-4-5",
            )
            is None
        )
        metered_messages_create(
            client,
            {
                "model": "claude-haiku-4-5",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            },
            provider=None,
        )
    finally:
        unbind_usage_db(previous)
    kinds = [row["call_kind"] for row in _rows(db_path)]
    assert kinds == ["drafter", "doctor_probe"]
    assert all(row["scope_id"] is None for row in _rows(db_path))


def test_price_is_never_invented(tmp_path: Path, monkeypatch) -> None:
    db_path = _db(tmp_path)
    previous = _bound(db_path)
    monkeypatch.delenv("STRATA_JUDGE_DAILY_TOKEN_CAP", raising=False)
    monkeypatch.delenv("STRATA_JUDGE_PRICE_TABLE", raising=False)
    client = MagicMock()
    client.messages.create.return_value = _response(_accept_context_input(), 1_000_000, 1_000_000)
    try:
        _judge(ScopeManager(client=client))
    finally:
        unbind_usage_db(previous)
    report = judge_usage_report(db_path)
    assert report["rows"][0]["cost"] is None
    text = format_judge_usage_report(report)
    assert "No price table is set" in text
    assert "no price set" in text
    assert "1.000000" not in text

    table = tmp_path / "prices.json"
    table.write_text(
        json.dumps({"claude-haiku-4-5": {"input_per_million": 1.0, "output_per_million": 2.0}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("STRATA_JUDGE_PRICE_TABLE", str(table))
    priced = judge_usage_report(db_path)
    assert priced["rows"][0]["cost"] == 3.0
    assert "3.000000" in format_judge_usage_report(priced)


def test_stats_filters_scope_and_day(tmp_path: Path, monkeypatch, capsys) -> None:
    db_path = _db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO judge_usage (
            id, created_at, scope_id, call_kind, contribution_id,
            model, provider, latency_ms, input_tokens, output_tokens
        ) VALUES
            ('jus_a', '2026-10-01T00:00:00+00:00', 'g_a', 'judgment', 'c_1',
             'm', NULL, 1, 10, 2),
            ('jus_b', '2026-10-07T00:00:00+00:00', 'g_b', 'carrier_check', NULL,
             'm', NULL, 1, 4, 1)
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.delenv("STRATA_JUDGE_PRICE_TABLE", raising=False)
    report = judge_usage_report(db_path, since="2026-10-07", scope_id="g_b")
    assert [row["scope_id"] for row in report["rows"]] == ["g_b"]
    assert report["rows"][0]["call_kind"] == "carrier_check"

    from strata.__main__ import cmd_stats_judge

    class _Opened:
        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            return None

    monkeypatch.setattr("strata.stores.open_embedded_stores", lambda: _Opened())
    monkeypatch.setattr(
        "strata.project_config.resolve_storage_paths",
        lambda *_a, **_k: SimpleNamespace(db_path=db_path),
    )
    rc = cmd_stats_judge(SimpleNamespace(since="2026-10-01", scope="g_a", json=False))
    output = capsys.readouterr().out
    assert rc == 0
    assert "g_a" in output
    assert "g_b" not in output
    assert "judgment" in output


def test_doctor_shows_the_cap_and_todays_usage(tmp_path: Path, monkeypatch) -> None:
    strata = tmp_path / ".strata"
    strata.mkdir()
    db_path = strata / "strata.db"
    run_migrations(str(db_path))
    (strata / "config.toml").write_text(
        'db = ".strata/strata.db"\nfleet_yaml = ".strata/fleet.yaml"\n'
        'summaries_dir = ".strata/summaries"\n',
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("STRATA_JUDGE_DAILY_TOKEN_CAP=40\n", encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO judge_usage (
            id, created_at, scope_id, call_kind, model, input_tokens, output_tokens
        ) VALUES ('jus_today', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'),
                  'g_a', 'judgment', 'm', 15, 5)
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.delenv("STRATA_JUDGE_DAILY_TOKEN_CAP", raising=False)

    from strata.__main__ import _check_judge

    check = _check_judge(tmp_path)
    assert "daily token cap 40" in check.message
    assert "20 tokens" in check.message
    assert "15 input, 5 output" in check.message
    assert cap_status_text(str(db_path), cap=40).startswith("daily token cap 40")


def test_http_cap_is_the_unavailable_judge_path(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "t.db"
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(_FLEET, encoding="utf-8")
    run_migrations(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO judge_usage (
            id, created_at, scope_id, call_kind, model, input_tokens, output_tokens
        ) VALUES ('jus_seed', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'),
                  'g_child', 'judgment', 'm', 80, 0)
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("STRATA_JUDGE_DAILY_TOKEN_CAP", "50")
    settings = Settings(
        db_path=str(db_path),
        summaries_dir=str(tmp_path / "summaries"),
        fleet_yaml_path=str(fleet_path),
        anthropic_api_key="test-key",
    )
    application = create_app(settings=settings)
    client_mock = MagicMock()
    client_mock.messages.create.return_value = _response(_accept_context_input(), 1, 1)
    application.dependency_overrides[get_scope_manager] = lambda: ScopeManager(client=client_mock)
    body = {
        "scope_id": "g_child",
        "content": "the port is 8443",
        "proposed_classification": "context",
        "subject": None,
        "supersedes": None,
        "contributor": {
            "scope_id": "g_child",
            "skill": "tester",
            "session_id": "sess_test",
            "ts": "2026-10-07T12:00:00+00:00",
        },
    }
    with TestClient(application) as client:
        refused = client.post("/contribute", json=body)
        assert refused.status_code == 503
        detail = refused.json()["detail"]
        assert detail["error"] == "scope_manager_failure"
        assert detail["error_class"] == "JudgeDailyCapReached"
        assert detail["retry"] == "strata_rejudge"
        assert client_mock.messages.create.call_count == 0
        listed = client.get("/judge-usage")
        assert listed.status_code == 200
        payload = listed.json()
        assert payload["cap"] == 50
        assert payload["over_cap"] is True
        assert payload["today"]["input_tokens"] == 80

    conn = sqlite3.connect(db_path)
    try:
        attempt = conn.execute("SELECT outcome, error_class FROM judgment_attempts").fetchone()
        judgments = conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0]
        usage_count = conn.execute("SELECT COUNT(*) FROM judge_usage").fetchone()[0]
    finally:
        conn.close()
    assert attempt == ("judge_failed", "JudgeDailyCapReached")
    assert judgments == 0
    assert usage_count == 1


def test_migration_0024_applies_on_a_populated_database(tmp_path: Path) -> None:
    without = tmp_path / "migrations"
    without.mkdir()
    for path in _default_migrations_dir().glob("*.sql"):
        if path.name != "0024_judge_usage.sql":
            (without / path.name).write_bytes(path.read_bytes())
    db_path = str(tmp_path / "populated.db")
    run_migrations(db_path, migrations_dir=without)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO contributions (
            id, scope_id, content, proposed_classification,
            contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts
        ) VALUES (
            'c_kept', 'g_a', 'kept', 'context', 'g_a', 'tester', 's', '2026-10-07T00:00:00+00:00'
        )
        """
    )
    conn.commit()
    conn.close()

    assert run_migrations(db_path) == ["0024_judge_usage.sql"]
    conn = sqlite3.connect(db_path)
    try:
        kept = conn.execute("SELECT content FROM contributions WHERE id = 'c_kept'").fetchone()
        assert kept == ("kept",)
        conn.execute(
            """
            INSERT INTO judge_usage (id, created_at, call_kind, input_tokens, output_tokens)
            VALUES ('jus_1', '2026-10-07T00:00:00+00:00', 'judgment', 1, 1)
            """
        )
        conn.commit()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
