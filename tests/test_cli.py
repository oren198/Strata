"""Tests for the ``strata`` CLI dispatcher."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from strata.__main__ import main


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    """Bare ``strata`` invocation prints help and exits 0."""
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Strata — shared memory for agent fleets." in captured.out
    assert "<command>" in captured.out


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """``strata --version`` prints the version and exits 0."""
    with pytest.raises(SystemExit) as ei:
        main(["--version"])
    assert ei.value.code == 0
    captured = capsys.readouterr()
    assert "strata " in captured.out


def test_migrate_calls_runner(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    """``strata migrate --db <path>`` calls the migrations runner."""
    db_path = str(tmp_path / "test.db")
    with patch("strata.migrator.run_migrations") as run:
        run.return_value = ["0001_initial.sql"]
        rc = main(["migrate", "--db", db_path])
    assert rc == 0
    run.assert_called_once_with(db_path)
    out = capsys.readouterr().out
    assert "0001_initial.sql" in out


def test_scopes_hits_backend(capsys: pytest.CaptureFixture[str]) -> None:
    """``strata scopes`` calls GET /scopes and prints the fleet."""
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "strata": [{"id": "L0", "name": "Executive", "ordinal": 0}],
        "scopes": [{"id": "g_ceo", "name": "CEO", "stratum_id": "L0"}],
        "edges": [{"from_scope_id": "g_eng", "to_scope_id": "g_ceo"}],
    }
    with patch("httpx.get", return_value=fake_resp) as g:
        rc = main(["scopes"])
    assert rc == 0
    g.assert_called_once()
    out = capsys.readouterr().out
    assert "L0" in out and "g_ceo" in out and "g_eng" in out


def test_summary_missing_scope_returns_1(capsys: pytest.CaptureFixture[str]) -> None:
    """A 404 from /scopes/{id}/summary maps to exit code 1."""
    fake_resp = MagicMock()
    fake_resp.status_code = 404
    with patch("httpx.get", return_value=fake_resp):
        rc = main(["summary", "g_nope"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Scope not found" in err


def test_record_renders_contributions_and_judgments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``strata record`` prints contribution + judgment lines per item."""
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "contributions": [
            {
                "id": "c_abc",
                "content": "use gRPC",
                "proposed_classification": "directive",
                "subject": "rpc-protocol",
                "supersedes": None,
                "contributor": {
                    "scope_id": "g_arch",
                    "skill": "architect",
                    "session_id": "sess_x",
                    "ts": "2026-05-23T20:00:00Z",
                },
            }
        ],
        "judgments": [{"contribution_id": "c_abc", "decision": "accept_as_directive"}],
    }
    with patch("httpx.get", return_value=fake_resp):
        rc = main(["record", "g_arch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "c_abc" in out
    assert "accept_as_directive" in out
    assert "use gRPC" in out


def test_backend_error_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    """Network errors from inspection commands surface as exit code 2."""
    import httpx

    with patch("httpx.get", side_effect=httpx.ConnectError("nope")):
        rc = main(["scopes"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Backend error" in err


def test_bootstrap_no_config_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``strata bootstrap`` with no discoverable config exits 1 with a clear message."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STRATA_FLEET_CONFIG", raising=False)
    # Capture, but use stderr buffer because main writes there.
    with patch("sys.stderr", new_callable=io.StringIO) as err_buf:
        rc = main(["bootstrap"])
    assert rc == 1
    assert "No fleet config found" in err_buf.getvalue()
