"""Tests for the SessionStart hook — the READ-side trigger for shared memory.

The WRITE side already learned this lesson: a Stop hook exists precisely
because "contribute before ending" as prose was not enough
(tests/test_freshness.py). The READ side had no equivalent trigger — nothing
fired at the moment a session begins, so an agent's static instructions to
read its perspective competed with everything else in context and lost.

`strata session-start-hook` (:mod:`strata.session_start`) closes that gap. It
is a TRIGGER, never a delivery channel: it prints a short imperative telling
the agent to call strata_read_perspective before its first substantive
answer, and — cheaply, from fleet.yaml alone, no database access, no LLM
call, no judgment — the fleet's scope ids, so a "which scope should this
session act as" ask is concrete. It must NEVER print memory content itself;
doing so would bypass scope binding, entitlement, and judgment.

Vocabulary follows CONTEXT.md: scope, fleet, perspective, bound.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata import session_start  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path, scopes: list[dict]) -> None:
    strata_dir = tmp_path / ".strata"
    strata_dir.mkdir()
    (strata_dir / "config.toml").write_text(
        'db = ".strata/strata.db"\n'
        'fleet_yaml = ".strata/fleet.yaml"\n'
        'summaries_dir = ".strata/summaries"\n',
        encoding="utf-8",
    )
    fleet = {
        "strata": [{"id": "L0", "name": "root", "ordinal": 0}],
        "scopes": scopes,
        "edges": [],
    }
    (strata_dir / "fleet.yaml").write_text(yaml.dump(fleet), encoding="utf-8")


# ---------------------------------------------------------------------------
# render_session_start_message — the printed text, never memory content
# ---------------------------------------------------------------------------


def test_message_with_no_scope_ids_still_carries_the_full_instruction() -> None:
    text = session_start.render_session_start_message(None)
    assert "strata_read_perspective" in text
    assert "not bound" in text
    assert "strata_bind" in text
    assert "which scope this session should act as" in text


def test_message_names_available_scopes_when_given() -> None:
    text = session_start.render_session_start_message(["g_backend", "g_frontend"])
    assert "g_backend" in text
    assert "g_frontend" in text


def test_message_with_empty_scope_list_omits_the_parenthetical() -> None:
    with_none = session_start.render_session_start_message(None)
    with_empty = session_start.render_session_start_message([])
    assert with_none == with_empty


def test_message_carries_no_internal_issue_or_adr_references() -> None:
    text = session_start.render_session_start_message(["g_root"])
    assert "issue #" not in text.lower()
    assert "adr" not in text.lower()


# ---------------------------------------------------------------------------
# _available_scope_ids — cheap fleet.yaml read, degrades silently
# ---------------------------------------------------------------------------


def test_available_scope_ids_reads_fleet_yaml(tmp_path: Path, monkeypatch) -> None:
    _make_project(
        tmp_path,
        [
            {"id": "g_backend", "name": "Backend", "stratum_id": "L0"},
            {"id": "g_frontend", "name": "Frontend", "stratum_id": "L0"},
        ],
    )
    monkeypatch.chdir(tmp_path)

    ids = session_start._available_scope_ids()  # noqa: SLF001

    assert ids == ["g_backend", "g_frontend"]


def test_available_scope_ids_excludes_archived_scopes(tmp_path: Path, monkeypatch) -> None:
    _make_project(
        tmp_path,
        [
            {"id": "g_backend", "name": "Backend", "stratum_id": "L0"},
            {
                "id": "g_old",
                "name": "Old",
                "stratum_id": "L0",
                "status": "archived",
            },
        ],
    )
    monkeypatch.chdir(tmp_path)

    ids = session_start._available_scope_ids()  # noqa: SLF001

    assert ids == ["g_backend"]


def test_available_scope_ids_none_when_unregistered(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # no .strata/config.toml anywhere up the tree

    assert session_start._available_scope_ids() is None  # noqa: SLF001


def test_available_scope_ids_none_on_invalid_fleet_yaml(tmp_path: Path, monkeypatch) -> None:
    strata_dir = tmp_path / ".strata"
    strata_dir.mkdir()
    (strata_dir / "config.toml").write_text(
        'db = ".strata/strata.db"\n'
        'fleet_yaml = ".strata/fleet.yaml"\n'
        'summaries_dir = ".strata/summaries"\n',
        encoding="utf-8",
    )
    (strata_dir / "fleet.yaml").write_text("this is not valid: [fleet yaml", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert session_start._available_scope_ids() is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# run_session_start_hook — always exits 0, writes the trigger to stdout
# ---------------------------------------------------------------------------


def test_run_session_start_hook_writes_instruction_and_returns_zero(
    tmp_path: Path, monkeypatch
) -> None:
    _make_project(tmp_path, [{"id": "g_root", "name": "Root", "stratum_id": "L0"}])
    monkeypatch.chdir(tmp_path)
    buf = io.StringIO()

    rc = session_start.run_session_start_hook(out=buf)

    assert rc == 0
    out = buf.getvalue()
    assert "strata_read_perspective" in out
    assert "g_root" in out


def test_run_session_start_hook_never_breaks_on_unregistered_project(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    buf = io.StringIO()

    rc = session_start.run_session_start_hook(out=buf)

    assert rc == 0
    assert "strata_read_perspective" in buf.getvalue()


# ---------------------------------------------------------------------------
# strata session-start-hook — the CLI entry point the vendored wrapper script
# execs, symmetric with `strata freshness-hook`.
# ---------------------------------------------------------------------------


def test_cmd_session_start_hook_prints_instruction_and_returns_zero(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import argparse
    import io as _io

    from strata.__main__ import cmd_session_start_hook

    _make_project(tmp_path, [{"id": "g_root", "name": "Root", "stratum_id": "L0"}])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", _io.StringIO("{}"))

    rc = cmd_session_start_hook(argparse.Namespace())

    assert rc == 0
    out = capsys.readouterr().out
    assert "strata_read_perspective" in out
    assert "g_root" in out


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
