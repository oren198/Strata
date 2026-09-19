"""Tests for the additive SessionStart-hook install machinery — symmetric with
the existing Stop-hook machinery (tests/test_freshness_install.py).

The READ side of memory has no equivalent trigger to the Stop hook's WRITE-side
"contribute before ending" nudge: an agent's static instructions to read its
perspective at session start compete with everything else in context and
never fire on their own. `strata register` (claude-code) now wires a
SessionStart hook running `strata session-start-hook`, installed the same way
the Stop hook is:

- merge_session_start_hook is strictly additive: appended only when absent, a
  user's own SessionStart hooks left intact, idempotent on re-merge.
- copy_hook(..., script_name=HOOK_SCRIPT_NAME_SESSION_START) / hook_matches_shipped
  mirror the skill copy/byte-identity rules.
- register installs the hook block + script on a fresh repo, --diff shows the
  delta without writing, a second register is a no-op.
- unregister removes the block + script only when byte-identical, and a
  pre-existing user SessionStart hook survives register->unregister untouched.

Vocabulary follows CONTEXT.md: scope, fleet, skill, scope-manager.
"""

from __future__ import annotations

import argparse
import importlib.resources
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata import install  # noqa: E402
from strata.__main__ import cmd_register, cmd_unregister  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_project(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()


def _register(tmp_path: Path, *, diff: bool = False) -> int:
    return cmd_register(
        argparse.Namespace(path=str(tmp_path), diff=diff, bootstrap_venv=False, harness=None)
    )


def _unregister(tmp_path: Path, *, purge_data: bool = False, dry_run: bool = False) -> int:
    return cmd_unregister(
        argparse.Namespace(path=str(tmp_path), dry_run=dry_run, purge_data=purge_data)
    )


def _settings(tmp_path: Path) -> dict:
    return json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# merge_session_start_hook — additive semantics
# ---------------------------------------------------------------------------


def test_merge_session_start_hook_into_empty_settings() -> None:
    data: dict = {}
    assert install.merge_session_start_hook(data) is True
    assert data["hooks"]["SessionStart"] == [install.HOOK_SESSION_START_ENTRY]


def test_merge_session_start_hook_is_idempotent() -> None:
    data: dict = {}
    install.merge_session_start_hook(data)
    assert install.merge_session_start_hook(data) is False
    assert len(data["hooks"]["SessionStart"]) == 1


def test_merge_preserves_a_users_existing_session_start_hook() -> None:
    user_hook = {"hooks": [{"type": "command", "command": "my-greeter"}]}
    data = {"hooks": {"SessionStart": [user_hook]}}
    assert install.merge_session_start_hook(data) is True
    start = data["hooks"]["SessionStart"]
    assert user_hook in start  # user's hook untouched
    assert install.HOOK_SESSION_START_ENTRY in start  # ours appended alongside
    assert len(start) == 2


def test_merge_session_start_preserves_other_hook_events() -> None:
    data = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "x"}]}]}}
    install.merge_session_start_hook(data)
    assert "PreToolUse" in data["hooks"]  # unrelated event preserved
    assert data["hooks"]["SessionStart"] == [install.HOOK_SESSION_START_ENTRY]


def test_merge_session_start_and_stop_hooks_coexist() -> None:
    data: dict = {}
    install.merge_stop_hook(data)
    install.merge_session_start_hook(data)
    assert data["hooks"]["Stop"] == [install.HOOK_STOP_ENTRY]
    assert data["hooks"]["SessionStart"] == [install.HOOK_SESSION_START_ENTRY]


def test_session_start_hook_present_ignores_unrelated_hooks() -> None:
    data = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "other"}]}]}}
    assert install.session_start_hook_present(data) is False


# ---------------------------------------------------------------------------
# remove_session_start_hook — reverse, byte-identical only
# ---------------------------------------------------------------------------


def test_remove_session_start_hook_removes_canonical_group() -> None:
    data: dict = {}
    install.merge_session_start_hook(data)
    assert install.remove_session_start_hook(data) == "removed"
    assert "hooks" not in data  # emptied containers cleaned up


def test_remove_session_start_hook_leaves_edited_group() -> None:
    data: dict = {}
    install.merge_session_start_hook(data)
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] += " --edited"
    assert install.remove_session_start_hook(data) == "edited"
    assert data["hooks"]["SessionStart"]  # left in place


def test_remove_session_start_hook_absent() -> None:
    data = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "other"}]}]}}
    assert install.remove_session_start_hook(data) == "absent"


def test_remove_session_start_hook_preserves_user_hook() -> None:
    user_hook = {"hooks": [{"type": "command", "command": "my-greeter"}]}
    data = {"hooks": {"SessionStart": [user_hook]}}
    install.merge_session_start_hook(data)
    assert install.remove_session_start_hook(data) == "removed"
    assert data["hooks"]["SessionStart"] == [user_hook]  # only ours removed


def test_remove_session_start_hook_does_not_touch_stop_hook() -> None:
    data: dict = {}
    install.merge_stop_hook(data)
    install.merge_session_start_hook(data)
    assert install.remove_session_start_hook(data) == "removed"
    assert data["hooks"]["Stop"] == [install.HOOK_STOP_ENTRY]
    assert "SessionStart" not in data["hooks"]


