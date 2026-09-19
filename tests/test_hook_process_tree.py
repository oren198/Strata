"""The Stop hook finds its session through REAL process trees (no mocked pids).

The MCP server keys a session by ``sess_auto_<harness pid>``. The hook must land
on the same id with no IPC, and the two harnesses spawn it differently:

* Codex runs the hook as a direct child of the harness process (verified live on
  codex-cli 0.153.4, TUI and ``codex exec``).
* Claude Code runs it as ``/bin/sh -c 'sh <script>'`` and that outer shell
  survives, so the hook's parent is the shell and the harness is its grandparent
  (found live in M3).

Each test builds that tree with real processes: a stand-in harness process, the
hook (the real ``strata freshness-hook``) underneath it, and a session record
seeded under the harness's actual pid.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

import strata
from strata.migrator import run_migrations
from strata.session_state import NUDGE_MIN_READS, SessionStateStore

_SRC = str(Path(strata.__file__).resolve().parent.parent)
_HOOK_SCRIPT = Path(strata.__file__).resolve().parent / "_hooks" / "strata-stop-hook"

# The stand-in harness runs whatever command it is given and exits with its code.
_HARNESS = "import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))"


def _project(root: Path) -> SessionStateStore:
    strata_dir = root / ".strata"
    strata_dir.mkdir()
    (strata_dir / "config.toml").write_text(
        'db = ".strata/strata.db"\n'
        'fleet_yaml = ".strata/fleet.yaml"\n'
        'summaries_dir = ".strata/summaries"\n',
        encoding="utf-8",
    )
    (strata_dir / "fleet.yaml").write_text(
        yaml.dump(
            {
                "strata": [{"id": "L0", "name": "root", "ordinal": 0}],
                "scopes": [{"id": "g_root", "name": "Root", "stratum_id": "L0"}],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    (strata_dir / "summaries").mkdir()
    run_migrations(str(strata_dir / "strata.db"))
    return SessionStateStore(strata_dir / "sessions")


def _env(root: Path, extra_path: str = "") -> dict[str, str]:
    env = {
        "PATH": (extra_path + os.pathsep if extra_path else "") + os.environ.get("PATH", ""),
        "PYTHONPATH": _SRC,
        "HOME": str(root),
    }
    return env  # no STRATA_AGENT_SESSION_ID: the hook must derive the session itself


def _run_tree(root: Path, harness_args: list[str], *, extra_path: str = "") -> tuple[int, str, int]:
    """Start the stand-in harness, seed a session under ITS pid, then feed the hook
    its stdin. Returns ``(harness pid, hook stdout, exit code)``."""
    store = SessionStateStore(root / ".strata" / "sessions")
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _HARNESS, *harness_args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(root, extra_path),
        cwd=str(root),
        text=True,
    )
    # The MCP server (a child of the harness) would have recorded this session.
    session_id = f"sess_auto_{proc.pid}"
    store.record_connect(session_id, harness="codex", pid=proc.pid)
    for _ in range(NUDGE_MIN_READS):
        store.record_read(session_id, "g_root")
    out, err = proc.communicate(
        json.dumps({"session_id": "x", "transcript_path": "/tmp/t", "stop_hook_active": False}),
        timeout=60,
    )
    assert proc.returncode == 0, err
    return proc.pid, out, proc.returncode


def test_the_hook_as_a_direct_child_of_the_harness_finds_its_session(tmp_path: Path) -> None:
    """Codex's tree: harness -> hook."""
    _project(tmp_path)

    pid, out, _ = _run_tree(tmp_path, [sys.executable, "-m", "strata", "freshness-hook"])

    assert json.loads(out)["decision"] == "block"
    state = SessionStateStore(tmp_path / ".strata" / "sessions").read(f"sess_auto_{pid}")
    assert state is not None and state.strict_blocked_at != ""


