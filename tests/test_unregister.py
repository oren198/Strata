"""Tests for `strata unregister` — reverse the brownfield wiring (issue #53).

`strata unregister` undoes exactly what `strata register` wired, honouring
ADR 0005 Decision 6 ("never delete or override user state") in reverse: every
artifact is removed ONLY when it still byte-matches what register would have
written. Edited artifacts are reported and left in place, and the run exits 1
so scripts can detect the partial case.

The load-bearing safety properties, each exercised below:

- register → unregister --purge-data on a clean project restores the working
  tree byte-for-byte (recursive listing + per-file hashes identical).
- an edited skill / edited settings entry is left in place, explained, exit 1.
- --dry-run changes nothing.
- --purge-data removes .strata/; without it .strata/ survives with the
  "memory, not wiring" note.
- unregister on an unregistered project reports nothing-to-do and exits 0.

Vocabulary: scope, fleet, skill, scope-manager.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.__main__ import _build_parser, cmd_register, cmd_unregister

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_args(path: str) -> argparse.Namespace:
    return argparse.Namespace(path=path, diff=False, bootstrap_venv=False, python=None)


def _unregister_args(
    path: str, *, dry_run: bool = False, purge_data: bool = False
) -> argparse.Namespace:
    return argparse.Namespace(path=path, dry_run=dry_run, purge_data=purge_data)


def _init_project(tmp_path: Path) -> None:
    """Create a minimal project with a .git marker."""
    (tmp_path / ".git").mkdir()


def _tree_snapshot(root: Path) -> dict[str, str]:
    """Map every file under *root* to a hash of its bytes (recursive).

    Directories are represented by a sentinel so an empty directory that
    appears or vanishes is caught by the comparison too.
    """
    snapshot: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_dir():
            snapshot[rel + "/"] = "<dir>"
        else:
            snapshot[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snapshot


# ---------------------------------------------------------------------------
# Acceptance criterion 1: clean round-trip restores the tree byte-for-byte
# ---------------------------------------------------------------------------


def test_register_then_unregister_purge_restores_tree_exactly(tmp_path: Path) -> None:
    """register → unregister --purge-data leaves the tree byte-for-byte identical.

    The project starts with a pre-existing `.gitignore` and
    `.claude/settings.json` carrying unrelated user entries. Those must survive
    exactly. The settings file is seeded in register's own writer format
    (json.dumps(indent=2) + trailing newline) so the round-trip is byte-exact
    where the JSON round-trip allows.
    """
    _init_project(tmp_path)

    # Pre-existing user .gitignore (ends in newline — the normal case).
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("__pycache__/\n*.log\n.env\n", encoding="utf-8")

    # Pre-existing user settings.json with unrelated entries, written in the
    # same format register uses so the round-trip can be byte-exact.
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    user_settings = {
        "theme": "dark",
        "mcpServers": {"other-tool": {"command": "other-tool-bin"}},
        "keybindings": [],
    }
    (claude_dir / "settings.json").write_text(
        json.dumps(user_settings, indent=2) + "\n", encoding="utf-8"
    )

    before = _tree_snapshot(tmp_path)

    rc_reg = cmd_register(_register_args(str(tmp_path)))
    assert rc_reg == 0

    rc_unreg = cmd_unregister(_unregister_args(str(tmp_path), purge_data=True))
    assert rc_unreg == 0

    after = _tree_snapshot(tmp_path)
    added = set(after) - set(before)
    changed = {k for k in before if k in after and after[k] != before[k]}
    removed = set(before) - set(after)
    assert after == before, (
        "register → unregister --purge-data did not restore the tree exactly.\n"
        f"added: {added}\nchanged: {changed}\nremoved: {removed}"
    )


def test_round_trip_with_no_preexisting_claude_or_gitignore(tmp_path: Path) -> None:
    """Round-trip on a bare project (register created .gitignore + .claude).

    register creates `.gitignore` and `.claude/` from scratch. After
    unregister --purge-data, the register-created files that become empty are
    conservatively left in place (their register-authorship is not detectable
    from content), mirroring the empty-.gitignore rule: `.gitignore` is now
    empty and `.claude/settings.json` is now `{}`. The vendored skills and
    the purged `.strata/` are gone.
    """
    _init_project(tmp_path)

    cmd_register(_register_args(str(tmp_path)))
    rc = cmd_unregister(_unregister_args(str(tmp_path), purge_data=True))
    assert rc == 0

    # .strata purged.
    assert not (tmp_path / ".strata").exists()
    # Skills removed; the skills dir is gone.
    assert not (tmp_path / ".claude" / "skills").exists()
    # settings.json is now an empty object, conservatively left in place.
    settings_json = tmp_path / ".claude" / "settings.json"
    assert json.loads(settings_json.read_text(encoding="utf-8")) == {}
    # The register-created .gitignore is now empty and left in place.
    gitignore = tmp_path / ".gitignore"
    assert gitignore.read_text(encoding="utf-8").strip() == ""


# ---------------------------------------------------------------------------
# Acceptance criterion 2: edited artifacts left in place, explained, exit 1
# ---------------------------------------------------------------------------


def test_edited_skill_left_in_place_exit_1(tmp_path: Path, capsys) -> None:
    """An edited vendored skill is left in place, explained, and exits 1."""
    _init_project(tmp_path)
    cmd_register(_register_args(str(tmp_path)))

    skill_md = tmp_path / ".claude" / "skills" / "strata-worker" / "Skill.md"
    edited = skill_md.read_text(encoding="utf-8") + "\n<!-- user tweak -->\n"
    skill_md.write_text(edited, encoding="utf-8")

    rc = cmd_unregister(_unregister_args(str(tmp_path)))

    assert rc == 1
    # The edited skill survives untouched.
    assert skill_md.exists()
    assert skill_md.read_text(encoding="utf-8") == edited
    captured = capsys.readouterr()
    assert "strata-worker" in captured.err
    assert "modified" in captured.err or "differs" in captured.err
    # The two unmodified skills were still removed.
    assert not (tmp_path / ".claude" / "skills" / "strata").exists()
    assert not (tmp_path / ".claude" / "skills" / "strata-inspect").exists()


def test_edited_settings_entry_left_in_place_exit_1(tmp_path: Path, capsys) -> None:
    """An edited mcpServers.strata entry is left in place and exits 1."""
    _init_project(tmp_path)
    cmd_register(_register_args(str(tmp_path)))

    settings_json = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings_json.read_text(encoding="utf-8"))
    data["mcpServers"]["strata"]["command"] = "/custom/strata-mcp"
    settings_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    rc = cmd_unregister(_unregister_args(str(tmp_path)))

    assert rc == 1
    on_disk = json.loads(settings_json.read_text(encoding="utf-8"))
    assert on_disk["mcpServers"]["strata"]["command"] == "/custom/strata-mcp"
    captured = capsys.readouterr()
    assert "settings.json" in captured.err
    assert "edited" in captured.err or "differs" in captured.err


def test_edited_gitignore_block_left_in_place_exit_1(tmp_path: Path, capsys) -> None:
    """An edited managed .gitignore block is left in place and exits 1."""
    _init_project(tmp_path)
    cmd_register(_register_args(str(tmp_path)))

    gitignore = tmp_path / ".gitignore"
    content = gitignore.read_text(encoding="utf-8")
    # Edit inside the managed block (keeps the marker header, changes a line).
    edited = content.replace(".strata/summaries/", ".strata/summaries/\n.strata/extra/")
    assert edited != content
    gitignore.write_text(edited, encoding="utf-8")

    rc = cmd_unregister(_unregister_args(str(tmp_path)))

    assert rc == 1
    assert gitignore.read_text(encoding="utf-8") == edited
    captured = capsys.readouterr()
    assert ".gitignore" in captured.err


# ---------------------------------------------------------------------------
# Acceptance criterion 3: --dry-run changes nothing
# ---------------------------------------------------------------------------


def test_dry_run_changes_nothing(tmp_path: Path, capsys) -> None:
    """--dry-run prints actions but does not touch the tree."""
    _init_project(tmp_path)
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    cmd_register(_register_args(str(tmp_path)))

    before = _tree_snapshot(tmp_path)

    rc = cmd_unregister(_unregister_args(str(tmp_path), dry_run=True, purge_data=True))
    assert rc == 0

    after = _tree_snapshot(tmp_path)
    assert after == before, "--dry-run modified the tree"

    captured = capsys.readouterr()
    # Dry-run output uses "would" language.
    assert "would" in (captured.out + captured.err).lower()


def test_dry_run_reports_edited_and_exits_1(tmp_path: Path) -> None:
    """--dry-run still surfaces the partial case (edited artifact) with exit 1."""
    _init_project(tmp_path)
    cmd_register(_register_args(str(tmp_path)))
    skill_md = tmp_path / ".claude" / "skills" / "strata" / "Skill.md"
    skill_md.write_text("edited\n", encoding="utf-8")

    before = _tree_snapshot(tmp_path)
    rc = cmd_unregister(_unregister_args(str(tmp_path), dry_run=True))
    after = _tree_snapshot(tmp_path)

    assert rc == 1
    assert after == before


# ---------------------------------------------------------------------------
# Acceptance criterion 4: --purge-data behaviour
# ---------------------------------------------------------------------------


def test_without_purge_data_strata_survives_with_note(tmp_path: Path, capsys) -> None:
    """Without --purge-data, .strata/ survives and the note is shown."""
    _init_project(tmp_path)
    cmd_register(_register_args(str(tmp_path)))

    rc = cmd_unregister(_unregister_args(str(tmp_path)))
    assert rc == 0

    strata_dir = tmp_path / ".strata"
    assert strata_dir.is_dir()
    assert (strata_dir / "config.toml").exists()
    assert (strata_dir / "fleet.yaml").exists()

    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "memory, not wiring" in out
    assert "--purge-data" in out


def test_purge_data_removes_strata(tmp_path: Path) -> None:
    """--purge-data removes the .strata/ workspace entirely."""
    _init_project(tmp_path)
    cmd_register(_register_args(str(tmp_path)))
    # Simulate accumulated memory: a DB file and a summaries dir.
    (tmp_path / ".strata" / "strata.db").write_text("x", encoding="utf-8")
    (tmp_path / ".strata" / "summaries").mkdir()
    (tmp_path / ".strata" / "summaries" / "g_root.md").write_text("y", encoding="utf-8")

    rc = cmd_unregister(_unregister_args(str(tmp_path), purge_data=True))
    assert rc == 0
    assert not (tmp_path / ".strata").exists()


# ---------------------------------------------------------------------------
# Acceptance criterion 5: idempotent on an unregistered project
# ---------------------------------------------------------------------------


def test_unregister_on_unregistered_project_nothing_to_do(tmp_path: Path, capsys) -> None:
    """unregister on a bare project reports nothing-to-do per item and exits 0."""
    _init_project(tmp_path)

    rc = cmd_unregister(_unregister_args(str(tmp_path)))
    assert rc == 0

    captured = capsys.readouterr()
    assert "nothing to do" in captured.out.lower()


def test_unregister_is_idempotent(tmp_path: Path) -> None:
    """Running unregister twice is safe; the second run is a no-op, exit 0."""
    _init_project(tmp_path)
    cmd_register(_register_args(str(tmp_path)))

    rc1 = cmd_unregister(_unregister_args(str(tmp_path), purge_data=True))
    rc2 = cmd_unregister(_unregister_args(str(tmp_path), purge_data=True))
    assert rc1 == 0
    assert rc2 == 0


# ---------------------------------------------------------------------------
# Per-step behaviour
# ---------------------------------------------------------------------------


def test_gitignore_block_removed_byte_exact(tmp_path: Path) -> None:
    """The managed block is removed and surrounding lines stay byte-identical."""
    _init_project(tmp_path)
    original = "node_modules/\n*.tmp\n"
    (tmp_path / ".gitignore").write_text(original, encoding="utf-8")

    cmd_register(_register_args(str(tmp_path)))
    cmd_unregister(_unregister_args(str(tmp_path), purge_data=True))

    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == original


def test_settings_entry_removed_preserving_other_keys(tmp_path: Path) -> None:
    """Removing mcpServers.strata preserves every other settings key."""
    _init_project(tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    user_settings = {
        "theme": "dark",
        "mcpServers": {"other-tool": {"command": "other-tool-bin"}},
    }
    (claude_dir / "settings.json").write_text(
        json.dumps(user_settings, indent=2) + "\n", encoding="utf-8"
    )

    cmd_register(_register_args(str(tmp_path)))
    cmd_unregister(_unregister_args(str(tmp_path), purge_data=True))

    data = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["mcpServers"] == {"other-tool": {"command": "other-tool-bin"}}
    assert "strata" not in data.get("mcpServers", {})


def test_settings_mcpservers_block_dropped_when_only_strata(tmp_path: Path) -> None:
    """When register created mcpServers solely for strata, the block is dropped."""
    _init_project(tmp_path)
    cmd_register(_register_args(str(tmp_path)))
    settings_json = tmp_path / ".claude" / "settings.json"
    assert "mcpServers" in json.loads(settings_json.read_text(encoding="utf-8"))

    cmd_unregister(_unregister_args(str(tmp_path)))

    data = json.loads(settings_json.read_text(encoding="utf-8"))
    assert "mcpServers" not in data


def test_unmodified_skills_removed(tmp_path: Path) -> None:
    """All three unmodified vendored skills are removed."""
    _init_project(tmp_path)
    cmd_register(_register_args(str(tmp_path)))

    cmd_unregister(_unregister_args(str(tmp_path)))

    for skill in ["strata", "strata-worker", "strata-inspect"]:
        assert not (tmp_path / ".claude" / "skills" / skill).exists()


def test_skill_dir_with_extra_user_file_is_preserved(tmp_path: Path, capsys) -> None:
    """A user file alongside a vendored Skill.md keeps the skill dir alive.

    The unmodified Skill.md is removed, but the directory survives because it
    still holds a user-authored file — we never delete user state.
    """
    _init_project(tmp_path)
    cmd_register(_register_args(str(tmp_path)))
    skill_dir = tmp_path / ".claude" / "skills" / "strata"
    (skill_dir / "notes.md").write_text("my notes\n", encoding="utf-8")

    rc = cmd_unregister(_unregister_args(str(tmp_path)))
    assert rc == 0

    # Skill.md removed (it matched shipped), but user file + dir survive.
    assert not (skill_dir / "Skill.md").exists()
    assert (skill_dir / "notes.md").read_text(encoding="utf-8") == "my notes\n"


# ---------------------------------------------------------------------------
# Parser wiring
# ---------------------------------------------------------------------------


def test_unregister_in_parser() -> None:
    """'strata unregister --help' must not raise (parser correctly wired)."""
    parser = _build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["unregister", "--help"])
    assert exc_info.value.code == 0


def test_unregister_flags_parse() -> None:
    """--dry-run and --purge-data parse into the expected Namespace fields."""
    parser = _build_parser()
    args = parser.parse_args(["unregister", "/some/path", "--dry-run", "--purge-data"])
    assert args.path == "/some/path"
    assert args.dry_run is True
    assert args.purge_data is True
    assert args.func is cmd_unregister


def test_unregister_help_documents_exit_codes() -> None:
    """The unregister description documents the exit-1 partial-case contract."""
    parser = _build_parser()
    # Reach into the subparser to inspect its description.
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    unreg = subparsers_action.choices["unregister"]
    assert "Exit code" in (unreg.description or "")
    assert "--purge-data" in (unreg.description or "")
