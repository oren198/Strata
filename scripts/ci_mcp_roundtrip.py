#!/usr/bin/env python3
"""Register, MCP stdio round trip, fleet-change, and export-fleet — every OS.

Standard library, the ``mcp`` client this package already depends on, and
Strata itself. No judge key and no network. Exits non-zero on the first
failed step and names that step. Each successful step prints one line:
``STEP <n> OK: <what>``.

POSIX versus Windows is ``strata.fleet_config.fcntl is None``, not
``sys.platform``. ``--simulate-no-fcntl`` is for
``tests/test_ci_mcp_roundtrip.py`` only: it re-execs this process so
``import fcntl`` fails the way it does on Windows, then the same check
selects the Windows branch. CI does not pass the flag.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import TextIO

_JUDGE_ENV = (
    "JUDGE_API_KEY",
    "STRATA_JUDGE_API_KEY",
    "ANTHROPIC_API_KEY",
    "STRATA_ANTHROPIC_API_KEY",
)
_SIMULATE_FLAG = "--simulate-no-fcntl"
_SIMULATE_ENV = "STRATA_SIMULATE_NO_FCNTL"
_SIMULATE_DIR_ENV = "STRATA_SIMULATE_DIR"
_CONTRIBUTION_ID = re.compile(r"\bc_[0-9a-f]{16}\b")

# Installed on PYTHONPATH before re-exec. Gated on the env var so a leftover
# path does nothing. Blocking ``import fcntl`` is how fleet_config records
# ``fcntl = None`` on Windows; the script then reads that attribute.
_SITECUSTOMIZE = """\
import os
import sys

if os.environ.get("STRATA_SIMULATE_NO_FCNTL") == "1":
    import importlib.machinery

    class _FcntlUnavailable:
        def find_spec(self, fullname, path, target=None):
            if fullname != "fcntl":
                return None
            return importlib.machinery.ModuleSpec(fullname, self)

        def create_module(self, spec):
            raise ImportError("simulated: fcntl is not available")

        def exec_module(self, module):
            raise ImportError("simulated: fcntl is not available")

    sys.meta_path.insert(0, _FcntlUnavailable())