# ---------------------------------------------------------------------------
# copy_hook / hook_matches_shipped — the SessionStart script
# ---------------------------------------------------------------------------


def test_copy_session_start_hook_installs_executable_script(tmp_path: Path) -> None:
    hooks_root = importlib.resources.files("strata") / "_hooks"
    dest = tmp_path / "hooks"
    assert (
        install.copy_hook(hooks_root, dest, script_name=install.HOOK_SCRIPT_NAME_SESSION_START)
        is True
    )
    script = dest / install.HOOK_SCRIPT_NAME_SESSION_START
    assert script.exists()
    assert script.stat().st_mode & 0o111  # executable bit set
    assert (
        install.copy_hook(hooks_root, dest, script_name=install.HOOK_SCRIPT_NAME_SESSION_START)
        is False
    )


def test_session_start_hook_matches_shipped(tmp_path: Path) -> None:
    hooks_root = importlib.resources.files("strata") / "_hooks"
    dest = tmp_path / "hooks"
    install.copy_hook(hooks_root, dest, script_name=install.HOOK_SCRIPT_NAME_SESSION_START)
    script = dest / install.HOOK_SCRIPT_NAME_SESSION_START
    assert (
        install.hook_matches_shipped(script, script_name=install.HOOK_SCRIPT_NAME_SESSION_START)
        is True
    )
    script.write_text("edited\n", encoding="utf-8")
    assert (
        install.hook_matches_shipped(script, script_name=install.HOOK_SCRIPT_NAME_SESSION_START)
        is False
    )


# ---------------------------------------------------------------------------
# register / --diff / unregister integration
# ---------------------------------------------------------------------------


def test_register_installs_session_start_hook_block_and_script(tmp_path: Path) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path) == 0

    script = tmp_path / ".claude" / "hooks" / install.HOOK_SCRIPT_NAME_SESSION_START
    assert script.exists()
    assert install.session_start_hook_present(_settings(tmp_path))
    assert _settings(tmp_path)["hooks"]["SessionStart"] == [install.HOOK_SESSION_START_ENTRY]
    # The Stop hook is still wired alongside it — additive, not replaced.
    assert install.stop_hook_present(_settings(tmp_path))


def test_register_diff_shows_session_start_hook_without_writing(tmp_path: Path, capsys) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path, diff=True) == 0
    out = capsys.readouterr().out
    assert install.HOOK_SCRIPT_NAME_SESSION_START in out
    assert not (tmp_path / ".claude" / "hooks").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_second_register_is_noop_for_session_start_hook(tmp_path: Path, capsys) -> None:
    _init_project(tmp_path)
    _register(tmp_path)
    before = _settings(tmp_path)
    capsys.readouterr()
    _register(tmp_path)
    out = capsys.readouterr().out
    assert "kept user's" in out or "skip" in out.lower()
    assert _settings(tmp_path) == before  # no duplicate SessionStart entry


def test_register_appends_to_preexisting_user_session_start_hook(tmp_path: Path) -> None:
    _init_project(tmp_path)
    claude = tmp_path / ".claude"
    claude.mkdir()
    user_hook = {"hooks": [{"type": "command", "command": "my-greeter.sh"}]}
    (claude / "settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": [user_hook]}}, indent=2) + "\n", encoding="utf-8"
    )
    assert _register(tmp_path) == 0

    start = _settings(tmp_path)["hooks"]["SessionStart"]
    assert user_hook in start  # user's SessionStart hook left intact
    assert install.HOOK_SESSION_START_ENTRY in start  # strata appended, not clobbering
    assert len(start) == 2


def test_unregister_removes_session_start_hook_block_and_script(tmp_path: Path) -> None:
    _init_project(tmp_path)
    _register(tmp_path)
    assert _unregister(tmp_path) == 0

    assert not (tmp_path / ".claude" / "hooks" / install.HOOK_SCRIPT_NAME_SESSION_START).exists()
    assert not install.session_start_hook_present(_settings(tmp_path))


def test_unregister_leaves_edited_session_start_hook_script(tmp_path: Path, capsys) -> None:
    _init_project(tmp_path)
    _register(tmp_path)
    script = tmp_path / ".claude" / "hooks" / install.HOOK_SCRIPT_NAME_SESSION_START
    script.write_text("# user edited\n", encoding="utf-8")

    rc = _unregister(tmp_path)
    assert rc == 1  # something asked-to-remove was left in place
    assert script.exists()
    err = capsys.readouterr().err
    assert install.HOOK_SCRIPT_NAME_SESSION_START in err
    assert "differs" in err or "modified" in err


def test_unregister_preserves_user_session_start_hook(tmp_path: Path) -> None:
    _init_project(tmp_path)
    claude = tmp_path / ".claude"
    claude.mkdir()
    user_hook = {"hooks": [{"type": "command", "command": "my-greeter.sh"}]}
    (claude / "settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": [user_hook]}}, indent=2) + "\n", encoding="utf-8"
    )
    _register(tmp_path)
    assert _unregister(tmp_path) == 0

    start = _settings(tmp_path)["hooks"]["SessionStart"]
    assert start == [user_hook]  # only strata's group removed


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
