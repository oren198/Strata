"""Tests for `strata register`'s self-update mechanism.

Re-running `strata register` today always *keeps* an existing managed
artifact (a skill's ``Skill.md``, the Stop-hook script, the AGENTS.md block)
exactly as it found it — so a guidance fix shipped in a later release never
reaches an already-registered project unless the user deletes the file by
hand. This module covers the three-state resolution register now applies to
every managed artifact:

1. Existing content == current shipped content → skip, unchanged (today's
   behavior).
2. Existing content's hash is a known *historical* shipped hash (an older
   `strata register` wrote it, and it was never hand-edited) → self-update:
   overwrite with current shipped content, report "updated ... (shipped
   content changed)".
3. Anything else (a hash this table doesn't recognize) → user-edited: kept,
   with a one-line "(differs from shipped — see strata register --diff)"
   note.

Also covers:

- Idempotence: a second register run after a self-update reports nothing
  left to do (case 1 for everything just updated).
- The release-discipline test: `strata.install._HISTORICAL_ARTIFACT_HASHES`
  records each artifact's actual, currently-shipped content hash under
  "current" — this fails the build the moment a shipped artifact's content
  changes without that table being updated (see its maintenance comment in
  src/strata/install.py for the "move current into historical" half a human
  still has to do by hand).
- register → self-update → unregister still removes cleanly (unregister's
  byte-match rule matches the just-self-updated content, which now equals
  current shipped).

Codex's `config.toml` blocks (`[mcp_servers.strata]`, `[[hooks.Stop]]`) are
NOT self-updated in place the way skills/the hook script/AGENTS.md are —
they're unstructured TOML, and register has never needed to "refresh" them.
They ARE, however, covered by the release-discipline test below (round-4
unregister fix, bug B): `remove_codex_mcp_server`/`remove_codex_freshness_hook`
now recognize historical shipped block text the same way the other managed
artifacts recognize historical shipped hashes, and that mechanism needs the
same "current recorded hash actually matches shipped content" guardrail so a
future release that changes either block can't silently reintroduce the bug
by forgetting to record the old text as historical.

Vocabulary: scope, fleet, skill, scope-manager.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.resources
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata import install  # noqa: E402
from strata.__main__ import cmd_register, cmd_unregister  # noqa: E402

# ---------------------------------------------------------------------------
# Isolation (mirrors tests/test_register.py): register's end-of-run judge-key
# prompt must stay deterministic and never read a developer's real env/stdin.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _default_no_judge_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    for var in (
        "JUDGE_API_KEY",
        "ANTHROPIC_API_KEY",
        "STRATA_JUDGE_API_KEY",
        "STRATA_ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_project(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()


def _register(
    tmp_path: Path,
    *,
    harness: list[str] | None = None,
    diff: bool = False,
) -> tuple[int, str]:
    args = argparse.Namespace(
        path=str(tmp_path),
        diff=diff,
        bootstrap_venv=False,
        harness=harness,
        yes=True,
    )
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_register(args)
    return rc, buf.getvalue()


def _unregister(tmp_path: Path, *, harness: list[str] | None = None) -> int:
    args = argparse.Namespace(
        path=str(tmp_path),
        dry_run=False,
        purge_data=False,
        harness=harness,
    )
    return cmd_unregister(args)


def _shipped_skill_text(skill_name: str) -> str:
    return (importlib.resources.files("strata") / "_skills" / skill_name / "Skill.md").read_text(
        encoding="utf-8"
    )


def _shipped_hook_text() -> str:
    return (importlib.resources.files("strata") / "_hooks" / install.HOOK_SCRIPT_NAME).read_text(
        encoding="utf-8"
    )


def _shipped_agents_block() -> str:
    return install._shipped_agents_md_block()  # noqa: SLF001


# ---------------------------------------------------------------------------
# Skills — three-state resolution
# ---------------------------------------------------------------------------


def test_skill_matching_shipped_is_skipped(tmp_path: Path) -> None:
    """Case 1: existing content == current shipped → skip, unchanged."""
    _init_project(tmp_path)
    rc, _ = _register(tmp_path, harness=["claude-code"])
    assert rc == 0

    skill_md = tmp_path / ".claude" / "skills" / "strata" / "Skill.md"
    before = skill_md.read_text(encoding="utf-8")

    rc2, out2 = _register(tmp_path, harness=["claude-code"])
    assert rc2 == 0
    assert skill_md.read_text(encoding="utf-8") == before
    # A plain "match" skip prints the ordinary skip line register always has
    # ("kept user's <path>", same wording every skip uses) with no self-update
    # note attached to this artifact's line.
    assert "kept user's .claude/skills/strata" in out2
    assert "differs from shipped" not in out2
    assert "updated:" not in out2


def test_skill_stale_historical_content_is_self_updated(tmp_path: Path) -> None:
    """Case 2: installed content hashes to a known-historical shipped
    version → register overwrites it with current shipped content and
    reports "updated ... (shipped content changed)"."""
    _init_project(tmp_path)
    rc, _ = _register(tmp_path, harness=["claude-code"])
    assert rc == 0

    skill_md = tmp_path / ".claude" / "skills" / "strata" / "Skill.md"
    # Simulate a project registered on an earlier release: content that was
    # shipped once but never hand-edited. The real table already has a
    # historical hash for the "strata" skill (an earlier shipped version);
    # this test uses synthetic content + a scoped monkeypatch of the table
    # instead of reconstructing that exact historical text, so it stays
    # independent of which release actually produced it.
    synthetic_old = "# Old strata skill content\nThis is a stale, unedited shipped version.\n"
    synthetic_hash = hashlib.sha256(synthetic_old.encode("utf-8")).hexdigest()
    skill_md.write_text(synthetic_old, encoding="utf-8")

    real_hashes = install._HISTORICAL_ARTIFACT_HASHES  # noqa: SLF001
    patched = {
        **real_hashes,
        "strata": {
            "current": real_hashes["strata"]["current"],
            "historical": frozenset({synthetic_hash}),
        },
    }
    import unittest.mock

    with unittest.mock.patch.object(install, "_HISTORICAL_ARTIFACT_HASHES", patched):
        rc2, out2 = _register(tmp_path, harness=["claude-code"])

    assert rc2 == 0
    assert skill_md.read_text(encoding="utf-8") == _shipped_skill_text("strata")
    assert "updated:" in out2
    assert "shipped content changed" in out2


def test_skill_stale_historical_content_under_diff_writes_nothing(tmp_path: Path) -> None:
    """--diff must stay read-only for a stale-but-historical artifact too:
    the classification and reporting run, but the file is never touched —
    only the applied ("updated:") vs. read-only ("[would update]") wording
    differs."""
    _init_project(tmp_path)
    rc, _ = _register(tmp_path, harness=["claude-code"])
    assert rc == 0

    skill_md = tmp_path / ".claude" / "skills" / "strata" / "Skill.md"
    synthetic_old = "# Old strata skill content\nThis is a stale, unedited shipped version.\n"
    synthetic_hash = hashlib.sha256(synthetic_old.encode("utf-8")).hexdigest()
    skill_md.write_text(synthetic_old, encoding="utf-8")
    before = skill_md.read_bytes()

    real_hashes = install._HISTORICAL_ARTIFACT_HASHES  # noqa: SLF001
    patched = {
        **real_hashes,
        "strata": {
            "current": real_hashes["strata"]["current"],
            "historical": frozenset({synthetic_hash}),
        },
    }
    import unittest.mock

    with unittest.mock.patch.object(install, "_HISTORICAL_ARTIFACT_HASHES", patched):
        rc2, out2 = _register(tmp_path, harness=["claude-code"], diff=True)

    assert rc2 == 0
    # No write at all — byte-identical to the stale content written above,
    # not to shipped content (that would prove --diff silently applied it).
    assert skill_md.read_bytes() == before
    assert skill_md.read_text(encoding="utf-8") != _shipped_skill_text("strata")
    assert "[would update]" in out2
    assert "shipped content changed" in out2
    assert "updated:" not in out2


def test_skill_edited_content_is_kept_with_note(tmp_path: Path) -> None:
    """Case 3: installed content doesn't match shipped and its hash isn't
    recognized as historical → user-edited: kept, with a diff note."""
    _init_project(tmp_path)
    rc, _ = _register(tmp_path, harness=["claude-code"])
    assert rc == 0

    skill_md = tmp_path / ".claude" / "skills" / "strata" / "Skill.md"
    edited = "# My own custom skill notes\nI changed this on purpose.\n"
    skill_md.write_text(edited, encoding="utf-8")

    rc2, out2 = _register(tmp_path, harness=["claude-code"])

    assert rc2 == 0
    assert skill_md.read_text(encoding="utf-8") == edited  # untouched
    assert "kept user's" in out2
    assert "differs from shipped" in out2
    assert "strata register --diff" in out2


# ---------------------------------------------------------------------------
# Stop-hook script — three-state resolution
# ---------------------------------------------------------------------------


def test_hook_matching_shipped_is_skipped(tmp_path: Path) -> None:
    _init_project(tmp_path)
    rc, _ = _register(tmp_path, harness=["claude-code"])
    assert rc == 0

    hook_script = tmp_path / ".claude" / "hooks" / install.HOOK_SCRIPT_NAME
    before = hook_script.read_text(encoding="utf-8")

    rc2, out2 = _register(tmp_path, harness=["claude-code"])
    assert rc2 == 0
    assert hook_script.read_text(encoding="utf-8") == before
    assert "updated:" not in out2


def test_hook_stale_historical_content_is_self_updated(tmp_path: Path) -> None:
    _init_project(tmp_path)
    rc, _ = _register(tmp_path, harness=["claude-code"])
    assert rc == 0

    hook_script = tmp_path / ".claude" / "hooks" / install.HOOK_SCRIPT_NAME
    synthetic_old = "#!/bin/sh\n# old stop hook\nexit 0\n"
    synthetic_hash = hashlib.sha256(synthetic_old.encode("utf-8")).hexdigest()
    hook_script.write_text(synthetic_old, encoding="utf-8")

    real_hashes = install._HISTORICAL_ARTIFACT_HASHES  # noqa: SLF001
    patched = {
        **real_hashes,
        "strata-stop-hook": {
            "current": real_hashes["strata-stop-hook"]["current"],
            "historical": frozenset({synthetic_hash}),
        },
    }
    import unittest.mock

    with unittest.mock.patch.object(install, "_HISTORICAL_ARTIFACT_HASHES", patched):
        rc2, out2 = _register(tmp_path, harness=["claude-code"])

    assert rc2 == 0
    assert hook_script.read_text(encoding="utf-8") == _shipped_hook_text()
    assert "updated:" in out2
    assert "shipped content changed" in out2
    # Executable bit is re-applied by self-update, matching copy_hook.
    assert hook_script.stat().st_mode & 0o111


def test_hook_edited_content_is_kept_with_note(tmp_path: Path) -> None:
    _init_project(tmp_path)
    rc, _ = _register(tmp_path, harness=["claude-code"])
    assert rc == 0

    hook_script = tmp_path / ".claude" / "hooks" / install.HOOK_SCRIPT_NAME
    edited = "#!/bin/sh\n# hand-edited by the user\nexit 0\n"
    hook_script.write_text(edited, encoding="utf-8")

    rc2, out2 = _register(tmp_path, harness=["claude-code"])

    assert rc2 == 0
    assert hook_script.read_text(encoding="utf-8") == edited
    assert "kept user's" in out2
    assert "differs from shipped" in out2


# ---------------------------------------------------------------------------
# AGENTS.md block — three-state, block-level resolution
# ---------------------------------------------------------------------------


@pytest.fixture()
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "codex_home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def test_agents_md_matching_shipped_is_skipped(tmp_path: Path, codex_home: Path) -> None:
    _init_project(tmp_path)
    rc, _ = _register(tmp_path, harness=["codex"])
    assert rc == 0

    agents_md = tmp_path / "AGENTS.md"
    before = agents_md.read_bytes()

    rc2, out2 = _register(tmp_path, harness=["codex"])
    assert rc2 == 0
    assert agents_md.read_bytes() == before
    assert "updated:" not in out2
    # This is the discriminating assertion: an "edited" block also leaves
    # the file byte-unchanged (register never overwrites case 3 either) and
    # never prints "updated:" — so without this, the test above can't tell
    # "match" apart from "edited". Only a genuine match skips silently, with
    # no diff-from-shipped note attached to the AGENTS.md line.
    assert "differs from shipped" not in out2


def test_agents_md_stale_block_is_self_updated_preserving_outside_content(
    tmp_path: Path, codex_home: Path
) -> None:
    """Case 2 at block granularity: only the fenced block is replaced —
    everything the user wrote outside it survives byte-for-byte."""
    _init_project(tmp_path)
    rc, _ = _register(tmp_path, harness=["codex"])
    assert rc == 0

    agents_md = tmp_path / "AGENTS.md"
    existing = agents_md.read_bytes().decode("utf-8")

    # Wrap the managed block in the user's own content on both sides, and
    # swap the block's interior for synthetic "historical" content bounded
    # by the same begin/end markers.
    user_before = "# My project\n\nSome notes I wrote.\n\n"
    user_after = "\n\n# More of my own notes\nDo not touch this.\n"
    synthetic_block = (
        f"{install.AGENTS_MD_MARKER}\n"
        "## Strata memory (old wording)\n"
        "This is stale, pre-refresh guidance.\n"
        f"{install.AGENTS_MD_END_MARKER}\n"
    )
    synthetic_hash = hashlib.sha256(synthetic_block.encode("utf-8")).hexdigest()
    new_text = user_before + synthetic_block + user_after
    agents_md.write_bytes(new_text.encode("utf-8"))
    assert existing  # sanity: register really did write something before

    real_hashes = install._HISTORICAL_ARTIFACT_HASHES  # noqa: SLF001
    patched = {
        **real_hashes,
        "agents-md": {
            "current": real_hashes["agents-md"]["current"],
            "historical": frozenset({synthetic_hash}),
        },
    }
    import unittest.mock

    with unittest.mock.patch.object(install, "_HISTORICAL_ARTIFACT_HASHES", patched):
        rc2, out2 = _register(tmp_path, harness=["codex"])

    assert rc2 == 0
    final_text = agents_md.read_bytes().decode("utf-8")
    assert final_text.startswith(user_before)
    assert final_text.endswith(user_after)
    assert _shipped_agents_block() in final_text
    assert synthetic_block not in final_text
    assert "updated:" in out2
    assert "shipped content changed" in out2


def test_agents_md_edited_block_is_kept_with_note(tmp_path: Path, codex_home: Path) -> None:
    _init_project(tmp_path)
    rc, _ = _register(tmp_path, harness=["codex"])
    assert rc == 0

    agents_md = tmp_path / "AGENTS.md"
    edited = (
        f"{install.AGENTS_MD_MARKER}\n"
        "## Strata memory (I rewrote this)\n"
        "My own custom wording.\n"
        f"{install.AGENTS_MD_END_MARKER}\n"
    )
    agents_md.write_bytes(edited.encode("utf-8"))

    rc2, out2 = _register(tmp_path, harness=["codex"])

    assert rc2 == 0
    assert agents_md.read_bytes().decode("utf-8") == edited
    assert "kept user's" in out2
    assert "differs from shipped" in out2


# ---------------------------------------------------------------------------
# Idempotence: a second register after a self-update reports nothing left.
# ---------------------------------------------------------------------------


def test_second_register_after_self_update_is_all_skips(tmp_path: Path, codex_home: Path) -> None:
    _init_project(tmp_path)
    rc, _ = _register(tmp_path, harness=["claude-code", "codex"])
    assert rc == 0

    skill_md = tmp_path / ".claude" / "skills" / "strata" / "Skill.md"
    hook_script = tmp_path / ".claude" / "hooks" / install.HOOK_SCRIPT_NAME
    agents_md = tmp_path / "AGENTS.md"

    synthetic_skill = "# stale skill\n"
    synthetic_hook = "#!/bin/sh\n# stale hook\nexit 0\n"
    skill_md.write_text(synthetic_skill, encoding="utf-8")
    hook_script.write_text(synthetic_hook, encoding="utf-8")

    real_hashes = install._HISTORICAL_ARTIFACT_HASHES  # noqa: SLF001
    patched = {
        **real_hashes,
        "strata": {
            "current": real_hashes["strata"]["current"],
            "historical": frozenset({hashlib.sha256(synthetic_skill.encode()).hexdigest()}),
        },
        "strata-stop-hook": {
            "current": real_hashes["strata-stop-hook"]["current"],
            "historical": frozenset({hashlib.sha256(synthetic_hook.encode()).hexdigest()}),
        },
    }
    import unittest.mock

    with unittest.mock.patch.object(install, "_HISTORICAL_ARTIFACT_HASHES", patched):
        rc2, out2 = _register(tmp_path, harness=["claude-code", "codex"])
        assert rc2 == 0
        assert "updated:" in out2  # sanity: the self-update actually fired

        # Second run, same patched table: everything should now match shipped.
        rc3, out3 = _register(tmp_path, harness=["claude-code", "codex"])

    assert rc3 == 0
    assert "updated:" not in out3
    assert "differs from shipped" not in out3
    assert skill_md.read_text(encoding="utf-8") == _shipped_skill_text("strata")
    assert hook_script.read_text(encoding="utf-8") == _shipped_hook_text()
    assert agents_md.exists()


# ---------------------------------------------------------------------------
# Release discipline: the historical-hash table can never silently go stale.
# ---------------------------------------------------------------------------


def test_release_discipline_hashes_are_current() -> None:
    """The ``"current"`` hash recorded for every shipped artifact must match
    what's actually shipped right now.

    This is the guardrail described in ``_HISTORICAL_ARTIFACT_HASHES``'s
    maintenance comment: change a shipped artifact's content without
    updating this table, and this test fails the build. It can't catch a
    forgotten "move old current into historical" (that's still on the
    developer), but it makes forgetting the "current" bump itself
    impossible.
    """
    for skill_name in install.SKILL_NAMES:
        shipped_hash = hashlib.sha256(_shipped_skill_text(skill_name).encode("utf-8")).hexdigest()
        recorded = install._HISTORICAL_ARTIFACT_HASHES[skill_name]["current"]  # noqa: SLF001
        assert shipped_hash == recorded, (
            f"shipped '{skill_name}' Skill.md content changed but "
            "_HISTORICAL_ARTIFACT_HASHES was not updated — move the old "
            "'current' hash into 'historical' and record the new 'current'."
        )

    hook_hash = hashlib.sha256(_shipped_hook_text().encode("utf-8")).hexdigest()
    recorded_hook = install._HISTORICAL_ARTIFACT_HASHES["strata-stop-hook"]["current"]  # noqa: SLF001
    assert hook_hash == recorded_hook, (
        "shipped strata-stop-hook content changed but _HISTORICAL_ARTIFACT_HASHES "
        "was not updated — move the old 'current' hash into 'historical' and "
        "record the new 'current'."
    )

    agents_hash = hashlib.sha256(_shipped_agents_block().encode("utf-8")).hexdigest()
    recorded_agents = install._HISTORICAL_ARTIFACT_HASHES["agents-md"]["current"]  # noqa: SLF001
    assert agents_hash == recorded_agents, (
        "shipped AGENTS-strata.md content changed but _HISTORICAL_ARTIFACT_HASHES "
        "was not updated — move the old 'current' hash into 'historical' and "
        "record the new 'current'."
    )

    # Round-4 unregister fix, bug B: the Codex config.toml blocks aren't
    # self-updated, but remove_codex_mcp_server / remove_codex_freshness_hook
    # now recognize historical shipped text the same way the artifacts above
    # recognize historical shipped hashes — this table entry, and this
    # assertion, are what force a future content change to be a deliberate,
    # documented decision instead of a silent regression of bug B.
    codex_mcp_hash = hashlib.sha256(install.CODEX_MCP_BLOCK.encode("utf-8")).hexdigest()
    recorded_codex_mcp = install._HISTORICAL_ARTIFACT_HASHES["codex-mcp"]["current"]  # noqa: SLF001
    assert codex_mcp_hash == recorded_codex_mcp, (
        "shipped CODEX_MCP_BLOCK content changed but _HISTORICAL_ARTIFACT_HASHES "
        "was not updated — move the old 'current' hash into 'historical', add the "
        "old block text to CODEX_MCP_BLOCK_HISTORICAL, and record the new 'current'."
    )

    codex_hook_hash = hashlib.sha256(install.CODEX_HOOK_BLOCK.encode("utf-8")).hexdigest()
    recorded_codex_hook = install._HISTORICAL_ARTIFACT_HASHES["codex-hook"]["current"]  # noqa: SLF001
    assert codex_hook_hash == recorded_codex_hook, (
        "shipped CODEX_HOOK_BLOCK content changed but _HISTORICAL_ARTIFACT_HASHES "
        "was not updated — move the old 'current' hash into 'historical', add the "
        "old block text to CODEX_HOOK_BLOCK_HISTORICAL, and record the new 'current'."
    )


def test_release_discipline_catches_a_codex_block_content_change() -> None:
    """Proves the new codex-mcp/codex-hook guardrail is red-on-change, not
    just green-by-construction: modifying CODEX_MCP_BLOCK without updating
    the recorded hash must fail test_release_discipline_hashes_are_current.
    """
    modified = install.CODEX_MCP_BLOCK.replace('command = "strata-mcp"', 'command = "changed"')
    modified_hash = hashlib.sha256(modified.encode("utf-8")).hexdigest()
    recorded = install._HISTORICAL_ARTIFACT_HASHES["codex-mcp"]["current"]  # noqa: SLF001
    assert modified_hash != recorded  # red: an unrecorded change is detected

    unmodified_hash = hashlib.sha256(install.CODEX_MCP_BLOCK.encode("utf-8")).hexdigest()
    assert unmodified_hash == recorded  # green: reverting restores the match


# ---------------------------------------------------------------------------
# Unregister interplay: self-update then unregister still removes cleanly.
# ---------------------------------------------------------------------------


def test_unregister_after_self_update_removes_cleanly(tmp_path: Path, codex_home: Path) -> None:
    """register → self-update path → unregister must still remove every
    managed artifact — unregister's byte-match rule compares against
    *current* shipped content, which is exactly what self-update just wrote.
    """
    _init_project(tmp_path)
    rc, _ = _register(tmp_path, harness=["claude-code", "codex"])
    assert rc == 0

    skill_md = tmp_path / ".claude" / "skills" / "strata" / "Skill.md"
    hook_script = tmp_path / ".claude" / "hooks" / install.HOOK_SCRIPT_NAME
    agents_md = tmp_path / "AGENTS.md"

    synthetic_skill = "# stale skill\n"
    synthetic_hook = "#!/bin/sh\n# stale hook\nexit 0\n"
    skill_md.write_text(synthetic_skill, encoding="utf-8")
    hook_script.write_text(synthetic_hook, encoding="utf-8")

    real_hashes = install._HISTORICAL_ARTIFACT_HASHES  # noqa: SLF001
    patched = {
        **real_hashes,
        "strata": {
            "current": real_hashes["strata"]["current"],
            "historical": frozenset({hashlib.sha256(synthetic_skill.encode()).hexdigest()}),
        },
        "strata-stop-hook": {
            "current": real_hashes["strata-stop-hook"]["current"],
            "historical": frozenset({hashlib.sha256(synthetic_hook.encode()).hexdigest()}),
        },
    }
    import unittest.mock

    with unittest.mock.patch.object(install, "_HISTORICAL_ARTIFACT_HASHES", patched):
        rc2, out2 = _register(tmp_path, harness=["claude-code", "codex"])
        assert rc2 == 0
        assert "updated:" in out2

    # Self-updated content now equals current shipped — unregister (which
    # never depends on the patched table, just byte-equality with what's
    # actually shipped) should remove it without complaint.
    rc3 = _unregister(tmp_path, harness=["claude-code", "codex"])
    assert rc3 == 0
    assert not skill_md.exists()
    assert not hook_script.exists()
    # AGENTS.md itself is user-owned (it may have existed before register);
    # unregister only ever strips the managed block, never deletes the file
    # — same "removed" outcome as the other artifacts, at block granularity.
    assert install.AGENTS_MD_MARKER not in agents_md.read_text(encoding="utf-8")