"""


class StepFailure(Exception):
    """A named step failed. ``number`` is the step the process exits with."""

    def __init__(self, number: int, detail: str) -> None:
        super().__init__(detail)
        self.number = number
        self.detail = detail


class _Roundtrip:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="strata-ci-roundtrip-"))
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.home.mkdir()
        self.project.mkdir()
        self.server_pids: set[int] = set()
        self._err_file: TextIO | None = None
        self._client_thread: threading.Thread | None = None
        self._close_session = threading.Event()

    def cleanup(self) -> None:
        # Unblock the client thread so it can leave the MCP context in the
        # same task that entered it, then drop temp dirs. Never traceback
        # from here: the step message, if any, was already printed.
        self._close_session.set()
        thread = self._client_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=20)
        if self._err_file is not None:
            self._err_file.close()
            self._err_file = None
        shutil.rmtree(self.root, ignore_errors=True)
        sim = os.environ.get(_SIMULATE_DIR_ENV)
        if sim:
            shutil.rmtree(sim, ignore_errors=True)


def _fail(number: int, detail: str) -> None:
    raise StepFailure(number, detail)


def _ok(number: int, detail: str) -> None:
    print(f"STEP {number} OK: {detail}", flush=True)


def _clip(text: str, limit: int = 800) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _strip_judge_keys() -> None:
    for name in _JUDGE_ENV:
        os.environ.pop(name, None)


def _maybe_reexec() -> None:
    """Re-exec once so ``fcntl`` is missing before Strata is imported."""
    if _SIMULATE_FLAG not in sys.argv:
        return
    if os.environ.get(_SIMULATE_ENV) == "1" and os.environ.get(_SIMULATE_DIR_ENV):
        return
    sim = Path(tempfile.mkdtemp(prefix="strata-nofcntl-"))
    (sim / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    env = os.environ.copy()
    for name in _JUDGE_ENV:
        env.pop(name, None)
    env[_SIMULATE_ENV] = "1"
    env[_SIMULATE_DIR_ENV] = str(sim)
    previous = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(sim) if not previous else str(sim) + os.pathsep + previous
    os.execve(sys.executable, [sys.executable, *sys.argv], env)


def _windows_like() -> bool:
    from strata.fleet_config import fcntl

    return fcntl is None


def _executable(name: str, step: int) -> str:
    found = shutil.which(name)
    if found:
        return found
    # Do not resolve(): a venv's python is a symlink into the system install,
    # and the console scripts sit next to that symlink.
    bindir = Path(sys.executable).absolute().parent
    for candidate in (bindir / name, bindir / f"{name}.exe"):
        if candidate.is_file():
            return str(candidate)
    raise StepFailure(step, f"{name} is not on PATH and not beside {sys.executable}")


def _child_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in _JUDGE_ENV:
        env.pop(name, None)
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(home / "codex")
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
    return env


def _run(
    number: int, argv: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise StepFailure(number, f"{argv[0]} timed out: {_clip(str(exc))}") from exc
    except OSError as exc:
        raise StepFailure(number, f"could not run {argv[0]}: {exc}") from exc


def _direct_child_pids() -> set[int]:
    me = os.getpid()
    if Path("/proc").is_dir():
        found: set[int] = set()
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                raw = Path(f"/proc/{entry}/stat").read_text(encoding="utf-8")
            except OSError:
                continue
            fields = raw[raw.rfind(")") + 2 :].split()
            if len(fields) >= 2 and int(fields[1]) == me:
                found.add(int(entry))
        return found
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter "
            f"'ParentProcessId={me}' | ForEach-Object {{ $_.ProcessId }}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return {int(line) for line in completed.stdout.splitlines() if line.strip().isdigit()}


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            kernel.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _act_count(db_path: Path) -> int:
    if not db_path.is_file():
        return 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'fleet_structure_acts'"
        ).fetchone()
        if row is None:
            return 0
        count = conn.execute("SELECT COUNT(*) FROM fleet_structure_acts").fetchone()
        return int(count[0]) if count else 0
    finally:
        conn.close()


def _step_register(state: _Roundtrip, env: dict[str, str]) -> None:
    git = _run(1, ["git", "init", "-q"], cwd=state.project, env=env)
    if git.returncode != 0:
        _fail(1, f"git init exited {git.returncode}: {_clip(git.stderr)}")
    strata = _executable("strata", 1)
    registered = _run(
        1,
        [strata, "register", "--yes", "--harness", "claude-code", "."],
        cwd=state.project,
        env=env,
    )
    if registered.returncode != 0:
        _fail(
            1,
            f"strata register exited {registered.returncode}: {_clip(registered.stderr)}",
        )
    config = state.project / ".strata" / "config.toml"
    fleet = state.project / ".strata" / "fleet.yaml"
    if not config.is_file() or not fleet.is_file():
        _fail(1, f"missing config.toml={config.is_file()} fleet.yaml={fleet.is_file()}")
    from strata.fleet_config import FleetConfig

    try:
        FleetConfig.load(fleet)
    except Exception as exc:  # noqa: BLE001 — the step names whatever load raises
        _fail(1, f"fleet.yaml did not load: {type(exc).__name__}: {exc}")
    _ok(1, "registered; fleet.yaml loads")


def _server_env(state: _Roundtrip) -> dict[str, str]:
    """Env for ``strata-mcp``. The MCP client also inherits a safe default set.

    Judge keys are not in that default set and are not added here.
    """
    env = {
        "HOME": str(state.home),
        "STRATA_AGENT_SCOPE": "g_root",
        "STRATA_AGENT_SESSION_ID": "ci-roundtrip",
        "CODEX_HOME": str(state.home / "codex"),
    }
    if os.name == "nt":
        env["USERPROFILE"] = str(state.home)
    simulate = os.environ.get(_SIMULATE_DIR_ENV)
    if simulate:
        previous = os.environ.get("PYTHONPATH", "")
        env["PYTHONPATH"] = simulate if not previous else simulate + os.pathsep + previous
        env[_SIMULATE_ENV] = "1"
    leaked = [name for name in _JUDGE_ENV if env.get(name)]
    if leaked:
        _fail(2, f"refusing to start strata-mcp with judge key env {leaked}")
    return env


def _tool_text(result: object) -> tuple[bool, str, object | None]:
    is_error = bool(getattr(result, "isError", False))
    chunks: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            chunks.append(text)
    text = "\n".join(chunks)
    data = getattr(result, "structuredContent", None)
    if data is None and text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
    return is_error, text, data


def _scope_id_of(data: object) -> str | None:
    if isinstance(data, dict):
        scope = data.get("scope_id")
        if isinstance(scope, str):
            return scope
        nested = data.get("result")
        if isinstance(nested, dict) and isinstance(nested.get("scope_id"), str):
            return nested["scope_id"]
    return None


def _record_has_id(data: object, contribution_id: str) -> tuple[bool, str | None]:
    payload = data
    if (
        isinstance(payload, dict)
        and "contributions" not in payload
        and isinstance(payload.get("result"), dict)
    ):
        payload = payload["result"]
    if not isinstance(payload, dict):
        return False, None
    contributions = payload.get("contributions")
    if not isinstance(contributions, list):
        return False, None
    found = False
    for item in contributions:
        if isinstance(item, dict) and item.get("id") == contribution_id:
            found = True
            break
    if not found:
        return False, None
    state: str | None = None
    for item in payload.get("contribution_states") or []:
        if isinstance(item, dict) and item.get("contribution_id") == contribution_id:
            value = item.get("state")
            state = value if isinstance(value, str) else None
    for item in payload.get("judgments") or []:
        if isinstance(item, dict) and item.get("contribution_id") == contribution_id:
            return False, "judged"
    return True, state


async def _mcp_calls(session: object) -> str:
    call_tool = session.call_tool  # type: ignore[attr-defined]
    read_result = await call_tool("strata_read_perspective", {})
    is_error, text, data = _tool_text(read_result)
    if is_error or _scope_id_of(data) != "g_root":
        _fail(2, f"strata_read_perspective did not return scope g_root: {_clip(text)}")

    contributed = await call_tool(
        "strata_contribute",
        {
            "scope_id": "g_root",
            "content": "CI round trip: one context item, left unjudged.",
            "proposed_classification": "context",
        },
    )
    is_error, text, _data = _tool_text(contributed)
    match = _CONTRIBUTION_ID.search(text)
    if not is_error or match is None or "no verdict" not in text:
        _fail(2, f"strata_contribute did not record an unjudged outcome: {_clip(text)}")
    contribution_id = match.group(0)

    record = await call_tool("strata_read_scope_record", {"scope_id": "g_root"})
    is_error, record_text, record_data = _tool_text(record)
    present, judgment_state = _record_has_id(record_data, contribution_id)
    if is_error or not present or judgment_state == "judged":
        _fail(
            2,
            f"strata_read_scope_record missing unjudged {contribution_id}: {_clip(record_text)}",
        )
    state_label = judgment_state or "unjudged"
    return (
        f"read g_root; contributed {contribution_id} unjudged "
        f"(no verdict, {state_label}); record has {contribution_id}"
    )


async def _hold_session(
    state: _Roundtrip,
    ready: threading.Event,
    box: dict[str, object],
) -> None:
    """Own the stdio client until step 5 asks it to close.

    anyio's cancel scope has to be exited in the task that entered it, so
    this coroutine stays alive across the fleet and export steps and leaves
    the ``async with`` only after ``_close_session`` is set.
    """
    from mcp import ClientSession, StdioServerParameters, types
    from mcp.client.stdio import stdio_client

    err_path = state.root / "mcp-stderr.txt"
    state._err_file = err_path.open("w", encoding="utf-8")
    params = StdioServerParameters(
        command=_executable("strata-mcp", 2),
        args=[],
        env=_server_env(state),
        cwd=str(state.project),
    )
    before = _direct_child_pids()
    async with (
        stdio_client(params, errlog=state._err_file) as (read, write),
        ClientSession(
            read,
            write,
            client_info=types.Implementation(name="ci-roundtrip", version="0"),
        ) as session,
    ):
        await session.initialize()
        state.server_pids = _direct_child_pids() - before
        if not state.server_pids:
            raise StepFailure(2, "strata-mcp process did not appear as a child of this script")
        box["detail"] = await _mcp_calls(session)
        ready.set()
        await asyncio.to_thread(state._close_session.wait)


def _client_thread(
    state: _Roundtrip,
    ready: threading.Event,
    box: dict[str, object],
) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_hold_session(state, ready, box))
    except Exception as exc:  # noqa: BLE001 — carried back to the step that owns it
        box["error"] = exc
        ready.set()
    finally:
        loop.close()
        err = state._err_file
        state._err_file = None
        if err is not None:
            err.close()


def _server_stderr(state: _Roundtrip) -> str:
    path = state.root / "mcp-stderr.txt"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _step_mcp(state: _Roundtrip) -> None:
    ready = threading.Event()
    box: dict[str, object] = {}
    thread = threading.Thread(
        target=_client_thread,
        args=(state, ready, box),
        name="ci-mcp-client",
        daemon=True,
    )
    state._client_thread = thread
    thread.start()
    if not ready.wait(90):
        state._close_session.set()
        thread.join(timeout=20)
        _fail(2, f"MCP client did not become ready; server stderr: {_clip(_server_stderr(state))}")
    error = box.get("error")
    if error is not None:
        thread.join(timeout=20)
        if isinstance(error, StepFailure):
            detail = error.detail
        else:
            detail = f"{type(error).__name__}: {error}"
        _fail(2, f"{detail}; server stderr: {_clip(_server_stderr(state))}")
    _ok(2, str(box.get("detail", "")))


def _step_fleet_change(state: _Roundtrip, env: dict[str, str], *, windows: bool) -> None:
    from strata.fleet_config import FLEET_FILE_LOCK_UNAVAILABLE

    fleet = state.project / ".strata" / "fleet.yaml"
    before = fleet.read_bytes()
    db_path = state.project / ".strata" / "strata.db"
    strata = _executable("strata", 3)
    completed = _run(
        3,
        [
            strata,
            "fleet",
            "add-scope",
            "--id",
            "g_ci",
            "--name",
            "CI",
            "--stratum",
            "L0",
        ],
        cwd=state.project,
        env=env,
    )
    after = fleet.read_bytes()
    backups = sorted(path.name for path in fleet.parent.glob("fleet.yaml.bak*"))
    acts = _act_count(db_path)
    if windows:
        if completed.returncode == 0:
            _fail(3, f"fleet add-scope exited 0 where fcntl is missing: {_clip(completed.stdout)}")
        if completed.stderr.strip() != FLEET_FILE_LOCK_UNAVAILABLE:
            _fail(3, f"stderr was not the lock refusal: {_clip(completed.stderr)}")
        if after != before:
            _fail(3, "fleet.yaml changed despite the lock refusal")
        if backups:
            _fail(3, f"backup files written: {backups}")
        if acts != 0:
            _fail(3, f"expected 0 acts, found {acts}")
        _ok(3, "fleet add-scope refused; fleet.yaml unchanged; 0 acts")
        return
    if completed.returncode != 0:
        _fail(3, f"fleet add-scope exited {completed.returncode}: {_clip(completed.stderr)}")
    if "applied" not in completed.stdout:
        _fail(3, f"fleet add-scope did not apply: {_clip(completed.stdout)}")
    if acts != 1:
        _fail(3, f"expected 1 act, found {acts}")
    if after == before:
        _fail(3, "fleet add-scope exited 0 but fleet.yaml was unchanged")
    _ok(3, "fleet add-scope applied; 1 act")


def _v1_database(directory: Path) -> Path | None:
    """A database that still has the V1 fleet tables, or None if the SQL is absent."""
    import strata.fleet_config as fleet_config

    sql_path = Path(fleet_config.__file__).resolve().parent / "_migrations" / "0001_initial.sql"
    if not sql_path.is_file():
        return None
    db_path = directory / "v1.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(sql_path.read_text(encoding="utf-8"))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L0', 'Executive', 0)")
        conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L1', 'Function', 1)")
        conn.execute("INSERT INTO scopes (id, name, stratum_id) VALUES ('s_ceo', 'CEO', 'L0')")
        conn.execute(
            "INSERT INTO scopes (id, name, stratum_id) VALUES ('s_eng', 'Engineering', 'L1')"
        )
        conn.execute(
            "INSERT INTO edges (id, from_scope_id, to_scope_id) VALUES ('e1', 's_ceo', 's_eng')"
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _step_export(state: _Roundtrip, env: dict[str, str], *, windows: bool) -> None:
    from strata.fleet_config import FLEET_FILE_LOCK_UNAVAILABLE

    try:
        db_path = _v1_database(state.root)
    except (OSError, sqlite3.Error) as exc:
        _ok(4, f"skipped: no V1 db fixture ({type(exc).__name__}: {exc})")
        return
    if db_path is None:
        _ok(4, "skipped: no V1 db fixture (0001_initial.sql is not in this install)")
        return
    out = state.root / "export-fleet.yaml"
    original = b"already here\n"
    out.write_bytes(original)
    strata = _executable("strata", 4)
    completed = _run(
        4,
        [strata, "export-fleet", "--db", str(db_path), "--out", str(out), "--force"],
        cwd=state.project,
        env=env,
    )
    if windows:
        if completed.returncode != 1:
            _fail(4, f"export-fleet exited {completed.returncode}, expected 1")
        if completed.stderr.strip() != FLEET_FILE_LOCK_UNAVAILABLE:
            _fail(4, f"stderr was not the lock refusal: {_clip(completed.stderr)}")
        if "Traceback" in completed.stderr or "Traceback" in completed.stdout:
            _fail(4, "export-fleet printed a traceback")
        if out.read_bytes() != original:
            _fail(4, "export-fleet changed the existing file")
        _ok(4, "export-fleet refused; no traceback")
        return
    if completed.returncode != 0:
        _fail(4, f"export-fleet exited {completed.returncode}: {_clip(completed.stderr)}")
    if "Traceback" in completed.stderr:
        _fail(4, "export-fleet printed a traceback")
    if out.read_bytes() == original:
        _fail(4, "export-fleet left the existing file unchanged")
    _ok(4, "export-fleet replaced existing file")


def _step_cleanup(state: _Roundtrip) -> None:
    pids = set(state.server_pids)
    state._close_session.set()
    thread = state._client_thread
    if thread is not None:
        thread.join(timeout=30)
        if thread.is_alive():
            _fail(5, "MCP client did not exit")
    still = sorted(pid for pid in pids if _pid_alive(pid))
    if still:
        _fail(5, f"strata-mcp still running: {still}")
    shutil.rmtree(state.root, ignore_errors=True)
    sim_raw = os.environ.get(_SIMULATE_DIR_ENV)
    if sim_raw:
        shutil.rmtree(sim_raw, ignore_errors=True)
    leftovers = []
    if state.root.exists():
        leftovers.append(str(state.root))
    if sim_raw and Path(sim_raw).exists():
        leftovers.append(sim_raw)
    if leftovers:
        _fail(5, f"temp dirs remain: {leftovers}")
    _ok(5, "server exited; temp dirs removed")


def _run_steps(state: _Roundtrip) -> None:
    os.environ["HOME"] = str(state.home)
    if os.name == "nt":
        os.environ["USERPROFILE"] = str(state.home)
    env = _child_env(state.home)
    windows = _windows_like()
    _step_register(state, env)
    _step_mcp(state)
    _step_fleet_change(state, env, windows=windows)
    _step_export(state, env, windows=windows)
    _step_cleanup(state)


def main(argv: list[str] | None = None) -> int:
    _maybe_reexec()
    _strip_judge_keys()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        _SIMULATE_FLAG,
        action="store_true",
        help="Test-only: behave as if fcntl could not be imported.",
    )
    parser.parse_args(argv)
    state = _Roundtrip()
    try:
        _run_steps(state)
    except StepFailure as exc:
        print(f"STEP {exc.number} FAIL: {exc.detail}", file=sys.stderr)
        return exc.number
    finally:
        state.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
