"""Tests for ``strata launch`` — skill resolution, .strata-role discovery,
session-ID generation, and CLI integration.

All tests that touch the CLI use the ``main()`` entry point with mocked
``httpx`` and ``os.execvpe`` so no real process is spawned and no real backend
is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from strata.__main__ import main
from strata.launch import (
    SkillResolutionError,
    StrataRoleParseError,
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


def _fake_scopes_response(scopes: list[dict]) -> MagicMock:
    """Build a mock httpx response for GET /scopes."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "strata": [{"id": "L1", "name": "Function", "ordinal": 1}],
        "scopes": scopes,
        "edges": [],
    }
    return resp


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

    def test_row4_no_skills_raises(self) -> None:
        """Row 4: neither default_skill nor permitted_skills → error."""
        with pytest.raises(SkillResolutionError, match="declares no skills"):
            resolve_skill(_SCOPE_NO_SKILLS, None, interactive=True)

    def test_row4_no_skills_non_tty_raises(self) -> None:
        """Row 4 (non-TTY): same error."""
        with pytest.raises(SkillResolutionError, match="declares no skills"):
            resolve_skill(_SCOPE_NO_SKILLS, None, interactive=False)

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
# CLI integration — backend unreachable
# ---------------------------------------------------------------------------


class TestLaunchBackendUnreachable:
    def test_backend_unreachable_exits_nonzero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Backend unreachable → exit 1 with hint to run strata start."""
        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            rc = main(["launch", "g_arch"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "strata start" in err


# ---------------------------------------------------------------------------
# CLI integration — unknown scope
# ---------------------------------------------------------------------------


class TestLaunchUnknownScope:
    def test_unknown_scope_exits_nonzero_lists_valid(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Unknown scope → exit 1, error message lists valid scope IDs."""
        resp = _fake_scopes_response([_SCOPE_DEFAULT_ONLY])
        with patch("httpx.get", return_value=resp):
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
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Non-TTY + no positional arg + no .strata-role → exit 1, no input() call."""
        resp = _fake_scopes_response([_SCOPE_DEFAULT_ONLY])
        called_input = []

        def fake_input(prompt: str = "") -> str:
            called_input.append(prompt)
            return "1"

        with (
            patch("httpx.get", return_value=resp),
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
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Interactive picker: user picks scope '1', skill resolved from default."""
        resp = _fake_scopes_response([_SCOPE_DEFAULT_ONLY])
        with (
            patch("httpx.get", return_value=resp),
            patch("strata.__main__.is_interactive", return_value=True),
            patch("builtins.input", return_value="1"),
            patch("pathlib.Path.cwd", return_value=tmp_path),
            patch("os.execvpe") as mock_exec,
        ):
            rc = main(["launch"])
        assert rc == 0
        mock_exec.assert_called_once()
        _cmd, _argv, env = mock_exec.call_args[0]
        assert env["STRATA_AGENT_SCOPE"] == "g_arch"
        assert env["STRATA_AGENT_SKILL"] == "code-writer"
        assert "STRATA_AGENT_SESSION_ID" in env


# ---------------------------------------------------------------------------
# CLI integration — execvp with correct args + env vars
# ---------------------------------------------------------------------------


