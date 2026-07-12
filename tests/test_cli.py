"""Tests for the ``strata`` CLI dispatcher."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from strata.__main__ import main
from strata.record_store import ContributorRef, RecordStore
from strata.summary_store import Directive, ScopeSummary, SummaryStore


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Clear the ``Settings`` lru_cache before and after every test.

    Several tests below point ``STRATA_FLEET_CONFIG`` / ``STRATA_DB_PATH`` /
    ``STRATA_SUMMARIES_DIR`` at a per-test ``tmp_path``; without clearing the
    cache a stale ``Settings`` singleton from a previous test can leak in
    (same pattern as ``tests/test_launch.py``'s ``fleet_env`` fixture).
    """
    from strata.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_FLEET_YAML = """\
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
  - id: g_arch
    name: Architect
    stratum_id: L1
edges:
  - from: g_arch
    to: g_ceo
"""


def _seed_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed a temp fleet.yaml + DB + summaries dir and point the CLI at them.

    Mirrors how the embedded stores (``strata.stores.open_embedded_stores``)
    resolve paths: env-var settings, since no ``.strata/config.toml`` is
    present in *tmp_path*.
    """
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(_FLEET_YAML, encoding="utf-8")
    db_path = tmp_path / "test.db"
    summaries_dir = tmp_path / "summaries"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRATA_FLEET_CONFIG", str(fleet_path))
    monkeypatch.setenv("STRATA_DB_PATH", str(db_path))
    monkeypatch.setenv("STRATA_SUMMARIES_DIR", str(summaries_dir))

    from strata.settings import get_settings

    get_settings.cache_clear()

    # Migrate up front so tests that seed the record store directly (before
    # ever invoking the CLI, which would migrate on its own) have the schema
    # in place.
    from strata.migrator import run_migrations

    run_migrations(str(db_path))


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


def test_scopes_reads_embedded_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``strata scopes`` reads fleet.yaml directly — no backend involved."""
    _seed_fleet(tmp_path, monkeypatch)

    rc = main(["scopes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "L0" in out and "g_ceo" in out and "g_arch" in out
    assert "g_arch" in out and "→ g_ceo" in out  # edge line: g_arch -> g_ceo


def test_scopes_no_fleet_config_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No fleet.yaml on disk → actionable error, exit 1 (no backend to blame)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRATA_FLEET_CONFIG", str(tmp_path / "nope.yaml"))
    monkeypatch.setenv("STRATA_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("STRATA_SUMMARIES_DIR", str(tmp_path / "summaries"))
    from strata.settings import get_settings

    get_settings.cache_clear()

    rc = main(["scopes"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No fleet config found" in err


def test_summary_missing_scope_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown scope ID maps to exit code 1."""
    _seed_fleet(tmp_path, monkeypatch)

    rc = main(["summary", "g_nope"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Scope not found" in err


def test_summary_prints_directives_and_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``strata summary`` renders a written ScopeSummary's directives + context."""
    _seed_fleet(tmp_path, monkeypatch)
    summaries_dir = tmp_path / "summaries"
    store = SummaryStore(str(summaries_dir))
    store.write(
        "g_arch",
        ScopeSummary(
            scope_id="g_arch",
            directives=[
                Directive(
                    id="c_abc",
                    content="use gRPC",
                    subject="rpc-protocol",
                    source_scope_id="g_arch",
                    source_skill="architect",
                    created_at="2026-05-23T20:00:00Z",
                )
            ],
            context="team is ramping up on Go",
            updated_at=datetime.now(tz=UTC).isoformat(),
        ),
    )

    rc = main(["summary", "g_arch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "c_abc" in out
    assert "use gRPC" in out
    assert "team is ramping up on Go" in out


def test_record_renders_contributions_and_judgments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``strata record`` prints contribution + judgment lines per item."""
    _seed_fleet(tmp_path, monkeypatch)
    db_path = tmp_path / "test.db"
    with RecordStore(str(db_path)) as rs:
        c = rs.append_contribution(
            scope_id="g_arch",
            content="use gRPC",
            proposed_classification="directive",
            subject="rpc-protocol",
            supersedes=None,
            contributor=ContributorRef(
                scope_id="g_arch",
                skill="architect",
                session_id="sess_x",
                ts="2026-05-23T20:00:00Z",
            ),
        )
        rs.record_judgment(
            contribution_id=c.id, decision="accept_as_directive", judged_by="scope-manager"
        )

    rc = main(["record", "g_arch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert c.id in out
    assert "accept_as_directive" in out
    assert "use gRPC" in out


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


def test_record_marks_pending_with_failed_attempt_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pending contribution with judgment-attempt-failed events renders as
    "(pending — N failed attempts)"; one with none stays a bare "(pending)"
    and a judged one shows its verdict (issue #57)."""
    _seed_fleet(tmp_path, monkeypatch)
    db_path = tmp_path / "test.db"

    def _contribute(rs: RecordStore, content: str, subject: str | None = None):
        return rs.append_contribution(
            scope_id="g_arch",
            content=content,
            proposed_classification="directive",
            subject=subject,
            supersedes=None,
            contributor=ContributorRef(
                scope_id="g_arch", skill="architect", session_id="s", ts="t"
            ),
        )

    with RecordStore(str(db_path)) as rs:
        c_failed = _contribute(rs, "judge kept crashing")
        _contribute(rs, "not judged yet")
        c_ok = _contribute(rs, "accepted")

        rs.record_judgment(
            contribution_id=c_ok.id, decision="accept_as_directive", judged_by="scope-manager"
        )
        rs.record_judgment_attempt(
            contribution_id=c_failed.id, error_class="ValueError", message="boom"
        )
        rs.record_judgment_attempt(
            contribution_id=c_failed.id, error_class="TimeoutError", message="slow"
        )

    rc = main(["record", "g_arch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(pending — 2 failed attempts)" in out
    assert "(pending)" in out  # the never-judged contribution has no attempts
    assert "accept_as_directive" in out  # c_ok is judged


def test_scopes_invalid_fleet_config_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fleet.yaml that fails FleetConfig validation surfaces its error, exit 1."""
    fleet_path = tmp_path / "fleet.yaml"
    # Well-formed YAML, but scope g_arch references a stratum that doesn't
    # exist -> FleetConfigError(kind="unknown_stratum_ref") from load-time
    # invariant validation (ADR 0002), not a raw pydantic schema error.
    fleet_path.write_text(
        "strata:\n  - id: L0\n    name: Root\n    ordinal: 0\n"
        "scopes:\n  - id: g_arch\n    name: Architect\n    stratum_id: L9\n"
        "edges: []\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRATA_FLEET_CONFIG", str(fleet_path))
    monkeypatch.setenv("STRATA_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("STRATA_SUMMARIES_DIR", str(tmp_path / "summaries"))
    from strata.settings import get_settings

    get_settings.cache_clear()

    rc = main(["scopes"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Fleet config invalid" in err
