"""Tests for 3d: strata register command (ADR 0005 Decision 4).

Tests:
1. No project marker → exit 1 with clear message.
2. Fresh project with .git marker → creates all artifacts.
3. Idempotent re-run → skips all existing artifacts.
4. Collision-skip on existing skills is reported.
5. Collision-skip on existing settings.json mcpServers.strata is reported.
6. --diff mode prints what would differ without writing.
7. Existing .strata/config.toml is not overwritten.
8. Existing .strata/fleet.yaml is not overwritten.
9. .gitignore block is idempotent (not duplicated on re-run).
10. --bootstrap-venv marked as slow (skipped in CI unless explicitly requested).

Vocabulary: scope, fleet, skill, scope-manager.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.__main__ import _build_parser, cmd_register

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(
    path: str | None = None,
    diff: bool = False,
    bootstrap_venv: bool = False,
):
    """Build a minimal argparse Namespace for cmd_register."""
    import argparse

    return argparse.Namespace(
        path=path,
        diff=diff,
        bootstrap_venv=bootstrap_venv,
    )


def _run_register(tmp_path: Path, *, diff: bool = False, bootstrap_venv: bool = False) -> int:
    """Run cmd_register against tmp_path (which must have a .git marker)."""
    args = _make_args(path=str(tmp_path), diff=diff, bootstrap_venv=bootstrap_venv)
    return cmd_register(args)


def _init_project(tmp_path: Path) -> None:
    """Create a minimal project with a .git marker."""
    (tmp_path / ".git").mkdir()


# ---------------------------------------------------------------------------
# Test 1: No project marker → exit 1
# ---------------------------------------------------------------------------


def test_no_project_marker_exits_1(tmp_path: Path, capsys) -> None:
    """A directory with no project markers must cause cmd_register to return 1."""
    # tmp_path has no .git, pyproject.toml, etc.
    args = _make_args(path=str(tmp_path))
    rc = cmd_register(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "not a project root" in captured.err.lower() or ".git" in captured.err


def test_no_project_marker_message_lists_markers(tmp_path: Path, capsys) -> None:
    """The error message must name at least one expected project marker."""
    args = _make_args(path=str(tmp_path))
    cmd_register(args)

    captured = capsys.readouterr()
    assert ".git" in captured.err


# ---------------------------------------------------------------------------
# Test 2: Fresh project → creates all artifacts
# ---------------------------------------------------------------------------


def test_fresh_project_creates_strata_dir(tmp_path: Path) -> None:
    """strata register must create .strata/ in a fresh project."""
    _init_project(tmp_path)
    rc = _run_register(tmp_path)

    assert rc == 0
    assert (tmp_path / ".strata").is_dir()


def test_fresh_project_creates_config_toml(tmp_path: Path) -> None:
    """strata register must create .strata/config.toml in a fresh project."""
    _init_project(tmp_path)
    _run_register(tmp_path)

    config = tmp_path / ".strata" / "config.toml"
    assert config.exists()
    content = config.read_text(encoding="utf-8")
    assert "fleet_yaml" in content
    assert "summaries_dir" in content
    assert "db" in content


def test_fresh_project_creates_fleet_yaml(tmp_path: Path) -> None:
    """strata register must seed .strata/fleet.yaml in a fresh project."""
    _init_project(tmp_path)
    _run_register(tmp_path)

    fleet = tmp_path / ".strata" / "fleet.yaml"
    assert fleet.exists()
    content = fleet.read_text(encoding="utf-8")
    assert "strata" in content
    assert "scopes" in content


def test_fresh_project_creates_skills(tmp_path: Path) -> None:
    """strata register must copy strata* skills to .claude/skills/ in a fresh project."""
    _init_project(tmp_path)
    _run_register(tmp_path)

    for skill in ["strata", "strata-worker", "strata-inspect"]:
        skill_md = tmp_path / ".claude" / "skills" / skill / "Skill.md"
        assert skill_md.exists(), f"Expected {skill_md} to exist after strata register"


def test_fresh_project_creates_settings_json(tmp_path: Path) -> None:
    """strata register must merge strata mcpServer into .claude/settings.json."""
    _init_project(tmp_path)
    _run_register(tmp_path)

    settings = tmp_path / ".claude" / "settings.json"
    assert settings.exists()
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert "mcpServers" in data
    assert "strata" in data["mcpServers"]
    assert data["mcpServers"]["strata"]["command"] == "strata-mcp"


def test_fresh_project_updates_gitignore(tmp_path: Path) -> None:
    """strata register must append a .gitignore block with .strata/ patterns."""
    _init_project(tmp_path)
    _run_register(tmp_path)

    gitignore = tmp_path / ".gitignore"
    assert gitignore.exists()
    content = gitignore.read_text(encoding="utf-8")
    assert ".strata/.venv/" in content
    assert ".strata/strata.db*" in content
    assert ".strata/summaries/" in content
    # fleet.yaml must NOT be ignored.
    assert ".strata/fleet.yaml" not in content


# ---------------------------------------------------------------------------
# Test 3: Idempotent re-run skips everything
# ---------------------------------------------------------------------------


def test_idempotent_rerun_returns_zero(tmp_path: Path) -> None:
    """strata register run twice must succeed both times (idempotent)."""
    _init_project(tmp_path)
    rc1 = _run_register(tmp_path)
    rc2 = _run_register(tmp_path)

    assert rc1 == 0
    assert rc2 == 0


def test_idempotent_rerun_does_not_duplicate_gitignore(tmp_path: Path) -> None:
    """strata register run twice must not duplicate the .gitignore Strata block."""
    _init_project(tmp_path)
    _run_register(tmp_path)
    _run_register(tmp_path)

    gitignore = tmp_path / ".gitignore"
    content = gitignore.read_text(encoding="utf-8")
    # Count occurrences of the Strata marker.
    assert content.count("# Strata") == 1, (
        "Strata block duplicated in .gitignore on second register run"
    )


def test_idempotent_rerun_does_not_modify_config_toml(tmp_path: Path) -> None:
    """strata register re-run must not touch .strata/config.toml if it exists."""
    _init_project(tmp_path)
    _run_register(tmp_path)

    config = tmp_path / ".strata" / "config.toml"
    original = config.read_text(encoding="utf-8")
    mtime_before = config.stat().st_mtime

    _run_register(tmp_path)

    assert config.read_text(encoding="utf-8") == original
    assert config.stat().st_mtime == mtime_before


# ---------------------------------------------------------------------------
# Test 4: Collision-skip on existing skills
# ---------------------------------------------------------------------------


def test_existing_skill_not_overwritten(tmp_path: Path) -> None:
    """strata register must not overwrite an existing .claude/skills/strata-worker/Skill.md."""
    _init_project(tmp_path)
    skill_dir = tmp_path / ".claude" / "skills" / "strata-worker"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "Skill.md"
    custom_content = "# My custom strata-worker skill\nDo not overwrite me.\n"
    skill_md.write_text(custom_content, encoding="utf-8")

    _run_register(tmp_path)

    # Custom content must be preserved.
    assert skill_md.read_text(encoding="utf-8") == custom_content


def test_existing_skill_skip_reported(tmp_path: Path, capsys) -> None:
    """strata register must report skipping an existing skill (not silently ignore)."""
    _init_project(tmp_path)
    skill_dir = tmp_path / ".claude" / "skills" / "strata-worker"
    skill_dir.mkdir(parents=True)
    (skill_dir / "Skill.md").write_text("# custom", encoding="utf-8")

    _run_register(tmp_path)

    captured = capsys.readouterr()
    # Should mention strata-worker in the skip output.
    assert "strata-worker" in captured.out


# ---------------------------------------------------------------------------
# Test 5: Collision-skip on existing settings.json strata entry
# ---------------------------------------------------------------------------


def test_existing_mcp_entry_not_overwritten(tmp_path: Path) -> None:
    """strata register must not overwrite an existing mcpServers.strata entry."""
    _init_project(tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    custom_settings = {
        "mcpServers": {
            "strata": {
                "command": "/custom/path/strata-mcp",
                "env": {"CUSTOM": "value"},
            }
        }
    }
    (claude_dir / "settings.json").write_text(
        json.dumps(custom_settings, indent=2), encoding="utf-8"
    )

    _run_register(tmp_path)

    settings = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    assert settings["mcpServers"]["strata"]["command"] == "/custom/path/strata-mcp"
    assert settings["mcpServers"]["strata"]["env"]["CUSTOM"] == "value"


def test_existing_mcp_preserves_other_keys(tmp_path: Path) -> None:
    """strata register must preserve all existing settings.json keys."""
    _init_project(tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    existing_settings = {
        "theme": "dark",
        "mcpServers": {"other-tool": {"command": "other-tool-bin"}},
        "keybindings": [],
    }
    (claude_dir / "settings.json").write_text(
        json.dumps(existing_settings, indent=2), encoding="utf-8"
    )

    _run_register(tmp_path)

    settings = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    assert settings["theme"] == "dark"
    assert "other-tool" in settings["mcpServers"]
    assert "strata" in settings["mcpServers"]
    assert settings["keybindings"] == []


# ---------------------------------------------------------------------------
# Test 6: --diff mode prints what would differ without writing
# ---------------------------------------------------------------------------


def test_diff_mode_does_not_write_files(tmp_path: Path) -> None:
    """--diff mode must not create any files."""
    _init_project(tmp_path)
    _run_register(tmp_path, diff=True)

    assert not (tmp_path / ".strata").exists()
    assert not (tmp_path / ".gitignore").exists()
    assert not (tmp_path / ".claude").exists()


def test_diff_mode_prints_would_create(tmp_path: Path, capsys) -> None:
    """--diff mode must print what would be created."""
    _init_project(tmp_path)
    _run_register(tmp_path, diff=True)

    captured = capsys.readouterr()
    out = captured.out.lower()
    assert "would" in out or "diff" in out or "create" in out


def test_diff_mode_on_existing_project_shows_unchanged(tmp_path: Path, capsys) -> None:
    """--diff mode on a fully-registered project shows all items as unchanged."""
    _init_project(tmp_path)
    _run_register(tmp_path)  # real run first
    capsys.readouterr()  # clear output

    _run_register(tmp_path, diff=True)
    captured = capsys.readouterr()
    # All items should show as unchanged/skip.
    assert "unchanged" in captured.out or "kept" in captured.out


# ---------------------------------------------------------------------------
# Test 7: Existing .strata/config.toml is not overwritten
# ---------------------------------------------------------------------------


def test_existing_config_toml_not_overwritten(tmp_path: Path) -> None:
    """strata register must not overwrite an existing .strata/config.toml."""
    _init_project(tmp_path)
    strata_dir = tmp_path / ".strata"
    strata_dir.mkdir()
    custom_config = (
        "# custom config\n"
        'db = "/custom/db.sqlite"\n'
        'fleet_yaml = "/custom/fleet.yaml"\n'
        'summaries_dir = "/custom/summaries"\n'
    )
    config = strata_dir / "config.toml"
    config.write_text(custom_config, encoding="utf-8")

    _run_register(tmp_path)

    assert config.read_text(encoding="utf-8") == custom_config


# ---------------------------------------------------------------------------
# Test 8: Existing .strata/fleet.yaml is not overwritten
# ---------------------------------------------------------------------------


def test_existing_fleet_yaml_not_overwritten(tmp_path: Path) -> None:
    """strata register must not overwrite an existing .strata/fleet.yaml."""
    _init_project(tmp_path)
    strata_dir = tmp_path / ".strata"
    strata_dir.mkdir()
    custom_fleet = "# my custom fleet\nstrata: []\nscopes: []\nedges: []\n"
    fleet = strata_dir / "fleet.yaml"
    fleet.write_text(custom_fleet, encoding="utf-8")

    _run_register(tmp_path)

    assert fleet.read_text(encoding="utf-8") == custom_fleet


# ---------------------------------------------------------------------------
# Test 9: pyproject.toml as project marker (non-git project)
# ---------------------------------------------------------------------------


def test_pyproject_toml_accepted_as_project_marker(tmp_path: Path) -> None:
    """pyproject.toml is sufficient as a project root marker."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'my-project'\n", encoding="utf-8")

    rc = _run_register(tmp_path)
    assert rc == 0
    assert (tmp_path / ".strata").is_dir()


