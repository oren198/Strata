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


def test_status_renders_per_scope_staleness_metric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``strata status`` renders the per-scope memory-freshness metric (issue #110)."""
    _seed_fleet(tmp_path, monkeypatch)

    # A session read g_ceo's perspective — recorded in the runtime sessions dir
    # (sibling of the summaries dir the embedded stores resolve to).
    from strata.session_state import SessionStateStore, sessions_dir_for

    summaries_dir = tmp_path / "summaries"
    store = SessionStateStore(sessions_dir_for(str(summaries_dir)))
    store.record_read("sess_a", "g_ceo")
    store.record_read("sess_b", "g_ceo")

    rc = main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Memory freshness" in out
    assert "g_ceo" in out
    # g_ceo has no accepted contribution yet, so both sessions' reads count.
    ceo_line = next(line for line in out.splitlines() if line.strip().startswith("g_ceo"))
    assert "2" in ceo_line
    # g_arch was read by nobody → 0.
    arch_line = next(line for line in out.splitlines() if line.strip().startswith("g_arch"))
    assert "0" in arch_line


def test_status_custom_window_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``strata status --window-days`` is reflected in the header."""
    _seed_fleet(tmp_path, monkeypatch)
    rc = main(["status", "--window-days", "7"])
    assert rc == 0
    assert "7-day window" in capsys.readouterr().out


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


# ---------------------------------------------------------------------------
# strata operator — ADR 0008 D1 local entry surface
# ---------------------------------------------------------------------------


def test_operator_root_prints_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``strata operator`` with no subcommand prints the group's help, exit 0."""
    _seed_fleet(tmp_path, monkeypatch)
    rc = main(["operator"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "publish" in out
    assert "supersede" in out
    assert "retire" in out
    assert "show" in out


def test_operator_publish_and_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``strata operator publish`` then ``show`` round-trips through the CLI."""
    _seed_fleet(tmp_path, monkeypatch)

    rc = main(
        [
            "operator",
            "publish",
            "g_ceo",
            "--kind",
            "directive",
            "--content",
            "All services must use TLS 1.3.",
            "--subject",
            "tls",
        ]
    )
    assert rc == 0
    publish_out = capsys.readouterr().out
    assert "Published operator directive" in publish_out
    assert "g_ceo" in publish_out

    rc = main(["operator", "show", "g_ceo"])
    assert rc == 0
    show_out = capsys.readouterr().out
    assert "All services must use TLS 1.3." in show_out
    assert "Health:" in show_out

    # `strata operator show` with no scope_id lists every attachment scope.
    rc = main(["operator", "show"])
    assert rc == 0
    show_all_out = capsys.readouterr().out
    assert "g_ceo" in show_all_out
    assert "Operator acts:" in show_all_out


def test_operator_publish_unknown_scope_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_fleet(tmp_path, monkeypatch)
    rc = main(
        [
            "operator",
            "publish",
            "g_does_not_exist",
            "--kind",
            "directive",
            "--content",
            "text",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "Scope not found" in err


def test_operator_supersede_op_prefixed_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An 'op_' id routes to the operator-stratum item supersede."""
    _seed_fleet(tmp_path, monkeypatch)
    main(
        [
            "operator",
            "publish",
            "g_ceo",
            "--kind",
            "directive",
            "--content",
            "v1",
        ]
    )
    publish_out = capsys.readouterr().out
    op_id = publish_out.split("[")[1].split("]")[0]

    rc = main(["operator", "supersede", "g_ceo", op_id, "--content", "v2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Superseded operator item" in out
    assert op_id in out


def test_operator_supersede_c_prefixed_directive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 'c_' id routes to the in-person correction of a scope's native directive."""
    _seed_fleet(tmp_path, monkeypatch)
    db_path = tmp_path / "test.db"
    summaries_dir = tmp_path / "summaries"

    contribution_id = "c_seed0001"
    with RecordStore(str(db_path)) as rs:
        c = rs.append_contribution(
            scope_id="g_arch",
            content="Use snake_case.",
            proposed_classification="directive",
            subject="naming",
            supersedes=None,
            contributor=ContributorRef(
                scope_id="g_arch",
                skill="architect",
                session_id="s1",
                ts="2026-07-01T00:00:00+00:00",
            ),
        )
        contribution_id = c.id
        rs.record_judgment(
            contribution_id=c.id, decision="accept_as_directive", judged_by="scope-manager"
        )
    SummaryStore(str(summaries_dir)).write(
        "g_arch",
        ScopeSummary(
            scope_id="g_arch",
            directives=[
                Directive(
                    id=contribution_id,
                    content="Use snake_case.",
                    subject="naming",
                    source_scope_id="g_arch",
                    source_skill="architect",
                    created_at="2026-07-01T00:00:00+00:00",
                )
            ],
            context="",
            updated_at="2026-07-01T00:00:00+00:00",
        ),
    )

    rc = main(
        [
            "operator",
            "supersede",
            "g_arch",
            contribution_id,
            "--content",
            "Use snake_case, PascalCase for classes.",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Superseded directive" in out
    assert "operator correction" in out


def test_operator_retire_unrecognized_id_prefix_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_fleet(tmp_path, monkeypatch)
    rc = main(["operator", "retire", "g_ceo", "bogus_id"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Unrecognized id" in err


def test_operator_retire_op_prefixed_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_fleet(tmp_path, monkeypatch)
    main(["operator", "publish", "g_ceo", "--kind", "context", "--content", "temp"])
    publish_out = capsys.readouterr().out
    op_id = publish_out.split("[")[1].split("]")[0]

    rc = main(["operator", "retire", "g_ceo", op_id, "--reason", "cleanup"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Retired operator item" in out


# ---------------------------------------------------------------------------
# strata publication — ADR 0007's local entry surface
# ---------------------------------------------------------------------------


def test_publication_root_prints_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``strata publication`` with no subcommand prints the group's help, exit 0."""
    _seed_fleet(tmp_path, monkeypatch)
    rc = main(["publication"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "show" in out
    assert "bootstrap" in out


def test_publication_show_no_scope_no_publications_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_fleet(tmp_path, monkeypatch)
    rc = main(["publication", "show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No scope has published anything yet." in out


def test_publication_show_unknown_scope_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_fleet(tmp_path, monkeypatch)
    rc = main(["publication", "show", "g_nonexistent"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Scope not found" in err


def test_publication_show_scope_with_no_publication_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_fleet(tmp_path, monkeypatch)
    rc = main(["publication", "show", "g_ceo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "published nothing yet" in out


def test_publication_show_prints_artifact_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``strata publication show <scope>`` prints the artifact's raw text, byte-for-byte."""
    _seed_fleet(tmp_path, monkeypatch)

    from strata.publication import PublishedItem, _render_publication, _write_publication
    from strata.settings import get_settings

    summaries_dir = get_settings().summaries_dir
    item = PublishedItem(
        id="pub_abc123",
        kind="directive",
        content="Use protobuf for all RPC.",
        subject="rpc-protocol",
        anchors=["directive:c_x1"],
        published_at="2026-07-12T00:00:00+00:00",
    )
    _write_publication("g_ceo", [item], summaries_dir=summaries_dir)

    rc = main(["publication", "show", "g_ceo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out == _render_publication("g_ceo", [item])

    # Without a scope_id: every scope that publishes, each under its own header.
    rc = main(["publication", "show"])
    assert rc == 0
    out_all = capsys.readouterr().out
    assert "=== g_ceo ===" in out_all
    assert "Use protobuf for all RPC." in out_all


def test_publication_bootstrap_accept_prints_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``strata publication bootstrap`` runs bootstrap_publication and prints the outcome."""
    _seed_fleet(tmp_path, monkeypatch)

    from strata.scope_manager import BootstrapJudgment, BootstrapPublishedItemInput

    fake_judgment = BootstrapJudgment(
        decision="accept",
        reasoning="One directive is fit for export.",
        items=[
            BootstrapPublishedItemInput(
                content="Use protobuf for all RPC.",
                kind="directive",
                subject="rpc",
                anchors=["subject:rpc"],
            )
        ],
    )

    with (
        patch(
            "strata.scope_manager.ScopeManager.judge_bootstrap_publication",
            return_value=fake_judgment,
        ),
        patch("anthropic.Anthropic"),
    ):
        rc = main(["publication", "bootstrap", "g_ceo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Bootstrapped 1 published item(s)" in out
    assert "g_ceo" in out

    from strata.publication import read_publication
    from strata.settings import get_settings

    items = read_publication("g_ceo", summaries_dir=get_settings().summaries_dir)
    assert len(items) == 1
    assert items[0].content == "Use protobuf for all RPC."


def test_publication_bootstrap_decline_prints_reasoning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_fleet(tmp_path, monkeypatch)

    from strata.scope_manager import BootstrapJudgment

    fake_judgment = BootstrapJudgment(
        decision="decline", reasoning="Nothing fit to publish yet.", items=[]
    )

    with (
        patch(
            "strata.scope_manager.ScopeManager.judge_bootstrap_publication",
            return_value=fake_judgment,
        ),
        patch("anthropic.Anthropic"),
    ):
        rc = main(["publication", "bootstrap", "g_ceo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "declined" in out
    assert "Nothing fit to publish yet." in out


def test_publication_bootstrap_unknown_scope_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_fleet(tmp_path, monkeypatch)
    rc = main(["publication", "bootstrap", "g_nonexistent"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Scope not found" in err
