"""The CI MCP round trip, on Linux and with ``fcntl`` simulated missing.

Both tests run ``scripts/ci_mcp_roundtrip.py`` as a subprocess. The script
is absent on the commit this branch fixes, so each test fails there.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ci_mcp_roundtrip.py"
_JUDGE_ENV = (
    "JUDGE_API_KEY",
    "STRATA_JUDGE_API_KEY",
    "ANTHROPIC_API_KEY",
    "STRATA_ANTHROPIC_API_KEY",
)


def _run(extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    assert _SCRIPT.is_file(), f"missing {_SCRIPT}"
    env = os.environ.copy()
    for name in _JUDGE_ENV:
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *(extra or [])],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )


def _steps(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("STEP ")]


def _require(result: subprocess.CompletedProcess[str], prefix: str) -> str:
    matches = [line for line in _steps(result.stdout) if line.startswith(prefix)]
    assert result.returncode == 0, result.stderr + "\n" + result.stdout
    assert matches, _steps(result.stdout)
    return matches[0]


def test_ci_mcp_roundtrip_on_posix() -> None:
    """Every STEP line, exit 0, and the POSIX fleet-change and export results."""
    result = _run()
    lines = _steps(result.stdout)
    assert result.returncode == 0, result.stderr + "\n" + result.stdout
    for number in range(1, 6):
        assert any(line.startswith(f"STEP {number} OK:") for line in lines), lines
    step3 = next(line for line in lines if line.startswith("STEP 3 "))
    step4 = next(line for line in lines if line.startswith("STEP 4 "))
    assert "applied" in step3
    assert "1 act" in step3
    assert step4.startswith("STEP 4 OK:")
    assert "refused" not in step4


def test_ci_mcp_roundtrip_windows_branch_when_fcntl_is_missing() -> None:
    """``--simulate-no-fcntl`` takes the Windows branch of steps 3 and 4."""
    result = _run(["--simulate-no-fcntl"])
    step3 = _require(result, "STEP 3 ")
    step4 = _require(result, "STEP 4 ")
    assert "refused" in step3
    assert "unchanged" in step3
    assert "0 acts" in step3
    assert "refused" in step4
    assert "no traceback" in step4
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout
