"""Tests for AGENTS.md seeding (Task 6, harness parity).

Codex has no skills mechanism (unlike Claude Code's `.claude/skills/`), so
`strata register --harness codex` seeds the same "read before working,
contribute what the next agent needs, expect the judge's verdict" guidance
into the project's own AGENTS.md instead — additively, idempotently, and
reversibly, mirroring the `.gitignore` / `config.toml` marker-block
conventions elsewhere in `strata.install`.

Vocabulary follows CONTEXT.md: scope, fleet, skill, scope-manager.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata import install  # noqa: E402
from strata.__main__ import cmd_register, cmd_unregister  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_project(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()


def _register(
    tmp_path: Path,
    *,
    harness: str = "codex",
    diff: bool = False,
    bootstrap_venv: bool = False,
) -> int:
    return cmd_register(
        argparse.Namespace(
            path=str(tmp_path),
            diff=diff,
            bootstrap_venv=bootstrap_venv,
            harness=[harness],
        )
    )


def _unregister(tmp_path: Path, *, harness: str = "codex", dry_run: bool = False) -> int:
    return cmd_unregister(
        argparse.Namespace(
            path=str(tmp_path),
            dry_run=dry_run,
            purge_data=False,
            harness=[harness],
        )
    )


@pytest.fixture()
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate $CODEX_HOME so tests never touch a real ~/.codex."""
    home = tmp_path / "codex_home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# install.merge_agents_md / remove_agents_md — unit-level marker semantics
# ---------------------------------------------------------------------------


def test_agents_md_marker_absent_on_empty() -> None:
    assert install.agents_md_present("") is False


def test_merge_agents_md_into_empty_file() -> None:
    text, added = install.merge_agents_md("")
    assert added is True
    assert install.AGENTS_MD_MARKER in text
    assert "<!-- strata:end -->" in text
    assert install.agents_md_present(text) is True


def test_merge_agents_md_is_idempotent() -> None:
    text, _ = install.merge_agents_md("")
    text2, added2 = install.merge_agents_md(text)
    assert added2 is False
    assert text2 == text  # byte-stable re-merge


def test_merge_agents_md_preserves_existing_user_content_byte_identical() -> None:
    existing = "# My Project\n\nSome hand-written agent instructions here.\n"
    text, added = install.merge_agents_md(existing)
    assert added is True
    assert text.startswith(existing)
    assert install.AGENTS_MD_MARKER in text


def test_remove_agents_md_round_trips_a_fresh_file() -> None:
    text, _ = install.merge_agents_md("")
    new_text, status = install.remove_agents_md(text)
    assert status == "removed"
    assert new_text == ""


def test_remove_agents_md_round_trips_users_preexisting_content() -> None:
    existing = "# My Project\n\nSome hand-written agent instructions here.\n"
    text, _ = install.merge_agents_md(existing)
    new_text, status = install.remove_agents_md(text)
    assert status == "removed"
    assert new_text == existing  # byte-identical round-trip


def test_remove_agents_md_absent_when_no_marker() -> None:
    new_text, status = install.remove_agents_md("# My Project\n")
    assert status == "absent"
    assert new_text == "# My Project\n"


def test_remove_agents_md_leaves_edited_block() -> None:
    text, _ = install.merge_agents_md("")
    edited = text.replace("Read before working", "Read before working (edited)")
    new_text, status = install.remove_agents_md(edited)
    assert status == "edited"
    assert new_text == edited  # left in place, untouched


# ---------------------------------------------------------------------------
# strata register --harness codex — AGENTS.md integration
# ---------------------------------------------------------------------------


def test_register_harness_codex_creates_agents_md(tmp_path: Path, codex_home: Path) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path, harness="codex") == 0

    agents_md = tmp_path / "AGENTS.md"
    assert agents_md.exists()
    assert install.agents_md_present(agents_md.read_text(encoding="utf-8"))


def test_register_harness_codex_appends_to_existing_agents_md(
    tmp_path: Path, codex_home: Path
) -> None:
    _init_project(tmp_path)
    agents_md = tmp_path / "AGENTS.md"
    existing = "# Project agent notes\n\nDo not touch prod.\n"
    agents_md.write_text(existing, encoding="utf-8")

    assert _register(tmp_path, harness="codex") == 0

    text = agents_md.read_text(encoding="utf-8")
    assert text.startswith(existing)
    assert install.agents_md_present(text)


def test_register_harness_codex_agents_md_is_idempotent(tmp_path: Path, codex_home: Path) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path, harness="codex") == 0
    agents_md = tmp_path / "AGENTS.md"
    before = agents_md.read_text(encoding="utf-8")

    assert _register(tmp_path, harness="codex") == 0
    after = agents_md.read_text(encoding="utf-8")
    assert after == before  # no duplicate blocks on re-run


def test_register_harness_codex_diff_mode_does_not_write_agents_md(
    tmp_path: Path, codex_home: Path
) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path, harness="codex", diff=True) == 0
    assert not (tmp_path / "AGENTS.md").exists()


def test_register_harness_claude_code_does_not_touch_agents_md(tmp_path: Path) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path, harness="claude-code") == 0
    assert not (tmp_path / "AGENTS.md").exists()


# ---------------------------------------------------------------------------
# strata unregister --harness codex — AGENTS.md integration
# ---------------------------------------------------------------------------