# ---------------------------------------------------------------------------
# Test 10: --bootstrap-venv (slow — skipped in CI unless STRATA_RUN_BOOTSTRAP_VENV=1)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_bootstrap_venv_creates_venv_and_updates_settings(tmp_path: Path) -> None:
    """--bootstrap-venv must create .strata/.venv/ and update settings.json.

    This test requires network access (pip install strata) and is slow.
    Skip in CI unless STRATA_RUN_BOOTSTRAP_VENV=1.
    """
    import os

    if not os.environ.get("STRATA_RUN_BOOTSTRAP_VENV"):
        pytest.skip("Skipped in CI: set STRATA_RUN_BOOTSTRAP_VENV=1 to run")

    _init_project(tmp_path)
    rc = _run_register(tmp_path, bootstrap_venv=True)

    assert rc == 0
    venv_dir = tmp_path / ".strata" / ".venv"
    assert venv_dir.is_dir(), ".strata/.venv/ was not created"
    strata_mcp_bin = venv_dir / "bin" / "strata-mcp"
    assert strata_mcp_bin.exists(), ".strata/.venv/bin/strata-mcp was not installed"

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert str(strata_mcp_bin) == settings["mcpServers"]["strata"]["command"]


# ---------------------------------------------------------------------------
# Test 11: strata register --help works (parser wired correctly)
# ---------------------------------------------------------------------------


