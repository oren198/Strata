"""Tests for `strata register --harness codex` (Task 6.2, local-launch-bar).

Verified surface only (docs/marketing/CODEX-surface-2026-08.md, 2026-08-23):

- MCP: Codex CLI reads `[mcp_servers.<name>]` tables from `config.toml` at
  `$CODEX_HOME/config.toml` (default ``~/.codex/config.toml``) — verified
  hands-on via `codex mcp add` round-tripping through `codex mcp list/get`
  on codex-cli 0.149.0.
- MCP env values are literal TOML strings — no ``${VAR}`` interpolation is
  documented, so the merged block ships empty placeholders the operator must
  fill in (or launch Codex with those vars already set and rely on the
  *unverified* env-inheritance path — see README).
- Hooks: the ``[[hooks.Stop]]`` / ``[[hooks.Stop.hooks]]`` TOML schema is
  accepted by `codex exec --strict-config` (schema-verified), but whether the
  hook actually fires and inherits the launching process's environment is
  NOT verified (no OpenAI credentials in the findings sandbox) — the merged
  block and this test suite treat that as "pending live verification" only.

Covers the additive/idempotent merge helpers in :mod:`strata.install`
(mirroring the Claude-Code Stop-hook merge tests' style in
tests/test_freshness_install.py) and their wiring into
`strata register --harness codex`.

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
    harness: str = "claude-code",
    diff: bool = False,
    bootstrap_venv: bool = False,
) -> int:
    # A parsed `--harness NAME` flag now yields a list (action="append");
    # wrap the single-harness convenience param the same way so these tests
    # exercise exactly the explicit-flags resolution path, never detection.
    return cmd_register(
        argparse.Namespace(
            path=str(tmp_path),
            diff=diff,
            bootstrap_venv=bootstrap_venv,
            harness=[harness],
        )
    )


def _unregister(tmp_path: Path, *, harness: str = "claude-code", dry_run: bool = False) -> int:
    # A parsed `--harness NAME` flag now yields a list (action="append"), same
    # shape as register's; wrap the single-harness convenience param the same
    # way so these tests exercise exactly the explicit-flags resolution path.
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
# install.codex_config_path — location resolution
# ---------------------------------------------------------------------------


def test_codex_config_path_respects_codex_home_env(codex_home: Path) -> None:
    assert install.codex_config_path() == codex_home / "config.toml"


def test_codex_config_path_defaults_to_home_dot_codex(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert install.codex_config_path() == tmp_path / ".codex" / "config.toml"


# ---------------------------------------------------------------------------
# install.merge_codex_mcp_server — additive TOML text merge
# ---------------------------------------------------------------------------


def test_codex_mcp_present_false_on_empty() -> None:
    assert install.codex_mcp_present("") is False


def test_merge_codex_mcp_server_into_empty_config() -> None:
    text, added = install.merge_codex_mcp_server("")
    assert added is True
    assert "[mcp_servers.strata]" in text
    assert 'command = "strata-mcp"' in text
    assert install.codex_mcp_present(text) is True


def test_merge_codex_mcp_server_is_idempotent() -> None:
    text, added = install.merge_codex_mcp_server("")
    text2, added2 = install.merge_codex_mcp_server(text)
    assert added2 is False
    assert text2 == text  # byte-stable re-merge


def test_merge_codex_mcp_server_preserves_existing_content() -> None:
    existing = '[mcp_servers.other-tool]\ncommand = "other-tool-bin"\n\n[model]\nname = "gpt-5"\n'
    text, added = install.merge_codex_mcp_server(existing)
    assert added is True
    assert "[mcp_servers.other-tool]" in text
    assert 'command = "other-tool-bin"' in text
    assert "[model]" in text
    assert 'name = "gpt-5"' in text
    assert "[mcp_servers.strata]" in text


def test_codex_mcp_present_detects_manual_codex_mcp_add_entry() -> None:
    # A user who already ran `codex mcp add strata ...` by hand should not
    # get a second, conflicting [mcp_servers.strata] table appended.
    manual = (
        "[mcp_servers.strata]\n"
        'command = "strata-mcp"\n\n'
        "[mcp_servers.strata.env]\n"
        'STRATA_AGENT_SCOPE = "g_root"\n'
    )
    assert install.codex_mcp_present(manual) is True
    text, added = install.merge_codex_mcp_server(manual)
    assert added is False
    assert text == manual  # untouched — the user's literal scope value survives


# ---------------------------------------------------------------------------
# install.merge_codex_freshness_hook — additive TOML text merge
# ---------------------------------------------------------------------------


def test_codex_hook_present_false_on_empty() -> None:
    assert install.codex_hook_present("") is False


def test_merge_codex_freshness_hook_into_empty_config() -> None:
    text, added = install.merge_codex_freshness_hook("")
    assert added is True
    assert "[[hooks.Stop]]" in text
    assert "[[hooks.Stop.hooks]]" in text
    assert 'command = "strata freshness-hook"' in text
    assert install.codex_hook_present(text) is True


def test_merge_codex_freshness_hook_is_idempotent() -> None:
    text, _ = install.merge_codex_freshness_hook("")
    text2, added2 = install.merge_codex_freshness_hook(text)
    assert added2 is False
    assert text2 == text


def test_merge_codex_freshness_hook_preserves_existing_hooks() -> None:
    existing = (
        '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "my-other-hook.sh"\n'
    )
    text, added = install.merge_codex_freshness_hook(existing)
    assert added is True
    assert 'command = "my-other-hook.sh"' in text  # user's hook untouched
    assert 'command = "strata freshness-hook"' in text  # ours appended


def test_merge_codex_freshness_hook_labelled_pending_verification() -> None:
    text, _ = install.merge_codex_freshness_hook("")
    # The merged block itself must self-document the unverified live-firing
    # gap so a user reading config.toml sees the caveat, not just the README.
    assert "not verified" in text.lower() or "pending" in text.lower()


# ---------------------------------------------------------------------------
# strata register --harness codex — integration
# ---------------------------------------------------------------------------


def test_register_harness_codex_writes_config_toml(tmp_path: Path, codex_home: Path) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path, harness="codex") == 0

    config = codex_home / "config.toml"
    assert config.exists()
    text = config.read_text(encoding="utf-8")
    assert install.codex_mcp_present(text)
    assert install.codex_hook_present(text)


def test_register_harness_codex_prints_progress_without_crashing(
    tmp_path: Path, codex_home: Path, capsys: pytest.CaptureFixture
) -> None:
    # Regression: codex_config_path() lives outside project_root (unlike
    # every other register artifact), so the progress-line renderer must not
    # assume it's a subpath of project_root (Path.relative_to raises if so).
    _init_project(tmp_path)
    assert _register(tmp_path, harness="codex") == 0
    out = capsys.readouterr().out
    assert str(codex_home / "config.toml") in out


def test_register_harness_codex_is_idempotent(tmp_path: Path, codex_home: Path) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path, harness="codex") == 0
    config = codex_home / "config.toml"
    before = config.read_text(encoding="utf-8")

    assert _register(tmp_path, harness="codex") == 0
    after = config.read_text(encoding="utf-8")
    assert after == before  # no duplicate tables on re-run


def test_register_harness_codex_preserves_user_codex_config(
    tmp_path: Path, codex_home: Path
) -> None:
    _init_project(tmp_path)
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        '[model]\nname = "gpt-5"\n\n[mcp_servers.other-tool]\ncommand = "other-bin"\n',
        encoding="utf-8",
    )

    assert _register(tmp_path, harness="codex") == 0

    text = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'name = "gpt-5"' in text
    assert "[mcp_servers.other-tool]" in text
    assert 'command = "other-bin"' in text
    assert install.codex_mcp_present(text)


def test_register_harness_codex_does_not_touch_claude_settings(
    tmp_path: Path, codex_home: Path
) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path, harness="codex") == 0
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / ".claude" / "hooks").exists()


def test_register_harness_codex_still_creates_strata_project_state(
    tmp_path: Path, codex_home: Path
) -> None:
    # The per-project .strata/ scaffold (config.toml, fleet.yaml, .gitignore
    # block) is harness-agnostic — a Codex-driven project still needs it.
    _init_project(tmp_path)
    assert _register(tmp_path, harness="codex") == 0
    assert (tmp_path / ".strata" / "config.toml").exists()
    assert (tmp_path / ".strata" / "fleet.yaml").exists()


def test_register_harness_codex_diff_mode_does_not_write(tmp_path: Path, codex_home: Path) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path, harness="codex", diff=True) == 0
    assert not (codex_home / "config.toml").exists()


def test_register_default_harness_is_claude_code(tmp_path: Path, codex_home: Path) -> None:
    # No --harness flag: unchanged Claude Code behaviour, codex config untouched.
    _init_project(tmp_path)
    assert _register(tmp_path) == 0
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert not (codex_home / "config.toml").exists()


def test_register_harness_codex_rejects_invalid_settings_json_independently(
    tmp_path: Path, codex_home: Path
) -> None:
    # Codex-harness register must not be blocked by an unrelated, broken
    # .claude/settings.json — that file is out of scope for this harness.
    _init_project(tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text("{not valid json", encoding="utf-8")

    assert _register(tmp_path, harness="codex") == 0
    assert install.codex_mcp_present((codex_home / "config.toml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# install.remove_codex_mcp_server / remove_codex_freshness_hook
# ---------------------------------------------------------------------------


def test_remove_codex_mcp_server_removes_canonical_block() -> None:
    text, _ = install.merge_codex_mcp_server("")
    new_text, status = install.remove_codex_mcp_server(text)
    assert status == "removed"
    assert install.codex_mcp_present(new_text) is False
    assert new_text == ""


def test_remove_codex_mcp_server_leaves_edited_block() -> None:
    text, _ = install.merge_codex_mcp_server("")
    edited = text.replace('command = "strata-mcp"', 'command = "strata-mcp-edited"')
    new_text, status = install.remove_codex_mcp_server(edited)
    assert status == "edited"
    assert new_text == edited  # left in place, untouched


def test_remove_codex_mcp_server_absent() -> None:
    new_text, status = install.remove_codex_mcp_server("")
    assert status == "absent"
    assert new_text == ""


def test_remove_codex_mcp_server_absent_when_register_left_a_users_manual_entry() -> None:
    # register never wrote our marker here (a manual `codex mcp add` entry
    # was already present, so merge_codex_mcp_server was a no-op) — unregister
    # must not touch it: nothing to remove, byte-identical round-trip.
    manual = (
        "[mcp_servers.strata]\n"
        'command = "strata-mcp"\n\n'
        "[mcp_servers.strata.env]\n"
        'STRATA_AGENT_SCOPE = "g_root"\n'
    )
    new_text, status = install.remove_codex_mcp_server(manual)
    assert status == "absent"
    assert new_text == manual


def test_remove_codex_mcp_server_preserves_other_content() -> None:
    existing = '[model]\nname = "gpt-5"\n'
    text, _ = install.merge_codex_mcp_server(existing)
    new_text, status = install.remove_codex_mcp_server(text)
    assert status == "removed"
    assert new_text == existing  # byte-identical round-trip


# ---------------------------------------------------------------------------
# install.strip_orphaned_mcp_strata_tables — round-4 unregister fix, bug A
#
# Live sequence this reproduces: register wrote [mcp_servers.strata] +
# [mcp_servers.strata.env]; the Codex CLI itself later appended its own
# [mcp_servers.strata.tools.<tool>] approval-state subtables during a live
# session; `strata unregister --harness codex` removed only the canonical
# block register wrote, leaving the tools.* subtables orphaned — Codex then
# failed startup with "invalid transport in mcp_servers.strata".
# ---------------------------------------------------------------------------


def test_strip_orphaned_mcp_strata_tables_removes_codex_appended_tool_subtables() -> None:
    # Simulates the parent block already having been removed by
    # remove_codex_mcp_server, leaving only what Codex itself appended.
    text = (
        "[mcp_servers.strata.tools.read_file]\n"
        "approved = true\n\n"
        "[mcp_servers.strata.tools.write_file]\n"
        "approved = false\n"
    )
    new_text, count = install.strip_orphaned_mcp_strata_tables(text)
    assert count == 2
    assert "mcp_servers.strata" not in new_text
    assert new_text == ""


def test_strip_orphaned_mcp_strata_tables_leaves_unrelated_tables_byte_identical() -> None:
    text = (
        '[model]\nname = "gpt-5"\n\n'
        "[mcp_servers.strata.tools.read_file]\n"
        "approved = true\n\n"
        '[mcp_servers.other-tool]\ncommand = "other-bin"\n'
    )
    new_text, count = install.strip_orphaned_mcp_strata_tables(text)
    assert count == 1
    assert "mcp_servers.strata" not in new_text
    assert (
        new_text == '[model]\nname = "gpt-5"\n\n[mcp_servers.other-tool]\ncommand = "other-bin"\n'
    )


def test_strip_orphaned_mcp_strata_tables_does_not_swallow_array_of_tables_header() -> None:
    # A [[hooks.Stop]] array-of-tables header (our own freshness-hook shape,
    # or a user's) sitting right after an orphaned strata subtable must end
    # that subtable's span, not get swallowed into it.
    text = (
        "[mcp_servers.strata.tools.read_file]\n"
        "approved = true\n"
        "[[hooks.Stop]]\n"
        "[[hooks.Stop.hooks]]\n"
        'type = "command"\n'
        'command = "my-hook.sh"\n'
    )
    new_text, count = install.strip_orphaned_mcp_strata_tables(text)
    assert count == 1
    assert "mcp_servers.strata" not in new_text
    assert (
        new_text
        == '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "my-hook.sh"\n'
    )


def test_strip_orphaned_mcp_strata_tables_does_not_swallow_our_own_hook_block() -> None:
    # CODEX_HOOK_BLOCK starts with comment lines (the marker + schema notes)
    # before its own [[hooks.Stop]] header. An orphaned strata subtable
    # sitting right before it must not swallow those comment lines into its
    # removed span — that would corrupt the still-canonical hook block and
    # break remove_codex_freshness_hook's own byte-match.
    text = "[mcp_servers.strata.tools.read_file]\napproved = true\n" + install.CODEX_HOOK_BLOCK
    new_text, count = install.strip_orphaned_mcp_strata_tables(text)
    assert count == 1
    assert "mcp_servers.strata" not in new_text
    assert new_text == install.CODEX_HOOK_BLOCK
    # The still-intact hook block remains removable by its own function.
    _, hook_status = install.remove_codex_freshness_hook(new_text)
    assert hook_status == "removed"


def test_strip_orphaned_mcp_strata_tables_absent_is_noop() -> None:
    text = '[model]\nname = "gpt-5"\n'
    new_text, count = install.strip_orphaned_mcp_strata_tables(text)
    assert count == 0
    assert new_text == text


def test_unregister_harness_codex_removes_orphaned_tool_subtables(
    tmp_path: Path, codex_home: Path
) -> None:
    """The exact live scenario: register, then Codex appends tool-approval
    subtables under our parent table, then unregister must remove the whole
    subtree — zero mcp_servers.strata references left, Codex startup fixed.
    """
    _init_project(tmp_path)
    _register(tmp_path, harness="codex")
    config = codex_home / "config.toml"
    # A user's own, unrelated table both before and after ours, to prove
    # unrelated content is byte-preserved through the sweep.
    before_text = '[model]\nname = "gpt-5"\n\n'
    after_text = '\n[mcp_servers.other-tool]\ncommand = "other-bin"\n'
    codex_appended = (
        "[mcp_servers.strata.tools.read_file]\n"
        "approved = true\n\n"
        "[mcp_servers.strata.tools.write_file]\n"
        "approved = false\n"
    )
    original = config.read_text(encoding="utf-8")
    config.write_text(before_text + original + codex_appended + after_text, encoding="utf-8")

    assert _unregister(tmp_path, harness="codex") == 0

    remaining = config.read_text(encoding="utf-8")
    assert "mcp_servers.strata" not in remaining
    assert "[model]" in remaining
    assert "[mcp_servers.other-tool]" in remaining


def test_unregister_harness_codex_manual_entry_with_subtables_fully_untouched(
    tmp_path: Path, codex_home: Path
) -> None:
    """A manually-created [mcp_servers.strata] entry (no register marker) —
    including any subtables under it — is never touched by unregister."""
    _init_project(tmp_path)
    codex_home.mkdir(parents=True)
    manual = (
        "[mcp_servers.strata]\n"
        'command = "strata-mcp"\n\n'
        "[mcp_servers.strata.env]\n"
        'STRATA_AGENT_SCOPE = "g_root"\n\n'
        "[mcp_servers.strata.tools.read_file]\n"
        "approved = true\n"
    )
    (codex_home / "config.toml").write_text(manual, encoding="utf-8")

    assert _unregister(tmp_path, harness="codex") == 0

    assert (codex_home / "config.toml").read_text(encoding="utf-8") == manual


def test_remove_codex_freshness_hook_removes_canonical_block() -> None:
    text, _ = install.merge_codex_freshness_hook("")
    new_text, status = install.remove_codex_freshness_hook(text)
    assert status == "removed"
    assert install.codex_hook_present(new_text) is False
    assert new_text == ""


def test_remove_codex_freshness_hook_leaves_edited_block() -> None:
    text, _ = install.merge_codex_freshness_hook("")
    edited = text.replace("timeout = 30", "timeout = 60")
    new_text, status = install.remove_codex_freshness_hook(edited)
    assert status == "edited"
    assert new_text == edited


def test_remove_codex_freshness_hook_absent() -> None:
    new_text, status = install.remove_codex_freshness_hook("")
    assert status == "absent"
    assert new_text == ""


def test_remove_codex_freshness_hook_preserves_users_own_stop_hook() -> None:
    user_hook = '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "my-hook.sh"\n'
    text, _ = install.merge_codex_freshness_hook(user_hook)
    new_text, status = install.remove_codex_freshness_hook(text)
    assert status == "removed"
    assert new_text == user_hook  # only ours removed, byte-identical round-trip


# ---------------------------------------------------------------------------
# strata unregister --harness codex — integration
# ---------------------------------------------------------------------------


def test_unregister_harness_codex_removes_canonical_wiring(
    tmp_path: Path, codex_home: Path
) -> None:
    _init_project(tmp_path)
    _register(tmp_path, harness="codex")
    assert _unregister(tmp_path, harness="codex") == 0

    text = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert install.codex_mcp_present(text) is False
    assert install.codex_hook_present(text) is False


def test_unregister_harness_codex_round_trips_a_users_preexisting_config(
    tmp_path: Path, codex_home: Path
) -> None:
    _init_project(tmp_path)
    codex_home.mkdir(parents=True)
    original = '[model]\nname = "gpt-5"\n\n[mcp_servers.other-tool]\ncommand = "other-bin"\n'
    (codex_home / "config.toml").write_text(original, encoding="utf-8")

    _register(tmp_path, harness="codex")
    assert _unregister(tmp_path, harness="codex") == 0

    text = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert text == original  # byte-identical round-trip


def test_unregister_harness_codex_leaves_edited_block_and_exits_1(
    tmp_path: Path, codex_home: Path
) -> None:
    # Editing only the mcp block: the (untouched) freshness-hook block is
    # still removed independently, mirroring the Claude-Code settings.json
    # unregister's per-entry granularity — only the edited entry is left.
    _init_project(tmp_path)
    _register(tmp_path, harness="codex")
    config = codex_home / "config.toml"
    edited = config.read_text(encoding="utf-8").replace(
        'command = "strata-mcp"', 'command = "strata-mcp-edited"'
    )
    config.write_text(edited, encoding="utf-8")

    assert _unregister(tmp_path, harness="codex") == 1
    remaining = config.read_text(encoding="utf-8")
    assert 'command = "strata-mcp-edited"' in remaining  # edited mcp block left in place
    assert install.codex_hook_present(remaining) is False  # unrelated hook block still removed


def test_unregister_harness_codex_absent_is_noop(tmp_path: Path, codex_home: Path) -> None:
    _init_project(tmp_path)
    assert _unregister(tmp_path, harness="codex") == 0
    assert not (codex_home / "config.toml").exists()


def test_unregister_harness_codex_does_not_touch_claude_settings(
    tmp_path: Path, codex_home: Path
) -> None:
    _init_project(tmp_path)
    _register(tmp_path)  # default (claude-code) wiring present
    _register(tmp_path, harness="codex")
    before = (tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8")

    assert _unregister(tmp_path, harness="codex") == 0

    after = (tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert after == before  # claude-code wiring untouched by a codex-harness unregister


def test_unregister_harness_codex_dry_run_writes_nothing(tmp_path: Path, codex_home: Path) -> None:
    _init_project(tmp_path)
    _register(tmp_path, harness="codex")
    before = (codex_home / "config.toml").read_text(encoding="utf-8")

    assert _unregister(tmp_path, harness="codex", dry_run=True) == 0

    assert (codex_home / "config.toml").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# --bootstrap-venv + --harness codex — notice, not a silent no-op
# ---------------------------------------------------------------------------


def test_register_bootstrap_venv_with_codex_harness_prints_skip_notice(
    tmp_path: Path, codex_home: Path, capsys: pytest.CaptureFixture
) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path, harness="codex", bootstrap_venv=True) == 0
    out = capsys.readouterr().out
    assert "--bootstrap-venv" in out
    assert "codex" in out.lower()
    assert not (tmp_path / ".strata" / ".venv").exists()


def test_register_bootstrap_venv_both_harnesses_notice_matches_behavior(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Regression (final fix wave, item 3): on a both-harness machine,
    --bootstrap-venv prints a "skipped" notice yet still creates the venv
    for claude-code — output and behavior disagreed. The notice must be
    scoped to codex specifically, and the venv must still actually get
    built for claude-code (real venv/pip calls are faked out here so the
    test stays fast and offline)."""
    import subprocess
    import venv as venv_module

    import strata.__main__ as main_mod

    _init_project(tmp_path)

    def _fake_venv_create(path: str, with_pip: bool = True, clear: bool = False) -> None:
        bin_dir = Path(path) / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "strata-mcp").write_text("#!/bin/sh\n", encoding="utf-8")

    # cmd_register does `import venv` / `import subprocess` locally, but that
    # binds the same real stdlib module objects — patching them here still
    # takes effect.
    monkeypatch.setattr(venv_module, "create", _fake_venv_create)
    monkeypatch.setattr(subprocess, "check_call", lambda *a, **k: 0)
    monkeypatch.setattr(main_mod, "_self_install_spec", lambda: "strata")

    rc = cmd_register(
        argparse.Namespace(
            path=str(tmp_path),
            diff=False,
            bootstrap_venv=True,
            harness=["codex", "claude-code"],
        )
    )
    assert rc == 0
    out = capsys.readouterr().out.lower()

    venv_bin = tmp_path / ".strata" / ".venv" / "bin" / "strata-mcp"
    assert venv_bin.exists(), "claude-code's venv must still be built alongside the codex notice"
    assert "creating .strata/.venv/" in out
    # The codex notice must read as codex-specific, not as "the whole
    # --bootstrap-venv step was skipped" — which the venv creation above
    # disproves.
    assert "skipping that step for codex only" in out
