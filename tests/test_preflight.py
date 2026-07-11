"""Tests for ``strata.preflight`` — prerequisite checks for strata start + launch.

Coverage:
- Each check tested in isolation (mocked OS/sys primitives).
- run_start_preflight and run_launch_preflight return the right check sets.
- Port-availability: free port → pass; held port → fail with port in message.
- Hard vs soft: hard failure → runner returns 1; only soft failures → returns 0.
- cmd_start integration: failing preflight prevents migrations from running.
- cmd_start --skip-preflight: bypasses preflight entirely.
- cmd_launch --skip-preflight: bypasses preflight entirely.

Vocabulary follows CONTEXT.md: scope, fleet, session, skill, etc.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from strata.__main__ import _run_preflight, main
from strata.preflight import (
    Check,
    _check_anthropic_api_key,
    _check_claude_on_path,
    _check_git_on_path,
    _check_port_available,
    _check_python_version,
    _check_write_perms,
    run_launch_preflight,
    run_start_preflight,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hard_fail(name: str = "test") -> Check:
    return Check(name=name, kind="hard", passed=False, message="something failed")


def _soft_fail(name: str = "test") -> Check:
    return Check(name=name, kind="soft", passed=False, message="warning only")


def _pass(name: str = "test") -> Check:
    return Check(name=name, kind="hard", passed=True, message="all good")


# ---------------------------------------------------------------------------
# Individual check: Python version
# ---------------------------------------------------------------------------


class TestCheckPythonVersion:
    def test_passes_on_311(self) -> None:
        """Passes when version_info >= (3, 11)."""
        with patch.object(sys, "version_info", (3, 11, 5, "final", 0)):
            check = _check_python_version()
        assert check.passed is True
        assert check.kind == "hard"
        assert "3.11.5" in check.message

    def test_passes_on_312(self) -> None:
        """Passes on Python 3.12."""
        with patch.object(sys, "version_info", (3, 12, 0, "final", 0)):
            check = _check_python_version()
        assert check.passed is True

    def test_fails_on_310(self) -> None:
        """Fails on Python 3.10 — hard check."""
        with patch.object(sys, "version_info", (3, 10, 9, "final", 0)):
            check = _check_python_version()
        assert check.passed is False
        assert check.kind == "hard"
        assert "3.10" in check.message

    def test_fails_on_39(self) -> None:
        """Fails on Python 3.9 — message includes actual minor."""
        with patch.object(sys, "version_info", (3, 9, 18, "final", 0)):
            check = _check_python_version()
        assert check.passed is False
        assert "3.9" in check.message


# ---------------------------------------------------------------------------
# Individual check: git on PATH
# ---------------------------------------------------------------------------


class TestCheckGitOnPath:
    def test_passes_when_git_found(self) -> None:
        """Passes when shutil.which('git') returns a path."""
        with patch("strata.preflight.shutil.which", return_value="/usr/bin/git"):
            check = _check_git_on_path()
        assert check.passed is True
        assert check.kind == "soft"

    def test_fails_when_git_absent(self) -> None:
        """Soft warning when git is not on PATH."""
        with patch("strata.preflight.shutil.which", return_value=None):
            check = _check_git_on_path()
        assert check.passed is False
        assert check.kind == "soft"
        assert "git" in check.message.lower()


# ---------------------------------------------------------------------------
# Individual check: claude CLI on PATH
# ---------------------------------------------------------------------------


class TestCheckClaudeOnPath:
    def test_passes_when_claude_found(self) -> None:
        """Passes when shutil.which('claude') returns a path."""
        with patch("strata.preflight.shutil.which", return_value="/usr/local/bin/claude"):
            check = _check_claude_on_path()
        assert check.passed is True
        assert check.kind == "soft"

    def test_fails_when_claude_absent(self) -> None:
        """Soft warning when claude CLI is not on PATH."""
        with patch("strata.preflight.shutil.which", return_value=None):
            check = _check_claude_on_path()
        assert check.passed is False
        assert check.kind == "soft"
        assert "claude" in check.message.lower()


# ---------------------------------------------------------------------------
# Individual check: write perms
# ---------------------------------------------------------------------------


class TestCheckWritePerms:
    def test_passes_when_writable(self, tmp_path: Path) -> None:
        """Passes when the parent directory is writable."""
        db_path = str(tmp_path / "strata.db")
        # tmp_path is writable in all standard CI/test environments.
        check = _check_write_perms(db_path)
        assert check.passed is True
        assert check.kind == "hard"

    def test_fails_when_not_writable(self, tmp_path: Path) -> None:
        """Hard failure when os.access reports no write permission."""
        db_path = str(tmp_path / "strata.db")
        with patch("strata.preflight.os.access", return_value=False):
            check = _check_write_perms(db_path)
        assert check.passed is False
        assert check.kind == "hard"
        assert "write" in check.message.lower() or "permission" in check.message.lower()


# ---------------------------------------------------------------------------
# Individual check: port availability
# ---------------------------------------------------------------------------


class TestCheckPortAvailable:
    def test_passes_on_free_port(self) -> None:
        """Passes when a genuinely free port is checked."""
        # Bind a socket, get the port, release it, then check.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        free_port: int = probe.getsockname()[1]
        probe.close()

        check = _check_port_available(free_port)
        assert check.passed is True
        assert check.kind == "hard"
        assert str(free_port) in check.message

    def test_fails_when_port_held(self) -> None:
        """Hard failure when a port is already bound; message contains port number."""
        # Bind and hold a socket so the preflight check cannot bind it.
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        held_port: int = holder.getsockname()[1]
        # Start listening so the OS keeps it occupied.
        holder.listen(1)
        try:
            check = _check_port_available(held_port)
        finally:
            holder.close()

        assert check.passed is False
        assert check.kind == "hard"
        assert str(held_port) in check.message

    def test_fail_message_does_not_require_lsof_pid(self) -> None:
        """The failure message is useful even without lsof (no PID assertion)."""
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        held_port = holder.getsockname()[1]
        holder.listen(1)
        # Suppress lsof so we test the no-lsof path.
        with patch("strata.preflight.shutil.which", return_value=None):
            try:
                check = _check_port_available(held_port)
            finally:
                holder.close()
        assert not check.passed
        assert str(held_port) in check.message

    def test_lsof_positive_branch_names_process(self) -> None:
        """_lsof_port_info returns ' (held by <name> PID <pid>)' when lsof finds one."""
        from strata.preflight import _lsof_port_info

        lsof_result = MagicMock(stdout="12345\n")
        ps_result = MagicMock(stdout="uvicorn\n")
        with (
            patch("strata.preflight.shutil.which", return_value="/usr/bin/lsof"),
            patch("subprocess.run", side_effect=[lsof_result, ps_result]),
        ):
            info = _lsof_port_info(8000)
        assert info == " (held by uvicorn PID 12345)"

    def test_lsof_positive_branch_empty_output(self) -> None:
        """lsof present but no listener → empty string, no crash."""
        from strata.preflight import _lsof_port_info

        with (
            patch("strata.preflight.shutil.which", return_value="/usr/bin/lsof"),
            patch("subprocess.run", return_value=MagicMock(stdout="")),
        ):
            info = _lsof_port_info(8000)
        assert info == ""


# ---------------------------------------------------------------------------
# Individual check: ANTHROPIC_API_KEY
# ---------------------------------------------------------------------------


class TestCheckAnthropicApiKey:
    def test_passes_when_key_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Passes when ANTHROPIC_API_KEY is present in the environment."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        monkeypatch.delenv("STRATA_ANTHROPIC_API_KEY", raising=False)
        check = _check_anthropic_api_key()
        assert check.passed is True
        assert check.kind == "soft"

    def test_passes_when_strata_key_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Passes when STRATA_ANTHROPIC_API_KEY is present (fallback var)."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("STRATA_ANTHROPIC_API_KEY", "sk-strata-key")
        check = _check_anthropic_api_key()
        assert check.passed is True

    def test_fails_when_key_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Soft warning when neither API key variable is set."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("STRATA_ANTHROPIC_API_KEY", raising=False)
        check = _check_anthropic_api_key()
        assert check.passed is False
        assert check.kind == "soft"
        assert "ANTHROPIC_API_KEY" in check.message


# ---------------------------------------------------------------------------
# run_start_preflight — check set
# ---------------------------------------------------------------------------


class TestRunStartPreflight:
    def test_returns_five_checks(self, tmp_path: Path) -> None:
        """run_start_preflight returns exactly 5 checks."""
        db_path = str(tmp_path / "strata.db")
        with (
            patch("strata.preflight.shutil.which", return_value="/usr/bin/git"),
            patch("strata.preflight.os.access", return_value=True),
            patch("strata.preflight.socket.socket") as mock_sock_cls,
        ):
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            checks = run_start_preflight(port=8000, db_path=db_path)

        assert len(checks) == 5

    def test_check_names(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_start_preflight includes Python, git, write perms, port, and API key checks."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("STRATA_ANTHROPIC_API_KEY", raising=False)
        db_path = str(tmp_path / "strata.db")
        with (
            patch("strata.preflight.shutil.which", return_value="/usr/bin/git"),
            patch("strata.preflight.os.access", return_value=True),
            patch("strata.preflight.socket.socket") as mock_sock_cls,
        ):
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            checks = run_start_preflight(port=8000, db_path=db_path)

        names = [c.name for c in checks]
        assert any("Python" in n for n in names)
        assert any("git" in n for n in names)
        assert any("write" in n for n in names)
        assert any("port" in n for n in names)
        assert any("ANTHROPIC_API_KEY" in n for n in names)

    def test_no_claude_check(self, tmp_path: Path) -> None:
        """run_start_preflight does NOT include a claude CLI check."""
        db_path = str(tmp_path / "strata.db")
        with (
            patch("strata.preflight.shutil.which", return_value="/usr/bin/git"),
            patch("strata.preflight.os.access", return_value=True),
            patch("strata.preflight.socket.socket") as mock_sock_cls,
        ):
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            checks = run_start_preflight(port=8000, db_path=db_path)

        names = [c.name for c in checks]
        assert not any("claude" in n.lower() for n in names)


# ---------------------------------------------------------------------------
# run_launch_preflight — check set
# ---------------------------------------------------------------------------


class TestRunLaunchPreflight:
    def test_returns_three_checks(self) -> None:
        """run_launch_preflight returns exactly 3 checks."""
        with patch("strata.preflight.shutil.which", return_value="/usr/bin/git"):
            checks = run_launch_preflight()
        assert len(checks) == 3

    def test_check_names(self) -> None:
        """run_launch_preflight includes Python, git, and claude checks."""
        with patch("strata.preflight.shutil.which", return_value="/usr/local/bin/claude"):
            checks = run_launch_preflight()

        names = [c.name for c in checks]
        assert any("Python" in n for n in names)
        assert any("git" in n for n in names)
        assert any("claude" in n.lower() for n in names)

    def test_no_port_or_api_key_check(self) -> None:
        """run_launch_preflight does NOT include port or ANTHROPIC_API_KEY checks."""
        with patch("strata.preflight.shutil.which", return_value="/usr/bin/git"):
            checks = run_launch_preflight()

        names = [c.name for c in checks]
        assert not any("port" in n for n in names)
        assert not any("ANTHROPIC_API_KEY" in n for n in names)


# ---------------------------------------------------------------------------
# _run_preflight runner — exit code behaviour
# ---------------------------------------------------------------------------


class TestRunPreflightRunner:
    def test_all_pass_returns_0(self, capsys: pytest.CaptureFixture[str]) -> None:
        """All-passing checks → runner returns 0."""
        checks = [_pass("Python"), _pass("git"), _pass("port")]
        rc = _run_preflight(checks)
        assert rc == 0

    def test_hard_fail_returns_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A single hard failure → runner returns 1."""
        checks = [_pass("Python"), _hard_fail("port")]
        rc = _run_preflight(checks)
        assert rc == 1

    def test_soft_fail_only_returns_0(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Soft failures only → runner returns 0 (warnings, not fatal)."""
        checks = [_pass("Python"), _soft_fail("git"), _soft_fail("ANTHROPIC_API_KEY")]
        rc = _run_preflight(checks)
        assert rc == 0

    def test_hard_fail_printed_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Hard failure message appears on stderr."""
        rc = _run_preflight([_hard_fail("port 8000")])
        err = capsys.readouterr().err
        assert "port 8000" in err
        assert rc == 1

    def test_soft_fail_printed_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Soft failure message appears on stderr."""
        _run_preflight([_soft_fail("git")])
        err = capsys.readouterr().err
        assert "git" in err

    def test_pass_printed_to_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Passing check message appears on stdout."""
        _run_preflight([_pass("Python")])
        out = capsys.readouterr().out
        assert "Python" in out

    def test_multiple_hard_failures_returns_1(self) -> None:
        """Multiple hard failures still return 1 (not 2)."""
        checks = [_hard_fail("port"), _hard_fail("write perms")]
        rc = _run_preflight(checks)
        assert rc == 1


# ---------------------------------------------------------------------------
# cmd_start integration
# ---------------------------------------------------------------------------


class TestCmdStartPreflight:
    def _good_preflight(self) -> list[Check]:
        """A set of fully-passing checks."""
        return [
            Check("Python ≥ 3.11", "hard", True, "Python 3.11.5"),
            Check("git on PATH", "soft", True, "git found"),
            Check("write perms on data directory", "hard", True, "writable"),
            Check("port 8000 available", "hard", True, "port 8000 is free"),
            Check("ANTHROPIC_API_KEY", "soft", True, "set"),
        ]

    def test_failing_preflight_returns_1_and_no_migrations(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A hard preflight failure → returns 1 and run_migrations is NOT called."""
        db_path = str(tmp_path / "strata.db")
        fleet_yaml_path = str(tmp_path / "fleet.yaml")
        failing_check = Check("port 8000 available", "hard", False, "Port 8000 is already in use.")

        with (
            patch("strata.__main__.run_start_preflight", return_value=[failing_check]),
            patch("strata.migrator.run_migrations", return_value=[]) as mock_migrate,
            patch("uvicorn.run"),
            patch("strata.__main__._fleet_config_default", return_value=fleet_yaml_path),
            patch("strata.__main__._db_path_default", return_value=db_path),
        ):
            rc = main(["start"])

        assert rc == 1
        mock_migrate.assert_not_called()
        err = capsys.readouterr().err
        assert "Port 8000" in err

    def test_passing_preflight_proceeds_to_migrations(
        self,
        tmp_path: Path,
    ) -> None:
        """All-passing preflight → run_migrations is called."""
        db_path = str(tmp_path / "strata.db")
        fleet_yaml_path = str(tmp_path / "fleet.yaml")

        with (
            patch("strata.__main__.run_start_preflight", return_value=self._good_preflight()),
            patch("strata.migrator.run_migrations", return_value=[]) as mock_migrate,
            patch("uvicorn.run"),
            patch("strata.__main__._fleet_config_default", return_value=fleet_yaml_path),
            patch("strata.__main__._db_path_default", return_value=db_path),
        ):
            rc = main(["start"])

        assert rc == 0
        mock_migrate.assert_called_once()

    def test_skip_preflight_bypasses_checks(
        self,
        tmp_path: Path,
    ) -> None:
        """--skip-preflight bypasses preflight entirely (run_start_preflight not called)."""
        db_path = str(tmp_path / "strata.db")
        fleet_yaml_path = str(tmp_path / "fleet.yaml")

        with (
            patch("strata.__main__.run_start_preflight") as mock_preflight,
            patch("strata.migrator.run_migrations", return_value=[]),
            patch("uvicorn.run"),
            patch("strata.__main__._fleet_config_default", return_value=fleet_yaml_path),
            patch("strata.__main__._db_path_default", return_value=db_path),
        ):
            rc = main(["start", "--skip-preflight"])

        assert rc == 0
        mock_preflight.assert_not_called()

    def test_soft_only_preflight_still_proceeds(
        self,
        tmp_path: Path,
    ) -> None:
        """Soft-only failures → run_migrations is still called (non-fatal)."""
        db_path = str(tmp_path / "strata.db")
        fleet_yaml_path = str(tmp_path / "fleet.yaml")
        soft_checks = [
            Check("Python ≥ 3.11", "hard", True, "Python 3.11.5"),
            Check("git on PATH", "soft", False, "git not found"),  # soft fail
            Check("write perms on data directory", "hard", True, "writable"),
            Check("port 8000 available", "hard", True, "port 8000 is free"),
            Check("ANTHROPIC_API_KEY", "soft", False, "key not set"),  # soft fail
        ]

        with (
            patch("strata.__main__.run_start_preflight", return_value=soft_checks),
            patch("strata.migrator.run_migrations", return_value=[]) as mock_migrate,
            patch("uvicorn.run"),
            patch("strata.__main__._fleet_config_default", return_value=fleet_yaml_path),
            patch("strata.__main__._db_path_default", return_value=db_path),
        ):
            rc = main(["start"])

        assert rc == 0
        mock_migrate.assert_called_once()


# ---------------------------------------------------------------------------
# cmd_launch integration
# ---------------------------------------------------------------------------


_SCOPE_DEFAULT_ONLY: dict = {
    "id": "g_arch",
    "name": "Architect",
    "stratum_id": "L1",
    "status": "active",
    "default_skill": "code-writer",
    "permitted_skills": None,
}


def _write_launch_fleet(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write a one-scope fleet.yaml and point STRATA_FLEET_CONFIG at it."""
    from strata.settings import get_settings

    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(
        "strata:\n  - id: L1\n    name: Function\n    ordinal: 1\n"
        "scopes:\n  - id: g_arch\n    name: Architect\n    stratum_id: L1\n"
        "    default_skill: code-writer\n"
        "edges: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("STRATA_FLEET_CONFIG", str(fleet))
    get_settings.cache_clear()


@pytest.fixture
def _settings_cache_guard():
    """Clear the Settings cache before and after (env-var isolation)."""
    from strata.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestCmdLaunchPreflight:
    def test_failing_preflight_and_missing_fleet_report_in_one_pass(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        _settings_cache_guard,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Hard preflight failure + missing fleet → BOTH reported in one run (#45)."""
        from strata.settings import get_settings

        hard_fail = Check("Python ≥ 3.11", "hard", False, "Python 3.10 too old.")
        monkeypatch.setenv("STRATA_FLEET_CONFIG", str(tmp_path / "absent.yaml"))
        get_settings.cache_clear()

        with (
            patch("strata.__main__.run_launch_preflight", return_value=[hard_fail]),
            patch("strata.__main__.exec_claude") as mock_exec,
        ):
            rc = main(["launch", "g_arch"])

        assert rc == 1
        mock_exec.assert_not_called()
        err = capsys.readouterr().err
        assert "Python 3.10 too old." in err
        assert "No fleet config found" in err  # reported in the SAME run

    def test_skip_preflight_bypasses_checks(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        _settings_cache_guard,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """--skip-preflight bypasses preflight entirely for launch."""
        _write_launch_fleet(tmp_path, monkeypatch)
        with (
            patch("strata.__main__.run_launch_preflight") as mock_preflight,
            patch("strata.__main__.exec_claude", return_value=0),
            patch("strata.__main__._run_manager_refresh"),
        ):
            rc = main(["launch", "g_arch", "--skip-preflight"])

        assert rc == 0
        mock_preflight.assert_not_called()

    def test_passing_preflight_proceeds_to_exec(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        _settings_cache_guard,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """All-passing launch preflight → fleet loaded from disk, claude exec'd."""
        good_checks = [
            Check("Python ≥ 3.11", "hard", True, "Python 3.11.5"),
            Check("git on PATH", "soft", True, "git found"),
            Check("claude CLI on PATH", "soft", True, "claude found"),
        ]
        _write_launch_fleet(tmp_path, monkeypatch)
        with (
            patch("strata.__main__.run_launch_preflight", return_value=good_checks),
            patch("strata.__main__.exec_claude", return_value=0) as mock_exec,
            patch("strata.__main__._run_manager_refresh"),
        ):
            rc = main(["launch", "g_arch"])

        assert rc == 0
        mock_exec.assert_called_once()
