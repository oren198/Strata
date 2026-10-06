"""`strata register` seeds Claude Code deny rules for `.strata/` (#173, ADR 0013 D6).

Claude Code honors ``permissions.deny`` in ``.claude/settings.json``. Codex's
sandbox config cannot deny a path inside a writable workspace, so register
does not write one. The Strata server is a process: the deny rules bind the
harness's tools, not the server.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from strata import install
from strata.__main__ import cmd_doctor, cmd_register
from strata.mcp import server as mcp_server
from strata.project_config import resolve_storage_paths
from strata.summary_store import ScopeSummary


@pytest.fixture(autouse=True)
def _clear_judge_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "JUDGE_API_KEY",
        "ANTHROPIC_API_KEY",
        "STRATA_JUDGE_API_KEY",
        "STRATA_ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _init_project(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()


def _register(tmp_path: Path, *, harness: str = "claude-code") -> int:
    return cmd_register(
        argparse.Namespace(
            path=str(tmp_path),
            diff=False,
            bootstrap_venv=False,
            harness=[harness],
        )
    )


def _settings(tmp_path: Path) -> dict:
    return json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _write_settings(tmp_path: Path, data: dict) -> None:
    claude = tmp_path / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "settings.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Fresh config
# ---------------------------------------------------------------------------


def test_fresh_register_seeds_claude_deny_rules(tmp_path: Path) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path) == 0

    data = _settings(tmp_path)
    assert data["permissions"]["deny"] == list(install.CLAUDE_STRATA_DENY_RULES)
    assert install.strata_deny_rules_present(data)
    # Write(path) is accepted by Claude Code and never consulted.
    assert not any(rule.startswith("Write(") for rule in data["permissions"]["deny"])


def test_merge_on_empty_settings_is_idempotent_in_memory() -> None:
    settings: dict = {}
    assert install.merge_strata_deny_rules(settings) is True
    assert install.merge_strata_deny_rules(settings) is False
    assert settings["permissions"]["deny"] == list(install.CLAUDE_STRATA_DENY_RULES)


# ---------------------------------------------------------------------------
# Existing config with the user's own rules
# ---------------------------------------------------------------------------


def test_register_appends_deny_rules_beside_user_rules(tmp_path: Path) -> None:
    _init_project(tmp_path)
    user = {
        "theme": "dark",
        "permissions": {
            "allow": ["Bash(npm test)"],
            "deny": ["Bash(rm *)"],
        },
    }
    _write_settings(tmp_path, user)

    assert _register(tmp_path) == 0

    data = _settings(tmp_path)
    assert data["theme"] == "dark"
    assert data["permissions"]["allow"] == ["Bash(npm test)"]
    assert data["permissions"]["deny"] == [
        "Bash(rm *)",
        *install.CLAUDE_STRATA_DENY_RULES,
    ]


def test_malformed_permissions_are_left_untouched(tmp_path: Path) -> None:
    _init_project(tmp_path)
    _write_settings(tmp_path, {"permissions": "open", "theme": "dark"})

    assert _register(tmp_path) == 0

    data = _settings(tmp_path)
    assert data["permissions"] == "open"
    assert data["theme"] == "dark"
    assert install.strata_deny_rules_present(data) is False


# ---------------------------------------------------------------------------
# Second register changes nothing
# ---------------------------------------------------------------------------


def test_second_register_does_not_change_settings(tmp_path: Path) -> None:
    _init_project(tmp_path)
    _write_settings(
        tmp_path,
        {"permissions": {"allow": ["Read"], "deny": ["Bash(git push *)"]}},
    )
    assert _register(tmp_path) == 0
    settings_path = tmp_path / ".claude" / "settings.json"
    before = settings_path.read_bytes()

    assert _register(tmp_path) == 0

    assert settings_path.read_bytes() == before
    deny = _settings(tmp_path)["permissions"]["deny"]
    assert deny.count("Read(/.strata/**)") == 1
    assert deny.count("Edit(/.strata/**)") == 1
    assert deny[0] == "Bash(git push *)"


def test_unregister_removes_only_seeded_deny_rules(tmp_path: Path) -> None:
    settings = {
        "permissions": {
            "allow": ["Bash"],
            "deny": ["Bash(rm *)", *install.CLAUDE_STRATA_DENY_RULES],
        }
    }
    assert install.remove_strata_deny_rules(settings) == "removed"
    assert settings["permissions"]["allow"] == ["Bash"]
    assert settings["permissions"]["deny"] == ["Bash(rm *)"]
    assert install.remove_strata_deny_rules(settings) == "absent"


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def _run_doctor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> tuple[int, str]:
    monkeypatch.chdir(tmp_path)
    rc = cmd_doctor(argparse.Namespace())
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def test_doctor_passes_when_deny_rules_are_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path) == 0
    from strata.migrator import run_migrations

    run_migrations(str(tmp_path / ".strata" / "strata.db"))
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_root")
    monkeypatch.setenv("STRATA_AGENT_SKILL", "strata-worker")
    monkeypatch.setenv("STRATA_AGENT_SESSION_ID", "sess_test")
    capsys.readouterr()

    rc, output = _run_doctor(tmp_path, monkeypatch, capsys)

    assert rc == 0
    assert "Store deny rules" in output
    assert "Read(/.strata/**)" in output
    assert "Edit(/.strata/**)" in output
    assert "Codex sandbox config cannot deny" in output


def test_doctor_fails_when_deny_rules_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path) == 0
    from strata.migrator import run_migrations

    run_migrations(str(tmp_path / ".strata" / "strata.db"))
    data = _settings(tmp_path)
    del data["permissions"]
    _write_settings(tmp_path, data)
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_root")
    monkeypatch.setenv("STRATA_AGENT_SKILL", "strata-worker")
    monkeypatch.setenv("STRATA_AGENT_SESSION_ID", "sess_test")
    capsys.readouterr()

    rc, output = _run_doctor(tmp_path, monkeypatch, capsys)

    assert rc == 1
    assert "Store deny rules" in output
    assert "missing from .claude/settings.json permissions.deny" in output
    assert "strata register" in output
    assert "Codex sandbox config cannot deny" in output


# ---------------------------------------------------------------------------
# Codex: do not fake a deny the sandbox cannot express
# ---------------------------------------------------------------------------


def test_codex_sandbox_cannot_deny_a_workspace_path() -> None:
    assert install.codex_sandbox_can_deny_workspace_path() is False


def test_codex_register_does_not_write_a_workspace_deny(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_project(tmp_path)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    original = (
        'sandbox_mode = "workspace-write"\n\n[sandbox_workspace_write]\nnetwork_access = false\n'
    )
    config.write_text(original, encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert _register(tmp_path, harness="codex") == 0

    text = config.read_text(encoding="utf-8")
    # The user's sandbox posture is preserved byte-for-byte. The only addition
    # is the managed MCP and hook blocks — no permissions profile, no path deny.
    assert text.startswith(original)
    added = text[len(original) :]
    assert "sandbox_mode" not in added
    assert "default_permissions" not in text
    assert "[permissions" not in text
    assert ".strata/" not in text
    assert "/.strata" not in text
    assert install.CODEX_MCP_MARKER in added
    assert install.CODEX_HOOK_MARKER in added


# ---------------------------------------------------------------------------
# The server process still reads .strata/ — deny rules bind harness tools.
# ---------------------------------------------------------------------------


def test_server_process_still_reads_dot_strata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert _register(tmp_path) == 0
    assert install.strata_deny_rules_present(_settings(tmp_path))

    saved = (
        mcp_server._db_path,
        mcp_server._summaries_dir,
        mcp_server._fleet_yaml_path,
        mcp_server._sessions_dir,
        mcp_server._project_root,
        mcp_server._record_store,
        mcp_server._summary_store,
        mcp_server._session_store,
    )
    try:
        paths = resolve_storage_paths(start=tmp_path)
        store_root = (tmp_path / ".strata").resolve()
        assert Path(paths.db_path).resolve().is_relative_to(store_root)
        assert Path(paths.summaries_dir).resolve().is_relative_to(store_root)

        mcp_server._set_paths(paths)
        mcp_server._init_stores()
        store = mcp_server._summary_store
        assert store is not None
        store.write(
            "g_root",
            ScopeSummary(
                scope_id="g_root",
                directives=[],
                context="server-can-read",
                updated_at="2026-10-06T00:00:00+00:00",
            ),
        )
        loaded = store.read("g_root")
        assert loaded is not None
        assert loaded.context == "server-can-read"
        assert store.path_for("g_root").resolve().is_relative_to(store_root)

        conn = sqlite3.connect(f"file:{mcp_server._db_path}?mode=ro", uri=True)
        try:
            applied = conn.execute("SELECT name FROM _migrations").fetchall()
        finally:
            conn.close()
        assert applied, "the server process migrated the database under .strata/"
    finally:
        record = mcp_server._record_store
        if record is not None and record is not saved[5]:
            record.close()
        (
            mcp_server._db_path,
            mcp_server._summaries_dir,
            mcp_server._fleet_yaml_path,
            mcp_server._sessions_dir,
            mcp_server._project_root,
            mcp_server._record_store,
            mcp_server._summary_store,
            mcp_server._session_store,
        ) = saved
