"""Tests for ``strata launch`` — skill resolution, .strata-role discovery,
session-ID generation, and CLI integration.

All tests that touch the CLI use the ``main()`` entry point with a real
fleet.yaml on disk (launch reads the fleet directly — embedded mode, issue
#45; no backend and no HTTP involved) and a mocked ``exec_claude`` seam so no
real process is spawned. The platform-specific handoff inside ``exec_claude``
(POSIX ``execvpe`` vs. the Windows spawn — issue #20) is covered separately by
``TestExecClaudePosix`` / ``TestExecClaudeWindowsBranch`` /
``TestExecClaudeWindowsIntegration``.
"""

from __future__ import annotations

import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from strata.__main__ import main
from strata.launch import (
    SkillResolutionError,
    StrataRoleParseError,
    _windows_claude_argv,
    exec_claude,
    find_strata_role,
    make_session_id,
    parse_strata_role,
    resolve_skill,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SCOPE_DEFAULT_ONLY: dict = {
    "id": "g_arch",
    "name": "Architect",
    "stratum_id": "L1",
    "status": "active",
    "default_skill": "code-writer",
    "permitted_skills": None,
}

_SCOPE_DEFAULT_AND_PERMITTED: dict = {
    "id": "g_arch",
    "name": "Architect",
    "stratum_id": "L1",
    "status": "active",
    "default_skill": "code-writer",
    "permitted_skills": ["code-writer", "evidence-summarizer"],
}

_SCOPE_PERMITTED_ONLY: dict = {
    "id": "g_arch",
    "name": "Architect",
    "stratum_id": "L1",
    "status": "active",
    "default_skill": None,
    "permitted_skills": ["code-writer", "evidence-summarizer"],
}

_SCOPE_NO_SKILLS: dict = {
    "id": "g_arch",
    "name": "Architect",
    "stratum_id": "L1",
    "status": "active",
    "default_skill": None,
    "permitted_skills": None,
}


def _write_fleet(tmp_path: Path, scopes: list[dict]) -> Path:
    """Write a valid fleet.yaml containing *scopes* and return its path."""
    fleet = {
        "strata": [{"id": "L1", "name": "Function", "ordinal": 1}],
        "scopes": [
            {k: v for k, v in sc.items() if v is not None and k != "status"} for sc in scopes
        ],
        "edges": [],
    }
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.dump(fleet), encoding="utf-8")
    return path


