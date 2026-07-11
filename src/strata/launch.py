"""Pure, testable logic for ``strata launch``.

Factored out of ``__main__`` so skill resolution, ``.strata-role`` discovery,
and session-ID generation can be unit-tested without spawning a process.

Vocabulary follows CONTEXT.md verbatim: scope, skill, session, stratum, fleet.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class ScopeData(TypedDict):
    """Minimal shape of a scope dict returned by GET /scopes."""

    id: str
    name: str
    stratum_id: str
    status: str
    default_skill: str | None
    permitted_skills: list[str] | None


class RoleBinding(TypedDict):
    """A resolved (scope_id, skill) pair ready for launch."""

    scope_id: str
    skill: str


# ---------------------------------------------------------------------------
# Skill resolution (ADR 0002 resolution table)
# ---------------------------------------------------------------------------


class SkillResolutionError(Exception):
    """Raised when skill cannot be resolved from the scope's declaration."""

    pass


def resolve_skill(
    scope: ScopeData,
    requested_skill: str | None,
    *,
    interactive: bool,
) -> str:
    """Resolve the skill for a session against *scope*, following the ADR 0002 table.

    Args:
        scope:           The target scope dict from GET /scopes.
        requested_skill: Value of --skill flag, or None.
        interactive:     True when sys.stdin.isatty() — allows prompting.

    Returns:
        The resolved skill name.

    Raises:
        SkillResolutionError: When the scope declares no skills (neither row),
            or when the user is not in an interactive context and skill cannot
            be determined unambiguously, or when --skill is not in
            permitted_skills.
    """
    default = scope.get("default_skill")
    permitted = scope.get("permitted_skills")
    scope_id = scope["id"]

    # --skill override: validate against permitted_skills if that list is set.
    if requested_skill is not None:
        if permitted is not None and requested_skill not in permitted:
            raise SkillResolutionError(
                f"Skill {requested_skill!r} is not permitted for scope {scope_id!r}. "
                f"Permitted skills: {permitted}"
            )
        # If permitted is None (unset), any explicit --skill is accepted.
        return requested_skill

    # Row 1 & 2: default_skill set → use it (permitted may or may not be set).
    if default is not None:
        return default

    # Row 3: no default, permitted list provided → need user choice.
    if permitted is not None:
        if not interactive:
            raise SkillResolutionError(
                f"Scope {scope_id!r} has no default_skill. "
                f"Pass --skill <skill> (permitted: {permitted})"
            )
        return _prompt_skill(scope_id, permitted)

    # Row 4: neither set → error.
    raise SkillResolutionError(f"Scope {scope_id!r} declares no skills.")


def _prompt_skill(scope_id: str, permitted: list[str]) -> str:
    """Interactively prompt the user to pick a skill from *permitted*."""
    print(f"Scope {scope_id!r} permits the following skills:")
    for i, skill in enumerate(permitted, start=1):
        print(f"  {i}. {skill}")
    raw = input("Pick a skill (number or name): ").strip()
    # Accept a number or a name.
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(permitted):
            return permitted[idx]
        raise SkillResolutionError(
            f"Invalid choice {raw!r}. Enter a number 1–{len(permitted)} or a skill name."
        )
    if raw in permitted:
        return raw
    raise SkillResolutionError(
        f"Invalid skill {raw!r}. Permitted skills for scope {scope_id!r}: {permitted}"
    )


# ---------------------------------------------------------------------------
# .strata-role discovery
# ---------------------------------------------------------------------------


class StrataRoleParseError(Exception):
    """Raised when a .strata-role file is malformed."""

    pass


def find_strata_role(start: Path) -> Path | None:
    """Walk from *start* upward to the git root (or filesystem root) looking for
    a ``.strata-role`` file.  Returns the first one found, or None.
    """
    current = start.resolve()
    while True:
        candidate = current / ".strata-role"
        if candidate.is_file():
            return candidate
        # Stop at git root or filesystem root.
        if (current / ".git").exists() or current.parent == current:
            return None
        current = current.parent


