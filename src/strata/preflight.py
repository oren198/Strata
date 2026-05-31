"""Prerequisite checks for ``strata start`` and ``strata launch``.

Each check returns a :class:`Check` dataclass describing whether the
condition was met.  ``hard`` checks gate execution — a failure exits
non-zero.  ``soft`` checks emit a warning but do not stop the process.

Public API::

    checks = run_start_preflight(port=8000, db_path="./strata.db")
    checks = run_launch_preflight()

Vocabulary follows ``CONTEXT.md``: scope, fleet, session, skill, etc.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class Check:
    """Result of a single preflight check."""

    name: str
    kind: Literal["hard", "soft"]
    passed: bool
    message: str  # actionable on failure; informational on pass


# ---------------------------------------------------------------------------
# Individual check implementations
# ---------------------------------------------------------------------------


def _check_python_version() -> Check:
    """Python ≥ 3.11 is required by the strata package."""
    ver = sys.version_info
    major, minor, micro = ver[0], ver[1], ver[2]
    if (major, minor) < (3, 11):
        return Check(
            name="Python ≥ 3.11",
            kind="hard",
            passed=False,
            message=(
                f"Python 3.11+ required; found {major}.{minor}. "
                "Upgrade or use a compatible interpreter."
            ),
        )
    return Check(
        name="Python ≥ 3.11",
        kind="hard",
        passed=True,
        message=f"Python {major}.{minor}.{micro}",
    )


def _check_git_on_path() -> Check:
    """git must be on PATH for strata register project-root detection."""
    if shutil.which("git") is None:
        return Check(
            name="git on PATH",
            kind="soft",
            passed=False,
            message=(
                "git not found on PATH. "
                "Install git — it is needed for strata register project-root detection."
            ),
        )
    return Check(
        name="git on PATH",
        kind="soft",
        passed=True,
        message="git found",
    )


def _check_claude_on_path() -> Check:
    """claude CLI must be on PATH for strata launch."""
    if shutil.which("claude") is None:
        return Check(
            name="claude CLI on PATH",
            kind="soft",
            passed=False,
            message=(
                "claude not found on PATH. "
                "Install Claude Code and ensure it is on your PATH — "
                "it is needed for strata launch."
            ),
        )
    return Check(
        name="claude CLI on PATH",
        kind="soft",
        passed=True,
        message="claude found",
    )


def _check_write_perms(db_path: str) -> Check:
    """The parent directory of db_path must be writable."""
    parent = Path(db_path).parent
    # Resolve to an existing ancestor so os.access works even if db_path
    # does not exist yet (e.g. fresh install).
    existing = parent
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent

    if not os.access(existing, os.W_OK):
        return Check(
            name="write perms on data directory",
            kind="hard",
            passed=False,
            message=(
                f"No write permission on {parent}. "
                "Ensure the process has write access to that directory."
            ),
        )
    return Check(
        name="write perms on data directory",
        kind="hard",
        passed=True,
        message=f"{parent} is writable",
    )


def _check_port_available(port: int) -> Check:
    """The TCP port must be free on 127.0.0.1."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        sock.bind(("127.0.0.1", port))
        sock.close()
        return Check(
            name=f"port {port} available",
            kind="hard",
            passed=True,
            message=f"port {port} is free",
        )
    except OSError:
        # Best-effort: try to name the PID holding the port via lsof.
        extra = _lsof_port_info(port)
        return Check(
            name=f"port {port} available",
            kind="hard",
            passed=False,
            message=(
                f"Port {port} is already in use.{extra} "
                "Stop the process using that port or choose a different --port."
            ),
        )
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def _lsof_port_info(port: int) -> str:
    """Return a human-readable string naming the process on *port*, or ''."""
    if shutil.which("lsof") is None:
        return ""
    try:
        import subprocess

        result = subprocess.run(
            ["lsof", "-t", "-i", f"TCP:{port}", "-s", "TCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        pid = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if not pid:
            return ""
        # Try to get the process name.
        proc_result = subprocess.run(
            ["ps", "-p", pid, "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=3,
        )
        proc_name = proc_result.stdout.strip() or pid
        return f" (held by {proc_name} PID {pid})"
    except Exception:  # noqa: BLE001
        return ""


def _check_anthropic_api_key() -> Check:
    """ANTHROPIC_API_KEY (or STRATA_ANTHROPIC_API_KEY) should be set."""
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("STRATA_ANTHROPIC_API_KEY")
    if not key:
        return Check(
            name="ANTHROPIC_API_KEY",
            kind="soft",
            passed=False,
            message=(
                "ANTHROPIC_API_KEY is not set. "
                "The scope-manager will not be able to judge contributions without it. "
                "Set the variable before running strata start."
            ),
        )
    return Check(
        name="ANTHROPIC_API_KEY",
        kind="soft",
        passed=True,
        message="ANTHROPIC_API_KEY is set",
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def run_start_preflight(*, port: int, db_path: str) -> list[Check]:
    """Run prerequisite checks for ``strata start``.

    Checks: Python ≥ 3.11, git on PATH, write perms on the db_path parent
    directory, port availability, and ANTHROPIC_API_KEY set.
    """
    return [
        _check_python_version(),
        _check_git_on_path(),
        _check_write_perms(db_path),
        _check_port_available(port),
        _check_anthropic_api_key(),
    ]


def run_launch_preflight() -> list[Check]:
    """Run prerequisite checks for ``strata launch``.

    Checks: Python ≥ 3.11, git on PATH, claude CLI on PATH.
    No port check (launch does not bind) and no API key check (the
    refresh logic in Phase 1 already handles that).
    """
    return [
        _check_python_version(),
        _check_git_on_path(),
        _check_claude_on_path(),
    ]
