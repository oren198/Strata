"""`strata register` seeds Claude Code deny rules for the resolved store (#173).

Claude Code honors ``permissions.deny`` in ``.claude/settings.json``. The
seeded layout lives under ``.strata/``. A ``config.toml`` that points the
store elsewhere (an external fleet directory, a split layout, a file in the
project root) is denied at the path it resolves to. Codex's sandbox config
cannot deny a path inside a writable workspace, so register does not write
one. The Strata server is a process: the deny rules bind the harness's
tools, not the server.
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
# The store may not live under .strata/ (issue #184)
# ---------------------------------------------------------------------------


def _write_config(project: Path, body: str) -> None:
    strata = project / ".strata"
    strata.mkdir(parents=True, exist_ok=True)
    (strata / "config.toml").write_text(body, encoding="utf-8")


def _spec(path: Path, suffix: str) -> str:
    """A Claude pattern for an absolute *path* (``suffix`` is ``/**`` or ``*``)."""
    return f"/{path.resolve().as_posix()}{suffix}"


def _read_rule(path: Path, *, directory: bool) -> str:
    """The ``Read(...)`` string register emits for an absolute *path*."""
    suffix = "/**" if directory else ""
    return f"Read({_spec(path, suffix)})"


def _rule_pairs(*specs: str) -> list[str]:
    rules: list[str] = []
    for spec in specs:
        rules.append(f"Read({spec})")
        rules.append(f"Edit({spec})")
    return rules


def test_no_config_resolves_to_the_seeded_dot_strata_rules(tmp_path: Path) -> None:
    assert install.claude_store_deny_rules(tmp_path) == install.CLAUDE_STRATA_DENY_RULES


def test_register_denies_an_external_fleet_directory(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    fleet = tmp_path / "strata-fleet"
    project.mkdir()
    _init_project(project)
    fleet.mkdir()
    _write_config(
        project,
        "\n".join(
            [
                f'db = "{(fleet / "strata.db").as_posix()}"',
                f'fleet_yaml = "{(fleet / "fleet.yaml").as_posix()}"',
                f'summaries_dir = "{(fleet / "summaries").as_posix()}"',
                "",
            ]
        ),
    )
    user_deny = "Bash(rm *)"
    _write_settings(project, {"permissions": {"allow": ["Bash(npm test)"], "deny": [user_deny]}})

    assert _register(project) == 0

    # The fleet directory holds fleet.yaml next to the database. Deny the
    # database file and Strata's own subdirectories, not the directory itself
    # and not fleet.yaml.
    fleet_root = fleet.resolve()
    owned = _rule_pairs(
        "/.strata/**",
        _spec(fleet_root / ".locks", "/**"),
        _spec(fleet_root / "sessions", "/**"),
        _spec(fleet_root / "strata.db", "*"),
        _spec(fleet_root / "summaries", "/**"),
    )
    data = _settings(project)
    assert data["permissions"]["allow"] == ["Bash(npm test)"]
    assert data["permissions"]["deny"] == [user_deny, *owned]
    assert install.claude_store_deny_rules(project) == tuple(owned)
    assert f"Read({_spec(fleet_root, '/**')})" not in data["permissions"]["deny"]
    assert all("fleet.yaml" not in rule for rule in data["permissions"]["deny"])
    settings_path = project / ".claude" / "settings.json"
    before = settings_path.read_bytes()
    assert _register(project) == 0
    assert settings_path.read_bytes() == before


def test_doctor_checks_the_resolved_external_rules(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # The project has to be tmp_path itself. The suite pins config discovery
    # at a guard directory inside tmp_path and walks upward, so a nested
    # project directory is invisible to `strata doctor`.
    fleet = tmp_path_factory.mktemp("strata-fleet")
    db = fleet / "strata.db"
    _init_project(tmp_path)
    _write_config(
        tmp_path,
        "\n".join(
            [
                f'db = "{db.as_posix()}"',
                f'fleet_yaml = "{(fleet / "fleet.yaml").as_posix()}"',
                f'summaries_dir = "{(fleet / "summaries").as_posix()}"',
                "",
            ]
        ),
    )
    assert _register(tmp_path) == 0
    from strata.migrator import run_migrations

    run_migrations(str(db))
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_root")
    monkeypatch.setenv("STRATA_AGENT_SKILL", "strata-worker")
    monkeypatch.setenv("STRATA_AGENT_SESSION_ID", "sess_test")
    capsys.readouterr()

    external = f"Read({_spec(db, '*')})"
    rc, output = _run_doctor(tmp_path, monkeypatch, capsys)
    assert rc == 0
    assert external in output
    assert f"Read({_spec(fleet, '/**')})" not in output
    assert "Read(/.strata/**)" in output

    data = _settings(tmp_path)
    data["permissions"]["deny"] = list(install.CLAUDE_STRATA_DENY_RULES)
    _write_settings(tmp_path, data)
    capsys.readouterr()

    rc, output = _run_doctor(tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "missing from .claude/settings.json permissions.deny" in output
    assert external in output


def test_unregister_removes_only_the_resolved_rules(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    fleet = tmp_path / "strata-fleet"
    project.mkdir()
    _init_project(project)
    fleet.mkdir()
    _write_config(
        project,
        "\n".join(
            [
                f'db = "{(fleet / "strata.db").as_posix()}"',
                f'fleet_yaml = "{(fleet / "fleet.yaml").as_posix()}"',
                f'summaries_dir = "{(fleet / "summaries").as_posix()}"',
                "",
            ]
        ),
    )
    _write_settings(project, {"theme": "dark", "permissions": {"deny": ["Bash(rm *)"]}})
    assert _register(project) == 0

    from strata.__main__ import cmd_unregister

    assert (
        cmd_unregister(argparse.Namespace(path=str(project), dry_run=False, purge_data=False)) == 0
    )

    data = _settings(project)
    assert data["theme"] == "dark"
    assert data["permissions"]["deny"] == ["Bash(rm *)"]


def test_split_store_directories_are_each_denied(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    db_dir = tmp_path / "db"
    fleet_dir = tmp_path / "fleet"
    mem = tmp_path / "mem"
    _write_config(
        project,
        "\n".join(
            [
                f'db = "{(db_dir / "strata.db").as_posix()}"',
                f'fleet_yaml = "{(fleet_dir / "fleet.yaml").as_posix()}"',
                f'summaries_dir = "{(mem / "summaries").as_posix()}"',
                "",
            ]
        ),
    )

    rules = install.claude_store_deny_rules(project)

    assert f"Read({_spec(db_dir / 'strata.db', '*')})" in rules
    assert _read_rule(db_dir / ".locks", directory=True) in rules
    assert _read_rule(mem / "summaries", directory=True) in rules
    assert _read_rule(mem / "sessions", directory=True) in rules
    assert "Read(/.strata/**)" in rules
    assert "Edit(/.strata/**)" in rules
    # The database's directory and the fleet file's directory stay open.
    assert _read_rule(db_dir, directory=True) not in rules
    assert _read_rule(fleet_dir, directory=True) not in rules
    assert all("fleet.yaml" not in rule for rule in rules)


def test_legacy_root_layout_denies_files_not_the_project(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        'db = "strata.db"\nfleet_yaml = "fleet.yaml"\nsummaries_dir = "summaries"\n',
    )

    rules = install.claude_store_deny_rules(tmp_path)

    assert "Read(/**)" not in rules
    assert "Edit(/**)" not in rules
    assert "Read(/)" not in rules
    assert "Edit(/)" not in rules
    assert all(not rule.startswith(("Read(//", "Edit(//")) for rule in rules)
    assert rules == (
        "Read(/.locks/**)",
        "Edit(/.locks/**)",
        "Read(/.strata/**)",
        "Edit(/.strata/**)",
        "Read(/sessions/**)",
        "Edit(/sessions/**)",
        "Read(/strata.db*)",
        "Edit(/strata.db*)",
        "Read(/summaries/**)",
        "Edit(/summaries/**)",
    )
    assert all("fleet.yaml" not in rule for rule in rules)


def test_file_in_an_ancestor_does_not_deny_that_ancestor(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = home / "proj"
    project.mkdir(parents=True)
    db = home / "strata.db"
    _write_config(
        project,
        "\n".join(
            [
                f'db = "{db.as_posix()}"',
                'fleet_yaml = ".strata/fleet.yaml"',
                'summaries_dir = ".strata/summaries"',
                "",
            ]
        ),
    )

    rules = install.claude_store_deny_rules(project)

    assert _read_rule(home, directory=True) not in rules
    assert f"Read(/{db.resolve().as_posix()}*)" in rules
    assert f"Edit(/{db.resolve().as_posix()}*)" in rules
    assert "Read(/.strata/**)" in rules
    assert "Edit(/.strata/**)" in rules


def test_malformed_config_falls_back_to_the_seeded_rules(tmp_path: Path) -> None:
    _write_config(tmp_path, "this is not toml\n")
    assert install.claude_store_deny_rules(tmp_path) == install.CLAUDE_STRATA_DENY_RULES


def test_explicit_paths_name_a_store_that_is_not_on_disk_yet(tmp_path: Path) -> None:
    fleet = tmp_path.parent / f"{tmp_path.name}-fleet"
    rules = install.claude_store_deny_rules(
        tmp_path,
        db=fleet / "strata.db",
        fleet_yaml=fleet / "fleet.yaml",
        summaries_dir=fleet / "summaries",
    )
    fleet_root = fleet.resolve()
    assert rules == tuple(
        _rule_pairs(
            "/.strata/**",
            _spec(fleet_root / ".locks", "/**"),
            _spec(fleet_root / "sessions", "/**"),
            _spec(fleet_root / "strata.db", "*"),
            _spec(fleet_root / "summaries", "/**"),
        )
    )
    assert f"Read({_spec(fleet_root, '/**')})" not in rules


def test_diff_preview_names_an_adopted_root_store(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _init_project(tmp_path)
    (tmp_path / "fleet.yaml").write_text(
        "strata:\n  - id: L0\n    name: root\n    ordinal: 0\n"
        "scopes:\n  - id: g_root\n    name: Root\n    stratum_id: L0\n"
        "edges: []\n",
        encoding="utf-8",
    )
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "g_root.md").write_text("kept\n", encoding="utf-8")

    rc = cmd_register(
        argparse.Namespace(
            path=str(tmp_path),
            diff=True,
            bootstrap_venv=False,
            harness=["claude-code"],
        )
    )
    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert rc == 0
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / ".strata" / "config.toml").exists()
    assert "Read(/strata.db*)" in output
    assert "Read(/summaries/**)" in output
    assert "Read(/.strata/**)" in output
    assert "Read(/fleet.yaml)" not in output
    assert "Read(/**)" not in output


def _patch_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setattr(install, "_user_home", lambda: home.resolve())


def test_coverage_names_python_node_and_sqlite3() -> None:
    text = install.CLAUDE_STRATA_DENY_COVERAGE
    assert "Python or Node" in text
    assert "sqlite3" in text
    assert "grep" not in text


def test_shared_directory_does_not_deny_other_repos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    dev = home / "dev"
    project = dev / "repo-a"
    other = dev / "repo-b"
    project.mkdir(parents=True)
    other.mkdir()
    (other / "README").write_text("other repo\n", encoding="utf-8")
    (dev / "Makefile").write_text("all:\n", encoding="utf-8")
    _patch_home(monkeypatch, home)
    db = dev / "strata.db"
    _write_config(
        project,
        "\n".join(
            [
                f'db = "{db.as_posix()}"',
                'fleet_yaml = ".strata/fleet.yaml"',
                'summaries_dir = ".strata/summaries"',
                "",
            ]
        ),
    )

    rules = install.claude_store_deny_rules(project)

    assert f"Read({_spec(db, '*')})" in rules
    assert f"Read({_spec(dev, '/**')})" not in rules
    assert f"Read({_spec(other, '/**')})" not in rules
    assert "Read(/.strata/**)" in rules
    assert all("Makefile" not in rule and "repo-b" not in rule for rule in rules)
    assert all("fleet.yaml" not in rule for rule in rules)


def test_fleet_yaml_in_home_is_not_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    project = home / "proj"
    project.mkdir(parents=True)
    (home / "notes.txt").write_text("personal\n", encoding="utf-8")
    _patch_home(monkeypatch, home)
    db = home / "strata.db"
    _write_config(
        project,
        "\n".join(
            [
                f'db = "{db.as_posix()}"',
                f'fleet_yaml = "{(home / "fleet.yaml").as_posix()}"',
                'summaries_dir = ".strata/summaries"',
                "",
            ]
        ),
    )

    rules = install.claude_store_deny_rules(project)

    assert f"Read({_spec(home, '/**')})" not in rules
    assert f"Read({_spec(db, '*')})" in rules
    assert f"Read({_spec(home / '.locks', '/**')})" in rules
    assert "Read(/.strata/**)" in rules
    assert all("fleet.yaml" not in rule for rule in rules)


def test_generic_home_directories_are_left_open_until_strata_creates_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    home = tmp_path / "home"
    summaries = home / "summaries"
    summaries.mkdir(parents=True)
    (summaries / "notes.txt").write_text("mine\n", encoding="utf-8")
    _patch_home(monkeypatch, home)
    _init_project(tmp_path)
    _write_config(
        tmp_path,
        "\n".join(
            [
                'db = ".strata/strata.db"',
                'fleet_yaml = ".strata/fleet.yaml"',
                f'summaries_dir = "{summaries.as_posix()}"',
                "",
            ]
        ),
    )

    plan = install.claude_store_deny_plan(tmp_path)
    sessions = home / "sessions"
    assert f"Read({_spec(summaries, '/**')})" not in plan.rules
    assert f"Read({_spec(sessions, '/**')})" not in plan.rules
    assert any(
        summaries.resolve().as_posix() in line and "home directory" in line for line in plan.skipped
    )
    assert any(
        sessions.resolve().as_posix() in line and "home directory" in line for line in plan.skipped
    )

    assert _register(tmp_path) == 0
    from strata.migrator import run_migrations

    run_migrations(str(tmp_path / ".strata" / "strata.db"))
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_root")
    monkeypatch.setenv("STRATA_AGENT_SKILL", "strata-worker")
    monkeypatch.setenv("STRATA_AGENT_SESSION_ID", "sess_test")
    capsys.readouterr()

    rc, output = _run_doctor(tmp_path, monkeypatch, capsys)
    assert rc == 0
    assert "Not denied:" in output
    assert summaries.resolve().as_posix() in output
    assert "Strata did not create it" in output


def test_generic_summaries_under_home_are_denied_when_strata_created_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = home / "proj"
    summaries = home / "summaries"
    summaries.mkdir(parents=True)
    (summaries / "g_root.md").write_text(
        "---\nscope_id: g_root\nversion: 1\n---\n# Scope: g_root\n",
        encoding="utf-8",
    )
    _patch_home(monkeypatch, home)
    project.mkdir()
    _write_config(
        project,
        "\n".join(
            [
                'db = ".strata/strata.db"',
                'fleet_yaml = ".strata/fleet.yaml"',
                f'summaries_dir = "{summaries.as_posix()}"',
                "",
            ]
        ),
    )

    rules = install.claude_store_deny_rules(project)

    assert f"Read({_spec(summaries, '/**')})" in rules
    assert f"Edit({_spec(summaries, '/**')})" in rules


def test_generic_directory_under_a_project_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    dev = home / "dev"
    project = dev / "repo"
    summaries = dev / "summaries"
    project.mkdir(parents=True)
    summaries.mkdir()
    (summaries / "notes.txt").write_text("not strata\n", encoding="utf-8")
    _patch_home(monkeypatch, home)
    _write_config(
        project,
        "\n".join(
            [
                'db = ".strata/strata.db"',
                'fleet_yaml = ".strata/fleet.yaml"',
                f'summaries_dir = "{summaries.as_posix()}"',
                "",
            ]
        ),
    )

    plan = install.claude_store_deny_plan(project)
    assert f"Read({_spec(summaries, '/**')})" not in plan.rules
    assert any(
        summaries.resolve().as_posix() in line and "ancestor of the project" in line
        for line in plan.skipped
    )

    (summaries / "notes.txt").unlink()
    (summaries / "g_root.md").write_text(
        "---\nscope_id: g_root\n---\n# Scope: g_root\n",
        encoding="utf-8",
    )
    created = install.claude_store_deny_plan(project)
    assert f"Read({_spec(summaries, '/**')})" in created.rules


def test_sessions_directory_above_home_is_not_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # summaries_dir is the home directory, so the sessions sibling is
    # <parent-of-home>/sessions — next to home, not inside it.
    home = tmp_path / "users" / "me"
    project = home / "proj"
    project.mkdir(parents=True)
    sessions = home.parent / "sessions"
    sessions.mkdir()
    (sessions / "sess.json").write_text(
        '{"connected_at": "t", "tool_calls": 1, "strict_blocks": 0}\n',
        encoding="utf-8",
    )
    _patch_home(monkeypatch, home)
    _write_config(
        project,
        "\n".join(
            [
                'db = ".strata/strata.db"',
                'fleet_yaml = ".strata/fleet.yaml"',
                f'summaries_dir = "{home.as_posix()}"',
                "",
            ]
        ),
    )

    plan = install.claude_store_deny_plan(project)

    assert f"Read({_spec(sessions, '/**')})" not in plan.rules
    assert f"Read({_spec(home, '/**')})" not in plan.rules
    assert any(
        sessions.resolve().as_posix() in line and "resolves above the home directory" in line
        for line in plan.skipped
    )
    assert any("contains the project" in line for line in plan.skipped)


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