def parse_strata_role(path: Path) -> tuple[str, str | None]:
    """Parse a ``.strata-role`` TOML file.

    Returns:
        A ``(scope_id, skill_or_None)`` tuple.

    Raises:
        StrataRoleParseError: If the file is not valid TOML or lacks ``scope``.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise StrataRoleParseError(f".strata-role at {path} is not valid TOML: {exc}") from exc

    scope_id = data.get("scope")
    if not scope_id or not isinstance(scope_id, str):
        raise StrataRoleParseError(f".strata-role at {path} is missing required field 'scope'.")
    skill: str | None = data.get("skill") or None
    return scope_id, skill


# ---------------------------------------------------------------------------
# Session ID generation
# ---------------------------------------------------------------------------


def make_session_id(scope_id: str, skill: str, *, ts: datetime | None = None) -> str:
    """Generate a session ID in the pinned format from ADR 0003.

    Format: ``sess_<scope>_<skill>_<YYYYMMDD-HHMMSS>``

    Args:
        scope_id: The target scope's ID.
        skill:    The resolved skill name.
        ts:       Optional UTC datetime to use (defaults to now in UTC).

    Returns:
        A session ID string, e.g. ``sess_g_arch_code-writer_20260527-134215``.
    """
    if ts is None:
        ts = datetime.now(tz=UTC)
    timestamp = ts.strftime("%Y%m%d-%H%M%S")
    return f"sess_{scope_id}_{skill}_{timestamp}"


# ---------------------------------------------------------------------------
# Interactive scope picker
# ---------------------------------------------------------------------------


def prompt_scope(scopes: list[ScopeData]) -> ScopeData:
    """Present an interactive picker and return the chosen scope.

    Raises:
        SystemExit: If stdin is not a TTY (should be guarded by caller).
    """
    print("Active scopes:")
    header = f"  {'#':>3}  {'id':<14}  {'stratum':<8}  name"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for i, sc in enumerate(scopes, start=1):
        default_skill = sc.get("default_skill") or "(none)"
        print(f"  {i:>3}  {sc['id']:<14}  {sc['stratum_id']:<8}  {sc['name']}  [{default_skill}]")
    raw = input("Pick a scope (number or id): ").strip()
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(scopes):
            return scopes[idx]
        raise SystemExit(f"Invalid choice {raw!r}. Enter a number 1–{len(scopes)} or a scope id.")
    for sc in scopes:
        if sc["id"] == raw:
            return sc
    valid = ", ".join(sc["id"] for sc in scopes)
    raise SystemExit(f"Unknown scope {raw!r}. Valid scope IDs: {valid}")


# ---------------------------------------------------------------------------
# TTY helper (separated for easy patching in tests)
# ---------------------------------------------------------------------------


def is_interactive() -> bool:
    """Return True when stdin is a TTY."""
    return sys.stdin.isatty()


# ---------------------------------------------------------------------------
# Handoff to claude (platform-specific)
# ---------------------------------------------------------------------------


def exec_claude(env: dict[str, str]) -> int:
    """Hand the console off to the ``claude`` binary with *env* applied.

    POSIX replaces this process image via :func:`os.execvpe` — a true handoff,
    so Ctrl-C, the exit code, and tty semantics belong to Claude Code directly.
    It never returns on success; a missing binary raises ``FileNotFoundError``
    for the caller to translate.

    Windows has no real ``exec``: there :func:`os.execvpe` spawns a *new*
    process and the parent exits immediately, which loses the child's exit code
    and orphans it against the console. So on Windows we spawn ``claude`` as a
    console-sharing child, ignore SIGINT in the launcher while it runs (the
    console delivers Ctrl-C to *both* processes; the child owns the interrupt,
    the launcher must survive to reap it), and return the child's exit code.

    Args:
        env: The full child environment (already carries STRATA_AGENT_*).

    Returns:
        The child's exit code on Windows. On POSIX it does not return on
        success — the process image is replaced.

    Raises:
        FileNotFoundError: When ``claude`` cannot be found on PATH. Raised on
            both paths for parity — POSIX from ``execvpe``, Windows from
            ``shutil.which`` — so the caller reports one message.
    """
    claude_bin = "claude"

    if os.name != "nt":
        # POSIX: true process replacement. execvpe resolves claude on PATH and
        # raises FileNotFoundError when it is absent.
        os.execvpe(claude_bin, [claude_bin], env)
        return 0  # pragma: no cover — execvpe never returns on success.

    # Windows: resolve via PATHEXT (claude may be claude.exe or a .cmd shim);
    # a bare Popen(["claude"]) would miss that resolution.
    resolved = shutil.which(claude_bin)
    if resolved is None:
        raise FileNotFoundError(claude_bin)
    argv = _windows_claude_argv(resolved)

    # The child shares this console, so a Ctrl-C reaches BOTH processes. Ignore
    # SIGINT in the launcher for the child's lifetime so the child handles the
    # interrupt and the launcher survives to reap it and report its real exit
    # code; restore the handler afterward.
    previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        child = subprocess.Popen(argv, env=env)
        return child.wait()
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def _windows_claude_argv(resolved: str) -> list[str]:
    """Build the spawn argv for a resolved Windows ``claude`` launcher.

    ``shutil.which`` may return a ``.cmd``/``.bat`` shim (the npm-installed
    form). ``CreateProcess`` — and therefore ``subprocess.Popen`` with a list —
    cannot execute a batch file directly (WinError 193), so route those through
    ``cmd.exe /c``. A real ``.exe`` is spawned directly.
    """
    if os.path.splitext(resolved)[1].lower() in (".cmd", ".bat"):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/c", resolved]
    return [resolved]