@pytest.fixture
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point STRATA_FLEET_CONFIG at a tmp fleet.yaml written per test.

    Yields a function ``set_scopes(scopes) -> Path``. Clears the Settings
    cache on setup and teardown so the env var takes effect and never leaks.
    """
    from strata.settings import get_settings

    def set_scopes(scopes: list[dict]) -> Path:
        path = _write_fleet(tmp_path, scopes)
        monkeypatch.setenv("STRATA_FLEET_CONFIG", str(path))
        get_settings.cache_clear()
        return path

    get_settings.cache_clear()
    yield set_scopes
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Skill resolution — one test per resolution-table row
# ---------------------------------------------------------------------------


class TestResolveSkill:
    """Unit tests for resolve_skill(); no I/O, no HTTP."""

    def test_row1_default_only_uses_default(self) -> None:
        """Row 1: default_skill set, permitted_skills unset → use default."""
        skill = resolve_skill(_SCOPE_DEFAULT_ONLY, None, interactive=False)
        assert skill == "code-writer"

    def test_row1_default_only_tty_uses_default(self) -> None:
        """Row 1 (TTY): same result regardless of TTY status."""
        skill = resolve_skill(_SCOPE_DEFAULT_ONLY, None, interactive=True)
        assert skill == "code-writer"

    def test_row2_default_and_permitted_uses_default(self) -> None:
        """Row 2: both set → use default_skill."""
        skill = resolve_skill(_SCOPE_DEFAULT_AND_PERMITTED, None, interactive=False)
        assert skill == "code-writer"

    def test_row2_skill_override_permitted(self) -> None:
        """Row 2: --skill in permitted_skills → use it."""
        skill = resolve_skill(
            _SCOPE_DEFAULT_AND_PERMITTED, "evidence-summarizer", interactive=False
        )
        assert skill == "evidence-summarizer"

    def test_row2_skill_override_not_permitted_raises(self) -> None:
        """Row 2: --skill NOT in permitted_skills → SkillResolutionError."""
        with pytest.raises(SkillResolutionError, match="not permitted"):
            resolve_skill(_SCOPE_DEFAULT_AND_PERMITTED, "scope-manager", interactive=False)

    def test_row3_permitted_only_non_tty_raises(self) -> None:
        """Row 3 (non-TTY): permitted only, no default → error."""
        with pytest.raises(SkillResolutionError, match="no default_skill"):
            resolve_skill(_SCOPE_PERMITTED_ONLY, None, interactive=False)

    def test_row3_permitted_only_tty_prompts(self) -> None:
        """Row 3 (TTY): permitted only → prompt user; mocked input returns '1'."""
        with patch("builtins.input", return_value="1"):
            skill = resolve_skill(_SCOPE_PERMITTED_ONLY, None, interactive=True)
        assert skill == "code-writer"

    def test_row3_permitted_only_tty_pick_by_name(self) -> None:
        """Row 3 (TTY): pick by skill name instead of number."""
        with patch("builtins.input", return_value="evidence-summarizer"):
            skill = resolve_skill(_SCOPE_PERMITTED_ONLY, None, interactive=True)
        assert skill == "evidence-summarizer"

    def test_row4_no_skills_resolves_none(self) -> None:
        """Row 4 (issue #121): neither default_skill nor permitted_skills → None.

        A skill carries a body or it is omitted; an unrestricted scope binds
        skill-less rather than raising (was SkillResolutionError before #121).
        """
        assert resolve_skill(_SCOPE_NO_SKILLS, None, interactive=True) is None

    def test_row4_no_skills_non_tty_resolves_none(self) -> None:
        """Row 4 (non-TTY): same — None, regardless of TTY."""
        assert resolve_skill(_SCOPE_NO_SKILLS, None, interactive=False) is None

    def test_row4_no_skills_explicit_skill_passes_through(self) -> None:
        """Row 4 with an explicit --skill on an unrestricted scope: accepted as-is."""
        assert (
            resolve_skill(_SCOPE_NO_SKILLS, "code-writer", interactive=False) == "code-writer"
        )

    def test_skill_flag_no_permitted_list_accepted(self) -> None:
        """--skill accepted when permitted_skills is None (no restriction)."""
        skill = resolve_skill(_SCOPE_DEFAULT_ONLY, "scope-manager", interactive=False)
        assert skill == "scope-manager"


# ---------------------------------------------------------------------------
# .strata-role parsing
# ---------------------------------------------------------------------------


class TestParseStrataRole:
    def test_scope_only(self, tmp_path: Path) -> None:
        """Scope-only .strata-role returns (scope_id, None)."""
        role = tmp_path / ".strata-role"
        role.write_text('scope = "g_arch"\n', encoding="utf-8")
        scope_id, skill = parse_strata_role(role)
        assert scope_id == "g_arch"
        assert skill is None

    def test_scope_and_skill(self, tmp_path: Path) -> None:
        """scope + skill .strata-role returns both."""
        role = tmp_path / ".strata-role"
        role.write_text('scope = "g_arch"\nskill = "code-writer"\n', encoding="utf-8")
        scope_id, skill = parse_strata_role(role)
        assert scope_id == "g_arch"
        assert skill == "code-writer"

    def test_missing_scope_raises(self, tmp_path: Path) -> None:
        """Missing 'scope' key raises StrataRoleParseError."""
        role = tmp_path / ".strata-role"
        role.write_text('skill = "code-writer"\n', encoding="utf-8")
        with pytest.raises(StrataRoleParseError, match="missing required field 'scope'"):
            parse_strata_role(role)

    def test_invalid_toml_raises(self, tmp_path: Path) -> None:
        """Invalid TOML raises StrataRoleParseError."""
        role = tmp_path / ".strata-role"
        role.write_text("scope = [\n", encoding="utf-8")
        with pytest.raises(StrataRoleParseError, match="not valid TOML"):
            parse_strata_role(role)


# ---------------------------------------------------------------------------
# .strata-role discovery (ancestor walk)
# ---------------------------------------------------------------------------


class TestFindStrataRole:
    def test_found_in_current_dir(self, tmp_path: Path) -> None:
        """File in the start dir is found immediately."""
        role = tmp_path / ".strata-role"
        role.write_text('scope = "g_arch"\n', encoding="utf-8")
        found = find_strata_role(tmp_path)
        assert found == role

    def test_found_in_parent(self, tmp_path: Path) -> None:
        """File in a parent dir is found when starting from a nested dir."""
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        role = tmp_path / ".strata-role"
        role.write_text('scope = "g_arch"\n', encoding="utf-8")
        # No .git present → walks to filesystem root, but role is at tmp_path.
        found = find_strata_role(nested)
        assert found == role

    def test_stops_at_git_root(self, tmp_path: Path) -> None:
        """Walk stops at the directory containing .git."""
        git_root = tmp_path / "repo"
        git_root.mkdir()
        (git_root / ".git").mkdir()
        nested = git_root / "sub" / "dir"
        nested.mkdir(parents=True)
        # .strata-role is ABOVE the git root — should NOT be found.
        role_above = tmp_path / ".strata-role"
        role_above.write_text('scope = "g_arch"\n', encoding="utf-8")
        found = find_strata_role(nested)
        assert found is None

    def test_found_at_git_root(self, tmp_path: Path) -> None:
        """.strata-role AT the git root is found when starting from a subdirectory."""
        git_root = tmp_path / "repo"
        git_root.mkdir()
        (git_root / ".git").mkdir()
        nested = git_root / "sub"
        nested.mkdir()
        role = git_root / ".strata-role"
        role.write_text('scope = "g_arch"\n', encoding="utf-8")
        found = find_strata_role(nested)
        assert found == role

    def test_absent(self, tmp_path: Path) -> None:
        """None returned when no file exists anywhere up the tree."""
        # Use a dir inside tmp_path that has no .strata-role.
        nested = tmp_path / "no_role"
        nested.mkdir()
        # Place a .git here so the walk stops.
        (nested / ".git").mkdir()
        found = find_strata_role(nested)
        assert found is None


# ---------------------------------------------------------------------------
# Session-ID generation
# ---------------------------------------------------------------------------


class TestMakeSessionId:
    def test_format_pinned(self) -> None:
        """Session ID matches the ADR 0003 pinned format."""
        ts = datetime(2026, 5, 27, 13, 42, 15, tzinfo=UTC)
        sid = make_session_id("g_arch", "code-writer", ts=ts)
        assert sid == "sess_g_arch_code-writer_20260527-134215"

    def test_session_override(self) -> None:
        """--session override bypasses auto-generation."""
        custom = "my-custom-session"
        # This is tested through cmd_launch, but make_session_id should not be
        # called at all when --session is provided.  Verify by asserting the
        # custom value is returned directly from cmd_launch (CLI integration
        # test below).
        ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        sid = make_session_id("s", "sk", ts=ts)
        assert sid != custom  # just a sanity check; override is in CLI test


# ---------------------------------------------------------------------------
# CLI integration — no fleet config (launch reads fleet.yaml directly; #45)
# ---------------------------------------------------------------------------


class TestLaunchNoFleet:
    def test_missing_fleet_exits_nonzero_with_actionable_message(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No fleet.yaml anywhere → exit 1 pointing at register / start."""
        from strata.settings import get_settings

        monkeypatch.setenv("STRATA_FLEET_CONFIG", str(tmp_path / "absent.yaml"))
        get_settings.cache_clear()
        try:
            rc = main(["launch", "g_arch"])
        finally:
            get_settings.cache_clear()
        assert rc == 1
        err = capsys.readouterr().err
        assert "strata register" in err

    def test_invalid_fleet_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Invalid fleet.yaml → exit 1 with the FleetConfig error, no traceback."""
        from strata.settings import get_settings

        bad = tmp_path / "fleet.yaml"
        bad.write_text(
            "strata:\n  - id: L1\n    name: F\n    ordinal: 1\n"
            "scopes:\n  - id: g_a\n    name: A\n    stratum_id: NOPE\n"
            "edges: []\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("STRATA_FLEET_CONFIG", str(bad))
        get_settings.cache_clear()
        try:
            rc = main(["launch", "g_a"])
        finally:
            get_settings.cache_clear()
        assert rc == 1
        err = capsys.readouterr().err
        assert "Fleet config invalid" in err


# ---------------------------------------------------------------------------
# CLI integration — unknown scope
# ---------------------------------------------------------------------------


class TestLaunchUnknownScope:
    def test_unknown_scope_exits_nonzero_lists_valid(
        self, fleet_env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Unknown scope → exit 1, error message lists valid scope IDs."""
        fleet_env([_SCOPE_DEFAULT_ONLY])
        rc = main(["launch", "g_nope"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "g_arch" in err
        assert "g_nope" in err


# ---------------------------------------------------------------------------
# CLI integration — non-TTY fail-fast
# ---------------------------------------------------------------------------


class TestLaunchNonTTY:
    def test_no_arg_no_role_non_tty_exits_nonzero(
        self, fleet_env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Non-TTY + no positional arg + no .strata-role → exit 1, no input() call."""
        fleet_env([_SCOPE_DEFAULT_ONLY])
        called_input = []

        def fake_input(prompt: str = "") -> str:
            called_input.append(prompt)
            return "1"

        with (
            patch("strata.__main__.is_interactive", return_value=False),
            patch("builtins.input", side_effect=fake_input),
            # Run from tmp_path which has no .strata-role and no .git nearby.
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            # Ensure no .strata-role exists above tmp_path in this test.
            # find_strata_role will walk upward; we patch cwd to tmp_path.
            # tmp_path itself has no .strata-role; the walk stops at filesystem root.
            rc = main(["launch"])
        assert rc == 1
        assert called_input == [], "input() must not be called in non-TTY context"
        err = capsys.readouterr().err
        assert "g_arch" in err


# ---------------------------------------------------------------------------
# CLI integration — interactive picker
# ---------------------------------------------------------------------------


class TestLaunchPicker:
    def test_picker_resolves_scope_and_skill(
        self, fleet_env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Interactive picker: user picks scope '1', skill resolved from default."""
        fleet_env([_SCOPE_DEFAULT_ONLY])
        with (
            patch("strata.__main__.is_interactive", return_value=True),
            patch("builtins.input", return_value="1"),
            patch("pathlib.Path.cwd", return_value=tmp_path),
            patch("strata.__main__.exec_claude", return_value=0) as mock_exec,
        ):
            rc = main(["launch"])
        assert rc == 0
        mock_exec.assert_called_once()
        (env,) = mock_exec.call_args[0]
        assert env["STRATA_AGENT_SCOPE"] == "g_arch"
        assert env["STRATA_AGENT_SKILL"] == "code-writer"
        assert "STRATA_AGENT_SESSION_ID" in env


# ---------------------------------------------------------------------------
# CLI integration — handoff receives the correct env (platform-independent)
# ---------------------------------------------------------------------------


class TestLaunchHandoff:
    """cmd_launch builds STRATA_AGENT_* and hands it to exec_claude.

    The exec_claude seam is patched, so these assert the launcher's binding job
    on any platform. The OS handoff mechanism itself is covered per-platform by
    TestExecClaude.
    """

    def test_handoff_called_with_correct_env(
        self, fleet_env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Happy path: exec_claude invoked with STRATA_AGENT_* env vars."""
        fleet_env([_SCOPE_DEFAULT_ONLY])
        with patch("strata.__main__.exec_claude", return_value=0) as mock_exec:
            rc = main(["launch", "g_arch"])
        assert rc == 0
        mock_exec.assert_called_once()
        (env,) = mock_exec.call_args[0]
        assert env["STRATA_AGENT_SCOPE"] == "g_arch"
        assert env["STRATA_AGENT_SKILL"] == "code-writer"
        assert env["STRATA_AGENT_SESSION_ID"].startswith("sess_g_arch_code-writer_")

    def test_child_exit_code_propagates(
        self, fleet_env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """cmd_launch returns exec_claude's value verbatim (Windows exit code)."""
        fleet_env([_SCOPE_DEFAULT_ONLY])
        with patch("strata.__main__.exec_claude", return_value=42):
            rc = main(["launch", "g_arch"])
        assert rc == 42

    def test_session_override_propagated(
        self, fleet_env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--session flag overrides auto-generated session ID."""
        fleet_env([_SCOPE_DEFAULT_ONLY])
        with patch("strata.__main__.exec_claude", return_value=0) as mock_exec:
            rc = main(["launch", "g_arch", "--session", "my-sess"])
        assert rc == 0
        (env,) = mock_exec.call_args[0]
        assert env["STRATA_AGENT_SESSION_ID"] == "my-sess"

    def test_skill_override_propagated(self, fleet_env, capsys: pytest.CaptureFixture[str]) -> None:
        """--skill flag overrides resolved skill (must be in permitted list)."""
        fleet_env([_SCOPE_DEFAULT_AND_PERMITTED])
        with patch("strata.__main__.exec_claude", return_value=0) as mock_exec:
            rc = main(["launch", "g_arch", "--skill", "evidence-summarizer"])
        assert rc == 0
        (env,) = mock_exec.call_args[0]
        assert env["STRATA_AGENT_SKILL"] == "evidence-summarizer"

    def test_skill_override_not_permitted_exits_nonzero(
        self, fleet_env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--skill not in permitted_skills → exit 1, no exec."""
        fleet_env([_SCOPE_DEFAULT_AND_PERMITTED])
        with patch("strata.__main__.exec_claude", return_value=0) as mock_exec:
            rc = main(["launch", "g_arch", "--skill", "scope-manager"])
        assert rc == 1
        mock_exec.assert_not_called()
        err = capsys.readouterr().err
        assert "not permitted" in err

    def test_claude_not_on_path_exits_nonzero(
        self, fleet_env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FileNotFoundError from exec_claude → exit 1 with clear message.

        Parity: raised by execvpe on POSIX and by shutil.which on Windows;
        cmd_launch reports one message either way.
        """
        fleet_env([_SCOPE_DEFAULT_ONLY])
        with patch("strata.__main__.exec_claude", side_effect=FileNotFoundError):
            rc = main(["launch", "g_arch"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "claude" in err.lower()


# ---------------------------------------------------------------------------
# CLI integration — .strata-role discovery end-to-end
# ---------------------------------------------------------------------------


class TestLaunchStrataRole:
    def test_strata_role_scope_only(
        self, fleet_env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """.strata-role with scope only → skill resolved from fleet declaration."""
        role = tmp_path / ".strata-role"
        role.write_text('scope = "g_arch"\n', encoding="utf-8")
        fleet_env([_SCOPE_DEFAULT_ONLY])
        with (
            patch("pathlib.Path.cwd", return_value=tmp_path),
            patch("strata.__main__.exec_claude", return_value=0) as mock_exec,
        ):
            rc = main(["launch"])
        assert rc == 0
        (env,) = mock_exec.call_args[0]
        assert env["STRATA_AGENT_SCOPE"] == "g_arch"
        assert env["STRATA_AGENT_SKILL"] == "code-writer"

    def test_strata_role_scope_and_skill(
        self, fleet_env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """.strata-role with scope+skill overrides resolution table."""
        role = tmp_path / ".strata-role"
        role.write_text('scope = "g_arch"\nskill = "evidence-summarizer"\n', encoding="utf-8")
        # Use a scope that would normally resolve code-writer.
        fleet_env([_SCOPE_DEFAULT_ONLY])
        with (
            patch("pathlib.Path.cwd", return_value=tmp_path),
            patch("strata.__main__.exec_claude", return_value=0) as mock_exec,
        ):
            rc = main(["launch"])
        assert rc == 0
        (env,) = mock_exec.call_args[0]
        assert env["STRATA_AGENT_SKILL"] == "evidence-summarizer"

    def test_strata_role_in_parent_dir(
        self, fleet_env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """.strata-role in a parent directory is discovered from a nested cwd."""
        role = tmp_path / ".strata-role"
        role.write_text('scope = "g_arch"\n', encoding="utf-8")
        nested = tmp_path / "proj" / "src"
        nested.mkdir(parents=True)
        fleet_env([_SCOPE_DEFAULT_ONLY])
        with (
            patch("pathlib.Path.cwd", return_value=nested),
            patch("strata.__main__.exec_claude", return_value=0) as mock_exec,
        ):
            rc = main(["launch"])
        assert rc == 0
        (env,) = mock_exec.call_args[0]
        assert env["STRATA_AGENT_SCOPE"] == "g_arch"

    def test_strata_role_inactive_scope_exits_nonzero(
        self, fleet_env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """.strata-role referencing a non-active scope ID exits nonzero."""
        role = tmp_path / ".strata-role"
        role.write_text('scope = "g_old"\n', encoding="utf-8")
        fleet_env([_SCOPE_DEFAULT_ONLY])  # only g_arch is active
        with patch("pathlib.Path.cwd", return_value=tmp_path):
            rc = main(["launch"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "g_old" in err


# ---------------------------------------------------------------------------
# CLI integration — no-skills scope
# ---------------------------------------------------------------------------


class TestLaunchNoSkills:
    def test_scope_declares_no_skills_launches_skill_less(
        self, fleet_env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Scope with no default_skill and no permitted_skills → launches skill-less.

        Issue #121: an unrestricted scope no longer errors. The launch
        proceeds, STRATA_AGENT_SKILL is left UNSET (never an empty or "None"
        placeholder), and the session id omits the skill segment.
        """
        fleet_env([_SCOPE_NO_SKILLS])
        with patch("strata.__main__.exec_claude", return_value=0) as mock_exec:
            rc = main(["launch", "g_arch"])
        assert rc == 0
        mock_exec.assert_called_once()
        (env,) = mock_exec.call_args[0]
        assert env["STRATA_AGENT_SCOPE"] == "g_arch"
        assert "STRATA_AGENT_SKILL" not in env
        # Session id: sess_<scope>_<YYYYMMDD-HHMMSS>, no skill segment.
        assert env["STRATA_AGENT_SESSION_ID"].startswith("sess_g_arch_")
        assert "code-writer" not in env["STRATA_AGENT_SESSION_ID"]

    def test_scope_declares_no_skills_explicit_skill_still_binds(
        self, fleet_env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An explicit --skill on an unrestricted scope passes through (issue #121)."""
        fleet_env([_SCOPE_NO_SKILLS])
        with patch("strata.__main__.exec_claude", return_value=0) as mock_exec:
            rc = main(["launch", "g_arch", "--skill", "code-writer"])
        assert rc == 0
        (env,) = mock_exec.call_args[0]
        assert env["STRATA_AGENT_SKILL"] == "code-writer"


# ---------------------------------------------------------------------------
# Session-ID format check via CLI
# ---------------------------------------------------------------------------


class TestSessionIdViaCLI:
    def test_session_id_format_in_env(self, fleet_env, capsys: pytest.CaptureFixture[str]) -> None:
        """Auto-generated session ID matches pinned format."""
        fleet_env([_SCOPE_DEFAULT_ONLY])
        with patch("strata.__main__.exec_claude", return_value=0) as mock_exec:
            rc = main(["launch", "g_arch"])
        assert rc == 0
        (env,) = mock_exec.call_args[0]
        sid = env["STRATA_AGENT_SESSION_ID"]
        # Format: sess_<scope>_<skill>_<YYYYMMDD-HHMMSS>
        assert sid.startswith("sess_g_arch_code-writer_")
        # Timestamp portion: 8 digits, dash, 6 digits.
        ts_part = sid.split("_")[-1]
        assert len(ts_part) == 15  # YYYYMMDD-HHMMSS
        assert ts_part[8] == "-"


# ---------------------------------------------------------------------------
# exec_claude — the platform-specific handoff (issue #20)
# ---------------------------------------------------------------------------


class TestWindowsClaudeArgv:
    """_windows_claude_argv routing — pure, runs on every platform."""

    def test_exe_spawned_directly(self) -> None:
        assert _windows_claude_argv(r"C:\tools\claude.exe") == [r"C:\tools\claude.exe"]

    def test_cmd_shim_routed_through_comspec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
        assert _windows_claude_argv(r"C:\npm\claude.cmd") == [
            r"C:\Windows\System32\cmd.exe",
            "/c",
            r"C:\npm\claude.cmd",
        ]

    def test_bat_shim_routed_through_comspec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COMSPEC", "cmd.exe")
        assert _windows_claude_argv(r"C:\x\claude.bat") == ["cmd.exe", "/c", r"C:\x\claude.bat"]

    def test_extension_match_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COMSPEC", "cmd.exe")
        assert _windows_claude_argv(r"C:\x\CLAUDE.CMD")[:2] == ["cmd.exe", "/c"]
        assert _windows_claude_argv(r"C:\x\CLAUDE.EXE") == [r"C:\x\CLAUDE.EXE"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec path (execvpe)")
class TestExecClaudePosix:
    """POSIX keeps true process replacement via os.execvpe."""

    def test_execvpe_called_with_claude_and_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """exec_claude replaces the image with claude, argv=['claude'], env passed."""
        execvpe = MagicMock()
        monkeypatch.setattr("strata.launch.os.execvpe", execvpe)
        env = {"STRATA_AGENT_SCOPE": "g_arch"}
        exec_claude(env)
        execvpe.assert_called_once_with("claude", ["claude"], env)

    def test_execvpe_filenotfound_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing claude surfaces as FileNotFoundError for the caller."""
        monkeypatch.setattr("strata.launch.os.execvpe", MagicMock(side_effect=FileNotFoundError))
        with pytest.raises(FileNotFoundError):
            exec_claude({"STRATA_AGENT_SCOPE": "g_arch"})


class TestExecClaudeWindowsBranch:
    """Windows spawn-and-wait logic, forced on any host by pinning os.name.

    shutil.which / subprocess.Popen / signal.signal are mocked, so no process
    is spawned — this exercises resolution, SIGINT handling, and exit-code
    propagation deterministically on Linux CI as well as on Windows.
    """

    @pytest.fixture
    def force_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("strata.launch.os.name", "nt")

    def test_resolves_exe_spawns_and_propagates_exit_code(
        self, force_windows, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        child = MagicMock()
        child.wait.return_value = 3
        popen = MagicMock(return_value=child)
        sig = MagicMock(return_value="prev-handler")
        which = MagicMock(return_value=r"C:\t\claude.exe")
        monkeypatch.setattr("strata.launch.shutil.which", which)
        monkeypatch.setattr("strata.launch.subprocess.Popen", popen)
        monkeypatch.setattr("strata.launch.signal.signal", sig)

        env = {"STRATA_AGENT_SCOPE": "g_arch"}
        rc = exec_claude(env)

        assert rc == 3
        popen.assert_called_once_with([r"C:\t\claude.exe"], env=env)
        child.wait.assert_called_once()

    def test_ignores_sigint_during_wait_then_restores(
        self, force_windows, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        child = MagicMock()
        child.wait.return_value = 0
        sig = MagicMock(return_value="prev-handler")
        which = MagicMock(return_value=r"C:\t\claude.exe")
        monkeypatch.setattr("strata.launch.shutil.which", which)
        monkeypatch.setattr("strata.launch.subprocess.Popen", MagicMock(return_value=child))
        monkeypatch.setattr("strata.launch.signal.signal", sig)

        exec_claude({"X": "1"})

        # First: ignore SIGINT so the child owns the interrupt. Last: restore.
        assert sig.call_args_list[0].args == (signal.SIGINT, signal.SIG_IGN)
        assert sig.call_args_list[-1].args == (signal.SIGINT, "prev-handler")

    def test_missing_claude_raises_filenotfound(
        self, force_windows, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("strata.launch.shutil.which", MagicMock(return_value=None))
        with pytest.raises(FileNotFoundError):
            exec_claude({"STRATA_AGENT_SCOPE": "g_arch"})


@pytest.mark.skipif(os.name != "nt", reason="Windows spawn path (real child)")
class TestExecClaudeWindowsIntegration:
    """End-to-end on Windows: a real fake claude spawned by exec_claude."""

    def test_spawns_child_binds_env_and_propagates_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The child sees STRATA_AGENT_* and its exit code becomes exec_claude's."""
        marker = tmp_path / "child_env.txt"
        fake = tmp_path / "claude.cmd"
        fake.write_text(
            "@echo off\r\n"
            f'>"{marker}" echo %STRATA_AGENT_SCOPE%\r\n'
            f'>>"{marker}" echo %STRATA_AGENT_SKILL%\r\n'
            f'>>"{marker}" echo %STRATA_AGENT_SESSION_ID%\r\n'
            "exit /b 7\r\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))

        env = os.environ.copy()
        env["STRATA_AGENT_SCOPE"] = "g_arch"
        env["STRATA_AGENT_SKILL"] = "code-writer"
        env["STRATA_AGENT_SESSION_ID"] = "sess_probe"

        rc = exec_claude(env)

        assert rc == 7  # exit /b 7 propagated through cmd.exe /c and Popen.wait()
        lines = marker.read_text(encoding="utf-8").split()
        assert lines == ["g_arch", "code-writer", "sess_probe"]


# ---------------------------------------------------------------------------
# Live console Ctrl-C on Windows (opt-in — needs a real console)
# ---------------------------------------------------------------------------

# Body of a fake 'claude', executed by claude.exe (a python copy) via stdin.
# Re-enables Ctrl-C (the launcher's new process group starts with it disabled),
# then exits with sentinel 42 on SIGINT. Paths arrive via env (no __file__ for
# a stdin script).
_CTRLC_CHILD = """\
import ctypes, os, signal, sys, time
def _w(p, t):
    with open(p, "w", encoding="utf-8") as f:
        f.write(t)
_w(os.environ["CHILD_STARTED_PATH"], "started")
ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
def _h(signum, frame):
    _w(os.environ["CHILD_MARKER_PATH"], "CHILD_GOT_SIGINT")
    sys.exit(42)
signal.signal(signal.SIGINT, _h)
_w(os.environ["CHILD_READY_PATH"], "ready")
time.sleep(30)
_w(os.environ["CHILD_MARKER_PATH"], "TIMEOUT")
sys.exit(99)
"""

# The launcher under test, run in its own process group so the driver can send
# CTRL_C_EVENT at it without hitting pytest. Its stdin is the child body.
_CTRLC_HARNESS = (
    "import os, sys;"
    "from strata.launch import exec_claude;"
    "e = os.environ.copy();"
    "e['STRATA_AGENT_SCOPE'] = 'g_arch';"
    "e['STRATA_AGENT_SKILL'] = 'code-writer';"
    "e['STRATA_AGENT_SESSION_ID'] = 'sess_ctrlc';"
    "sys.exit(exec_claude(e))"
)


@pytest.mark.ctrlc
@pytest.mark.skipif(os.name != "nt", reason="Windows console Ctrl-C path")
def test_ctrlc_launcher_survives_child_handles_and_exit_code_propagates(
    tmp_path: Path,
) -> None:
    """Live Ctrl-C: launcher ignores SIGINT and survives, child handles it, and
    the child's exit code propagates.

    Opt-in (needs a real console): set STRATA_RUN_CTRLC=1. Delivers a real
    CTRL_C_EVENT to the launcher's process group and asserts exec_claude
    returned the child's sentinel exit code (42).
    """
    if os.environ.get("STRATA_RUN_CTRLC") != "1":
        pytest.skip("Set STRATA_RUN_CTRLC=1 to run the live Ctrl-C test.")

    import ctypes
    import shutil
    import subprocess
    import sys
    import time

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # A real .exe fake claude (a python copy): exec_claude spawns it directly —
    # no cmd.exe layer — and it runs the child body from its inherited stdin.
    shutil.copy(sys.executable, bin_dir / "claude.exe")

    child_py = tmp_path / "child.py"
    child_py.write_text(_CTRLC_CHILD, encoding="utf-8")
    ready = tmp_path / "ready.txt"
    marker = tmp_path / "marker.txt"

    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    env["CHILD_STARTED_PATH"] = str(tmp_path / "started.txt")
    env["CHILD_READY_PATH"] = str(ready)
    env["CHILD_MARKER_PATH"] = str(marker)

    create_new_process_group = 0x00000200
    ctrl_c_event = 0

    with open(child_py, "rb") as child_stdin:
        proc = subprocess.Popen(
            [sys.executable, "-c", _CTRLC_HARNESS],
            stdin=child_stdin,
            env=env,
            creationflags=create_new_process_group,
        )
        deadline = time.time() + 15
        while time.time() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists(), "fake claude child never signaled readiness"
        time.sleep(0.5)  # let the child enter its wait before the interrupt

        sent = ctypes.windll.kernel32.GenerateConsoleCtrlEvent(ctrl_c_event, proc.pid)
        assert sent, "GenerateConsoleCtrlEvent failed"

        rc = proc.wait(timeout=15)

    assert marker.read_text(encoding="utf-8") == "CHILD_GOT_SIGINT"  # child handled it
    assert rc == 42  # launcher survived the Ctrl-C and propagated the child's exit code