class TestLaunchExecvpe:
    def test_execvpe_called_with_correct_env(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Happy path: execvpe invoked with STRATA_AGENT_* env vars."""
        resp = _fake_scopes_response([_SCOPE_DEFAULT_ONLY])
        with (
            patch("httpx.get", return_value=resp),
            patch("os.execvpe") as mock_exec,
        ):
            rc = main(["launch", "g_arch"])
        assert rc == 0
        mock_exec.assert_called_once()
        cmd, argv, env = mock_exec.call_args[0]
        assert cmd == "claude"
        assert argv == ["claude"]
        assert env["STRATA_AGENT_SCOPE"] == "g_arch"
        assert env["STRATA_AGENT_SKILL"] == "code-writer"
        assert env["STRATA_AGENT_SESSION_ID"].startswith("sess_g_arch_code-writer_")

    def test_session_override_propagated(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--session flag overrides auto-generated session ID."""
        resp = _fake_scopes_response([_SCOPE_DEFAULT_ONLY])
        with (
            patch("httpx.get", return_value=resp),
            patch("os.execvpe") as mock_exec,
        ):
            rc = main(["launch", "g_arch", "--session", "my-sess"])
        assert rc == 0
        _cmd, _argv, env = mock_exec.call_args[0]
        assert env["STRATA_AGENT_SESSION_ID"] == "my-sess"

    def test_skill_override_propagated(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--skill flag overrides resolved skill (must be in permitted list)."""
        resp = _fake_scopes_response([_SCOPE_DEFAULT_AND_PERMITTED])
        with (
            patch("httpx.get", return_value=resp),
            patch("os.execvpe") as mock_exec,
        ):
            rc = main(["launch", "g_arch", "--skill", "evidence-summarizer"])
        assert rc == 0
        _cmd, _argv, env = mock_exec.call_args[0]
        assert env["STRATA_AGENT_SKILL"] == "evidence-summarizer"

    def test_skill_override_not_permitted_exits_nonzero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--skill not in permitted_skills → exit 1, no exec."""
        resp = _fake_scopes_response([_SCOPE_DEFAULT_AND_PERMITTED])
        with (
            patch("httpx.get", return_value=resp),
            patch("os.execvpe") as mock_exec,
        ):
            rc = main(["launch", "g_arch", "--skill", "scope-manager"])
        assert rc == 1
        mock_exec.assert_not_called()
        err = capsys.readouterr().err
        assert "not permitted" in err

    def test_claude_not_on_path_exits_nonzero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """FileNotFoundError from execvpe → exit 1 with clear message."""
        resp = _fake_scopes_response([_SCOPE_DEFAULT_ONLY])
        with (
            patch("httpx.get", return_value=resp),
            patch("os.execvpe", side_effect=FileNotFoundError),
        ):
            rc = main(["launch", "g_arch"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "claude" in err.lower()


# ---------------------------------------------------------------------------
# CLI integration — .strata-role discovery end-to-end
# ---------------------------------------------------------------------------


class TestLaunchStrataRole:
    def test_strata_role_scope_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """.strata-role with scope only → skill resolved from fleet declaration."""
        role = tmp_path / ".strata-role"
        role.write_text('scope = "g_arch"\n', encoding="utf-8")
        resp = _fake_scopes_response([_SCOPE_DEFAULT_ONLY])
        with (
            patch("httpx.get", return_value=resp),
            patch("pathlib.Path.cwd", return_value=tmp_path),
            patch("os.execvpe") as mock_exec,
        ):
            rc = main(["launch"])
        assert rc == 0
        _cmd, _argv, env = mock_exec.call_args[0]
        assert env["STRATA_AGENT_SCOPE"] == "g_arch"
        assert env["STRATA_AGENT_SKILL"] == "code-writer"

    def test_strata_role_scope_and_skill(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """.strata-role with scope+skill overrides resolution table."""
        role = tmp_path / ".strata-role"
        role.write_text('scope = "g_arch"\nskill = "evidence-summarizer"\n', encoding="utf-8")
        # Use a scope that would normally resolve code-writer.
        resp = _fake_scopes_response([_SCOPE_DEFAULT_ONLY])
        with (
            patch("httpx.get", return_value=resp),
            patch("pathlib.Path.cwd", return_value=tmp_path),
            patch("os.execvpe") as mock_exec,
        ):
            rc = main(["launch"])
        assert rc == 0
        _cmd, _argv, env = mock_exec.call_args[0]
        assert env["STRATA_AGENT_SKILL"] == "evidence-summarizer"

    def test_strata_role_in_parent_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """.strata-role in a parent directory is discovered from a nested cwd."""
        role = tmp_path / ".strata-role"
        role.write_text('scope = "g_arch"\n', encoding="utf-8")
        nested = tmp_path / "proj" / "src"
        nested.mkdir(parents=True)
        resp = _fake_scopes_response([_SCOPE_DEFAULT_ONLY])
        with (
            patch("httpx.get", return_value=resp),
            patch("pathlib.Path.cwd", return_value=nested),
            patch("os.execvpe") as mock_exec,
        ):
            rc = main(["launch"])
        assert rc == 0
        _cmd, _argv, env = mock_exec.call_args[0]
        assert env["STRATA_AGENT_SCOPE"] == "g_arch"

    def test_strata_role_inactive_scope_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """.strata-role referencing a non-active scope ID exits nonzero."""
        role = tmp_path / ".strata-role"
        role.write_text('scope = "g_old"\n', encoding="utf-8")
        resp = _fake_scopes_response([_SCOPE_DEFAULT_ONLY])  # only g_arch is active
        with (
            patch("httpx.get", return_value=resp),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            rc = main(["launch"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "g_old" in err


# ---------------------------------------------------------------------------
# CLI integration — no-skills scope
# ---------------------------------------------------------------------------


class TestLaunchNoSkills:
    def test_scope_declares_no_skills_exits_nonzero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Scope with no default_skill and no permitted_skills → exit 1."""
        resp = _fake_scopes_response([_SCOPE_NO_SKILLS])
        with (
            patch("httpx.get", return_value=resp),
            patch("os.execvpe") as mock_exec,
        ):
            rc = main(["launch", "g_arch"])
        assert rc == 1
        mock_exec.assert_not_called()
        err = capsys.readouterr().err
        assert "declares no skills" in err


# ---------------------------------------------------------------------------
# Session-ID format check via CLI
# ---------------------------------------------------------------------------


class TestSessionIdViaCLI:
    def test_session_id_format_in_env(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Auto-generated session ID matches pinned format."""
        resp = _fake_scopes_response([_SCOPE_DEFAULT_ONLY])
        with (
            patch("httpx.get", return_value=resp),
            patch("os.execvpe") as mock_exec,
        ):
            rc = main(["launch", "g_arch"])
        assert rc == 0
        _cmd, _argv, env = mock_exec.call_args[0]
        sid = env["STRATA_AGENT_SESSION_ID"]
        # Format: sess_<scope>_<skill>_<YYYYMMDD-HHMMSS>
        assert sid.startswith("sess_g_arch_code-writer_")
        # Timestamp portion: 8 digits, dash, 6 digits.
        ts_part = sid.split("_")[-1]
        assert len(ts_part) == 15  # YYYYMMDD-HHMMSS
        assert ts_part[8] == "-"