def test_the_hook_under_a_surviving_sh_c_wrapper_finds_its_session(tmp_path: Path) -> None:
    """Claude Code's tree: harness -> `/bin/sh -c 'sh <hook script>'` (the shell
    survives) -> the shipped strata-stop-hook script -> `strata freshness-hook`.
    The hook's parent is the shell, so only the ancestor walk reaches the session."""
    _project(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "strata"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m strata "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    # The trailing `exit $?` keeps the outer shell alive whatever /bin/sh is, which
    # is exactly the behaviour observed with Claude Code's hook launch.
    command = f'sh "{_HOOK_SCRIPT}"; exit $?'

    pid, out, _ = _run_tree(tmp_path, ["/bin/sh", "-c", command], extra_path=str(bin_dir))

    assert json.loads(out)["decision"] == "block"
    state = SessionStateStore(tmp_path / ".strata" / "sessions").read(f"sess_auto_{pid}")
    assert state is not None and state.strict_blocked_at != ""


def test_the_hook_does_not_attach_to_an_unrelated_session(tmp_path: Path) -> None:
    """A session record under some other pid is not this hook's: with no matching
    ancestor the hook stays silent instead of blocking a stranger."""
    store = _project(tmp_path)
    store.record_connect("sess_auto_999999999", harness="codex", pid=999999999)
    store.record_read("sess_auto_999999999", "g_root")
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "strata", "freshness-hook"],
        input=json.dumps({"session_id": "x", "transcript_path": "/tmp/t"}),
        capture_output=True,
        text=True,
        env=_env(tmp_path),
        cwd=str(tmp_path),
        timeout=60,
        check=False,
    )

    assert proc.stdout == ""
    assert store.read("sess_auto_999999999").strict_blocked_at == ""  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Cap-2: the hook blocks at most twice per session, and a second time only when no
# strata call followed the first. Real processes again: one stand-in harness runs
# the real hook once per "stop", in sequence, so every stop resolves the session
# through the same ancestor walk.
# ---------------------------------------------------------------------------

_STOPS_HARNESS = """
import json, pathlib, subprocess, sys, time
sync = pathlib.Path(sys.argv[1])
payloads = json.loads(sys.argv[2])
pause_after_first = sys.argv[3] == "pause"
while not (sync / "start").exists():
    time.sleep(0.02)
outs = []
for i, payload in enumerate(payloads):
    if i == 1 and pause_after_first:
        (sync / "paused").write_text("x")
        while not (sync / "go").exists():
            time.sleep(0.02)
    r = subprocess.run(
        [sys.executable, "-m", "strata", "freshness-hook"],
        input=json.dumps(payload), capture_output=True, text=True,
    )
    outs.append(r.stdout)
print(json.dumps(outs))
"""


def _stops(root: Path, active_flags: list[bool], *, strata_call_between: bool) -> list[str]:
    """Run one stop per flag under a single harness pid; optionally have the agent
    make a strata tool call between the first and second stop. Returns each stop's
    hook stdout (``""`` when it did not block)."""
    import time

    store = SessionStateStore(root / ".strata" / "sessions")
    sync = root / "sync"
    sync.mkdir()
    payloads = [
        {"session_id": "x", "transcript_path": "/tmp/t", "stop_hook_active": flag}
        for flag in active_flags
    ]
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            _STOPS_HARNESS,
            str(sync),
            json.dumps(payloads),
            "pause" if strata_call_between else "run",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(root),
        cwd=str(root),
        text=True,
    )
    session_id = f"sess_auto_{proc.pid}"
    store.record_connect(session_id, harness="codex", pid=proc.pid)
    for _ in range(NUDGE_MIN_READS):
        store.record_read(session_id, "g_root")
    (sync / "start").write_text("x")
    if strata_call_between:
        deadline = time.monotonic() + 60
        while not (sync / "paused").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        store.record_tool_call(session_id)  # the agent reacted to the first block
        (sync / "go").write_text("x")
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, err
    return json.loads(out)


def test_repeated_stops_block_twice_and_never_a_third_time(tmp_path: Path) -> None:
    _project(tmp_path)

    outs = _stops(tmp_path, [False, True, True, False, True], strata_call_between=False)

    decisions = [json.loads(o)["decision"] if o else None for o in outs]
    assert decisions == ["block", "block", None, None, None]
    assert "last reminder" in json.loads(outs[1])["reason"]


def test_a_strata_call_between_the_stops_prevents_the_second_block(tmp_path: Path) -> None:
    _project(tmp_path)

    outs = _stops(tmp_path, [False, True, False], strata_call_between=True)

    assert [bool(o) for o in outs] == [True, False, False]