def test_register_in_parser() -> None:
    """'strata register --help' must not raise (parser correctly wired)."""
    parser = _build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["register", "--help"])
    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# Test 12: .strata/ collision check (ADR 0005 Decision 4 / PR #30 review)
# ---------------------------------------------------------------------------


def test_existing_strata_dir_without_config_toml_rejected(tmp_path: Path, capsys) -> None:
    """If .strata/ exists but lacks config.toml, register refuses (not a Strata workspace).

    Prevents silently writing into a foreign tool's directory and prevents
    register from running against a half-initialised state from an interrupted
    prior register.
    """
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    # Create .strata/ with content that does NOT look like a Strata workspace.
    strata_dir = tmp_path / ".strata"
    strata_dir.mkdir()
    (strata_dir / "some-other-tool.txt").write_text("not strata", encoding="utf-8")

    args = argparse.Namespace(path=str(tmp_path), diff=False, bootstrap_venv=False, python=None)
    rc = cmd_register(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "does not look like a Strata workspace" in captured.err
    # Nothing else should have been written.
    assert not (strata_dir / "config.toml").exists()
    assert not (tmp_path / ".claude").exists()


def test_existing_strata_dir_with_config_toml_proceeds(tmp_path: Path) -> None:
    """If .strata/ already has config.toml, register proceeds (idempotent re-run)."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    strata_dir = tmp_path / ".strata"
    strata_dir.mkdir()
    (strata_dir / "config.toml").write_text(
        'db = ".strata/strata.db"\nfleet_yaml = ".strata/fleet.yaml"\n'
        'summaries_dir = ".strata/summaries"\n',
        encoding="utf-8",
    )

    args = argparse.Namespace(path=str(tmp_path), diff=False, bootstrap_venv=False, python=None)
    rc = cmd_register(args)

    assert rc == 0


# ---------------------------------------------------------------------------
# Test 13: V1.2-shape mcpServers entry detection (ADR 0005 Decision 6)
# ---------------------------------------------------------------------------


def test_v1_2_shape_mcp_entry_warns(tmp_path: Path, capsys) -> None:
    """Existing V1.2-shape mcpServers.strata entry → register warns about staleness.

    The strict-additive contract holds: register does not overwrite. But it
    surfaces the upgrade-path issue (V1.2 `python -m mcp_server.strata_mcp` +
    `STRATA_BACKEND_URL` env block is broken on V1.3) at register time, when
    the user is in fix-mind.
    """
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    settings_json = settings_dir / "settings.json"
    # Stale V1.2-shape entry.
    settings_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "strata": {
                        "command": "python",
                        "args": ["-m", "mcp_server.strata_mcp"],
                        "env": {"STRATA_BACKEND_URL": "http://127.0.0.1:8000"},
                    }
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    args = argparse.Namespace(path=str(tmp_path), diff=False, bootstrap_venv=False, python=None)
    rc = cmd_register(args)

    assert rc == 0
    captured = capsys.readouterr()
    err = captured.err
    assert "V1.2-shape" in err, "expected stale-shape warning"
    assert "strata register --diff" in err, "expected --diff hint"
    # Strict-additive: the user's V1.2-shape entry is NOT overwritten.
    on_disk = json.loads(settings_json.read_text(encoding="utf-8"))
    assert on_disk["mcpServers"]["strata"]["command"] == "python", (
        "register must not overwrite user's stale mcpServer entry"
    )


def test_v3_shape_mcp_entry_no_warning(tmp_path: Path, capsys) -> None:
    """Canonical V1.3-shape mcpServers.strata entry → no V1.2 stale warning."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    settings_json = settings_dir / "settings.json"
    settings_json.write_text(
        json.dumps({"mcpServers": {"strata": {"command": "strata-mcp", "env": {}}}}, indent=2),
        encoding="utf-8",
    )

    args = argparse.Namespace(path=str(tmp_path), diff=False, bootstrap_venv=False, python=None)
    rc = cmd_register(args)

    assert rc == 0
    captured = capsys.readouterr()
    assert "V1.2-shape" not in captured.err


# ---------------------------------------------------------------------------
# Test 14: --python flag for --bootstrap-venv (ADR 0005 Decision 7)
# ---------------------------------------------------------------------------


def test_python_flag_in_parser() -> None:
    """'strata register --python /path' is accepted by the parser."""
    parser = _build_parser()
    args = parser.parse_args(["register", "--python", "/usr/bin/python3.11"])
    assert args.python == "/usr/bin/python3.11"


def test_python_flag_default_is_none() -> None:
    """When --python is not passed, args.python defaults to None (use sys.executable)."""
    parser = _build_parser()
    args = parser.parse_args(["register"])
    assert args.python is None
