"""Tests for the additive Stop-hook install machinery (issue #112; ADR 0005 D6).

Covers the engine-level merge/copy/remove helpers in :mod:`strata.install` and
their wiring into ``strata register`` / ``register --diff`` / ``strata
unregister``:

- merge_stop_hook is strictly additive: appended only when absent, a user's own
  Stop hooks left intact, idempotent on re-merge.
- copy_hook / hook_matches_shipped mirror the skill copy/byte-identity rules.
- register installs the hook block + script on a fresh repo, --diff shows the
  delta without writing, a second register is a no-op.
- unregister removes the block + script only when byte-identical, and a
  pre-existing user Stop hook survives register→unregister untouched.

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
    return cmd_register(argparse.Namespace(path=str(tmp_path), diff=diff, bootstrap_venv=False))


def _unregister(tmp_path: Path, *, purge_data: bool = False, dry_run: bool = False) -> int:
    return cmd_unregister(
        argparse.Namespace(path=str(tmp_path), dry_run=dry_run, purge_data=purge_data)
    )


def _settings(tmp_path: Path) -> dict:
    return json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# merge_stop_hook — additive semantics
# ---------------------------------------------------------------------------


def test_merge_stop_hook_into_empty_settings() -> None:
    data: dict = {}
    assert install.merge_stop_hook(data) is True
    assert data["hooks"]["Stop"] == [install.HOOK_STOP_ENTRY]


def test_merge_stop_hook_is_idempotent() -> None:
    data: dict = {}
    install.merge_stop_hook(data)
    # A second merge finds it present and does not duplicate.
    assert install.merge_stop_hook(data) is False
    assert len(data["hooks"]["Stop"]) == 1


def test_merge_preserves_a_users_existing_stop_hook() -> None:
    user_hook = {"hooks": [{"type": "command", "command": "my-linter"}]}
    data = {"hooks": {"Stop": [user_hook]}}
    assert install.merge_stop_hook(data) is True
    stop = data["hooks"]["Stop"]
    assert user_hook in stop  # user's hook untouched
    assert install.HOOK_STOP_ENTRY in stop  # ours appended alongside
    assert len(stop) == 2


def test_merge_preserves_other_hook_events() -> None:
    data = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "x"}]}]}}
    install.merge_stop_hook(data)
    assert "PreToolUse" in data["hooks"]  # unrelated event preserved
    assert data["hooks"]["Stop"] == [install.HOOK_STOP_ENTRY]


def test_stop_hook_present_ignores_unrelated_hooks() -> None:
    data = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}
    assert install.stop_hook_present(data) is False


# ---------------------------------------------------------------------------
# remove_stop_hook — reverse, byte-identical only
# ---------------------------------------------------------------------------


def test_remove_stop_hook_removes_canonical_group() -> None:
    data: dict = {}
    install.merge_stop_hook(data)
    assert install.remove_stop_hook(data) == "removed"
    assert "hooks" not in data  # emptied containers cleaned up


def test_remove_stop_hook_leaves_edited_group() -> None:
    data: dict = {}
    install.merge_stop_hook(data)
    data["hooks"]["Stop"][0]["hooks"][0]["command"] += " --edited"
    assert install.remove_stop_hook(data) == "edited"
    assert data["hooks"]["Stop"]  # left in place


def test_remove_stop_hook_absent() -> None:
    data = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}
    assert install.remove_stop_hook(data) == "absent"


def test_remove_stop_hook_preserves_user_hook() -> None:
    user_hook = {"hooks": [{"type": "command", "command": "my-linter"}]}
    data = {"hooks": {"Stop": [user_hook]}}
    install.merge_stop_hook(data)
    assert install.remove_stop_hook(data) == "removed"
    assert data["hooks"]["Stop"] == [user_hook]  # only ours removed


# ---------------------------------------------------------------------------
# copy_hook / hook_matches_shipped
# ---------------------------------------------------------------------------


def test_copy_hook_installs_executable_script(tmp_path: Path) -> None:
    hooks_root = importlib.resources.files("strata") / "_hooks"
    dest = tmp_path / "hooks"
    assert install.copy_hook(hooks_root, dest) is True
    script = dest / install.HOOK_SCRIPT_NAME
    assert script.exists()
    assert script.stat().st_mode & 0o111  # executable bit set
    # Additive: a second copy is a no-op.
    assert install.copy_hook(hooks_root, dest) is False


def test_hook_matches_shipped(tmp_path: Path) -> None:
    hooks_root = importlib.resources.files("strata") / "_hooks"
    dest = tmp_path / "hooks"
    install.copy_hook(hooks_root, dest)
    script = dest / install.HOOK_SCRIPT_NAME
    assert install.hook_matches_shipped(script) is True
    script.write_text("edited\n", encoding="utf-8")
    assert install.hook_matches_shipped(script) is False


# ---------------------------------------------------------------------------
# register / --diff / unregister integration
# ---------------------------------------------------------------------------


def test_register_installs_hook_block_and_script(tmp_path: Path) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path) == 0

    script = tmp_path / ".claude" / "hooks" / install.HOOK_SCRIPT_NAME
    assert script.exists()
    assert install.stop_hook_present(_settings(tmp_path))
    assert _settings(tmp_path)["hooks"]["Stop"] == [install.HOOK_STOP_ENTRY]


def test_register_diff_shows_hook_without_writing(tmp_path: Path, capsys) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path, diff=True) == 0
    out = capsys.readouterr().out
    assert install.HOOK_SCRIPT_NAME in out
    # --diff writes nothing.
    assert not (tmp_path / ".claude" / "hooks").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_second_register_is_noop_for_hook(tmp_path: Path, capsys) -> None:
    _init_project(tmp_path)
    _register(tmp_path)
    before = _settings(tmp_path)
    capsys.readouterr()
    _register(tmp_path)
    out = capsys.readouterr().out
    assert "kept user's" in out or "skip" in out.lower()
    assert _settings(tmp_path) == before  # no duplicate Stop entry


def test_register_appends_to_preexisting_user_stop_hook(tmp_path: Path) -> None:
    _init_project(tmp_path)
    claude = tmp_path / ".claude"
    claude.mkdir()
    user_hook = {"hooks": [{"type": "command", "command": "my-linter.sh"}]}
    (claude / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [user_hook]}}, indent=2) + "\n", encoding="utf-8"
    )
    assert _register(tmp_path) == 0

    stop = _settings(tmp_path)["hooks"]["Stop"]
    assert user_hook in stop  # user's Stop hook left intact
    assert install.HOOK_STOP_ENTRY in stop  # strata appended, not clobbering
    assert len(stop) == 2


def test_unregister_removes_hook_block_and_script(tmp_path: Path) -> None:
    _init_project(tmp_path)
    _register(tmp_path)
    assert _unregister(tmp_path) == 0

    assert not (tmp_path / ".claude" / "hooks" / install.HOOK_SCRIPT_NAME).exists()
    assert not install.stop_hook_present(_settings(tmp_path))


def test_unregister_leaves_edited_hook_script(tmp_path: Path, capsys) -> None:
    _init_project(tmp_path)
    _register(tmp_path)
    script = tmp_path / ".claude" / "hooks" / install.HOOK_SCRIPT_NAME
    script.write_text("# user edited\n", encoding="utf-8")

    rc = _unregister(tmp_path)
    assert rc == 1  # something asked-to-remove was left in place
    assert script.exists()
    err = capsys.readouterr().err
    assert install.HOOK_SCRIPT_NAME in err
    assert "differs" in err or "modified" in err


def test_unregister_preserves_user_stop_hook(tmp_path: Path) -> None:
    _init_project(tmp_path)
    claude = tmp_path / ".claude"
    claude.mkdir()
    user_hook = {"hooks": [{"type": "command", "command": "my-linter.sh"}]}
    (claude / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [user_hook]}}, indent=2) + "\n", encoding="utf-8"
    )
    _register(tmp_path)
    assert _unregister(tmp_path) == 0

    stop = _settings(tmp_path)["hooks"]["Stop"]
    assert stop == [user_hook]  # only strata's group removed


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