def test_unregister_harness_codex_removes_agents_md_block(tmp_path: Path, codex_home: Path) -> None:
    _init_project(tmp_path)
    _register(tmp_path, harness="codex")
    assert _unregister(tmp_path, harness="codex") == 0

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert install.agents_md_present(text) is False


def test_unregister_harness_codex_round_trips_users_preexisting_agents_md(
    tmp_path: Path, codex_home: Path
) -> None:
    _init_project(tmp_path)
    agents_md = tmp_path / "AGENTS.md"
    original = "# Project agent notes\n\nDo not touch prod.\n"
    agents_md.write_text(original, encoding="utf-8")

    _register(tmp_path, harness="codex")
    assert _unregister(tmp_path, harness="codex") == 0

    assert agents_md.read_text(encoding="utf-8") == original  # byte-identical round-trip


def test_unregister_harness_codex_leaves_edited_agents_md_block_and_exits_1(
    tmp_path: Path, codex_home: Path
) -> None:
    _init_project(tmp_path)
    _register(tmp_path, harness="codex")
    agents_md = tmp_path / "AGENTS.md"
    edited = agents_md.read_text(encoding="utf-8").replace(
        "Read before working", "Read before working (edited)"
    )
    agents_md.write_text(edited, encoding="utf-8")

    assert _unregister(tmp_path, harness="codex") == 1
    remaining = agents_md.read_text(encoding="utf-8")
    assert remaining == edited  # left in place, untouched


def test_unregister_harness_codex_agents_md_absent_is_noop(
    tmp_path: Path, codex_home: Path
) -> None:
    _init_project(tmp_path)
    assert _unregister(tmp_path, harness="codex") == 0
    assert not (tmp_path / "AGENTS.md").exists()


def test_unregister_harness_codex_dry_run_writes_nothing_to_agents_md(
    tmp_path: Path, codex_home: Path
) -> None:
    _init_project(tmp_path)
    _register(tmp_path, harness="codex")
    agents_md = tmp_path / "AGENTS.md"
    before = agents_md.read_text(encoding="utf-8")

    assert _unregister(tmp_path, harness="codex", dry_run=True) == 0
    assert agents_md.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# CRLF round-trip through the real CLI (final fix wave, item 2).
#
# Path.read_text/write_text universal-newline translation strips \r before
# any CRLF-aware code runs, so an on-disk CRLF AGENTS.md silently came back
# all-LF (or mixed) through register/unregister. The three in-scope I/O
# sites must read/write raw bytes instead.
# ---------------------------------------------------------------------------


def test_register_preserves_on_disk_crlf_agents_md(tmp_path: Path, codex_home: Path) -> None:
    _init_project(tmp_path)
    agents_md = tmp_path / "AGENTS.md"
    crlf_text = "# My project\r\n\r\nSome existing notes.\r\n"
    agents_md.write_bytes(crlf_text.encode("utf-8"))

    assert _register(tmp_path, harness="codex") == 0

    raw = agents_md.read_bytes()
    # The user's own pre-existing lines must still be CRLF-terminated.
    assert b"# My project\r\n\r\nSome existing notes.\r\n" in raw
    assert install.agents_md_present(raw.decode("utf-8"))


def test_unregister_preserves_on_disk_crlf_agents_md(tmp_path: Path, codex_home: Path) -> None:
    _init_project(tmp_path)
    agents_md = tmp_path / "AGENTS.md"
    # A CRLF-authored AGENTS.md with the user's own pre-existing content,
    # then register's own LF-style shipped block appended on top of it
    # (merge_agents_md/_append_block don't rewrite the file's own newline
    # style — only set_default_harness's TOML writer does). This is what a
    # real Windows-authored AGENTS.md + `register --harness codex` produces.
    user_crlf = "# My project\r\n\r\nSome existing notes.\r\n"
    shipped_block = install.merge_agents_md("")[0]
    agents_md.write_bytes((user_crlf + "\n" + shipped_block).encode("utf-8"))

    assert _unregister(tmp_path, harness="codex") == 0

    raw = agents_md.read_bytes()
    assert not install.agents_md_present(raw.decode("utf-8"))
    # The user's own CRLF-terminated lines must survive byte-for-byte.
    assert b"# My project\r\n\r\nSome existing notes.\r\n" in raw


# ---------------------------------------------------------------------------
# Template content — plain-language guardrails (no consumer names)
# ---------------------------------------------------------------------------


def test_agents_md_template_names_no_consumer() -> None:
    text, _ = install.merge_agents_md("")
    lowered = text.lower()
    for consumer_name in ("claude", "codex", "anthropic", "openai", "chatgpt"):
        assert consumer_name not in lowered


def test_agents_md_template_covers_the_three_memory_moves() -> None:
    text, _ = install.merge_agents_md("")
    lowered = text.lower()
    assert "read before working" in lowered
    assert "contribute" in lowered
    assert "judge" in lowered or "scope-manager" in lowered


def test_agents_md_template_mentions_env_binding() -> None:
    text, _ = install.merge_agents_md("")
    assert "STRATA_AGENT_SCOPE" in text
    assert "STRATA_AGENT_SKILL" in text
    assert "STRATA_AGENT_SESSION_ID" in text
