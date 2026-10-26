"""A session's confirmed binding, kept across an MCP reconnect.

``/mcp reconnect`` restarts the ``strata-mcp`` process but not the harness, so the module globals
that held the binding are gone while the session is the same. This module writes the binding the
user confirmed to ``<sessions_dir>/<session_id>.binding`` (not ``*.json``: the session-state scans
read every ``*.json`` there) and restores it on the next start.

The key is the server-derived session id AND the harness identity (:func:`harness_token`, the
harness process's pid with its start time). Nothing the agent can set enters it. A restore is no
more powerful than ``strata_bind``: the caller runs the persisted scope/skill through the same
validation, and a config-class failure blocks it. Windows has no cheap start time, so its token is
empty and both this and session continuity stay off.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: Binding files untouched this long are deleted at startup (their harness is long gone).
STALE_AFTER_SECONDS = 30 * 24 * 3600

BINDING_SUFFIX = ".binding"


def harness_token(pid: int | None = None) -> str:
    """``"<pid>:<start time>"`` of the harness process, or ``""`` when the start time cannot be
    read (then nothing is persisted or restored). *pid* defaults to this process's parent."""
    if os.name == "nt":
        return ""
    pid = os.getppid() if pid is None else pid
    started = _start_time(pid)
    return f"{pid}:{started}" if started else ""


def _start_time(pid: int) -> str:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # field 22 (starttime, in clock ticks since boot); the comm field may hold spaces/parens,
        # so count from the LAST ')'.
        return stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    try:
        out = subprocess.run(  # noqa: S603
            ["ps", "-o", "lstart=", "-p", str(pid)],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return out


def binding_path(sessions_dir: str | Path, session_id: str) -> Path:
    return Path(sessions_dir) / f"{session_id}{BINDING_SUFFIX}"


def write_binding(
    sessions_dir: str | Path,
    session_id: str,
    token: str,
    scope_id: str,
    skill: str | None,
) -> bool:
    """Persist a confirmed binding (atomic, mode 0600). ``False`` when persistence is off for
    this session (empty token) or the write failed; a failed write never fails a bind."""
    if not token or not session_id or not scope_id:
        return False
    final = binding_path(sessions_dir, session_id)
    tmp = final.with_name(f"{final.name}.{os.getpid()}.tmp")
    body = {
        "session_id": session_id,
        "harness_token": token,
        "scope_id": scope_id,
        "skill": skill,
        "bound_at": datetime.now(UTC).isoformat(),
    }
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(body, indent=2))
        os.replace(tmp, final)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    return True


def _delete(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def delete_stale(sessions_dir: str | Path, *, now: float | None = None) -> int:
    """Delete binding files older than :data:`STALE_AFTER_SECONDS`; returns how many."""
    now = time.time() if now is None else now
    removed = 0
    try:
        entries = list(Path(sessions_dir).glob(f"*{BINDING_SUFFIX}"))
    except OSError:
        return 0
    for entry in entries:
        try:
            if now - entry.stat().st_mtime > STALE_AFTER_SECONDS:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


@dataclass(frozen=True)
class Restore:
    """What :func:`restore_binding` decided. ``status``: ``none`` (nothing to restore, or the
    file was not this session's and was ignored), ``restored``, ``invalid`` (the earlier binding
    no longer holds; ``message`` says why, the file is deleted) or ``blocked`` (a config-class
    failure; the file is kept)."""

    status: str
    scope_id: str = ""
    skill: str | None = None
    message: str = ""


NONE = Restore("none")


def restore_binding(
    sessions_dir: str | Path,
    session_id: str,
    token: str,
    validate: Callable[[str, str | None], tuple[str | None, list[str]]],
    *,
    config_errors: list[str] | None = None,
) -> Restore:
    """Read this session's persisted binding and validate it with *validate* (the same rule
    ``strata_bind`` applies: ``(scope, skill) -> (resolved skill, errors)``).

    The file is trusted only for its own consistency: its ``session_id`` equals the id this
    server derived AND the filename stem, and its ``harness_token`` equals the live *token*. A
    file that fails either is ignored (and deleted when it is provably not this harness's).
    """
    if not token:
        return NONE
    path = binding_path(sessions_dir, session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return NONE
    except (OSError, ValueError):
        _delete(path)
        return NONE
    if not isinstance(data, dict):
        _delete(path)
        return NONE
    stem = path.name[: -len(BINDING_SUFFIX)]
    if data.get("session_id") != session_id or session_id != stem:
        _delete(path)
        return NONE
    if data.get("harness_token") != token:
        _delete(path)  # a recycled pid, or another harness: never this session's binding
        return NONE
    scope_id = data.get("scope_id")
    skill = data.get("skill")
    if (
        not isinstance(scope_id, str)
        or not scope_id
        or not (skill is None or isinstance(skill, str))
    ):
        _delete(path)
        return NONE
    if config_errors:
        return Restore("blocked", scope_id, skill)
    resolved, errors = validate(scope_id, skill)
    if errors:
        _delete(path)
        return Restore(
            "invalid",
            scope_id,
            skill,
            f"your earlier binding to {scope_id!r} no longer holds: " + "; ".join(errors),
        )
    return Restore("restored", scope_id, resolved)
