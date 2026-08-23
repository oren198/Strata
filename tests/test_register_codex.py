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
from strata.__main__ import cmd_register  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_project(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()


def _register(
    tmp_path: Path, *, harness: str = "claude-code", diff: bool = False
) -> int:
    return cmd_register(
        argparse.Namespace(
            path=str(tmp_path),
            diff=diff,
            bootstrap_venv=False,
            harness=harness,
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
    existing = (
        '[mcp_servers.other-tool]\ncommand = "other-tool-bin"\n\n'
        "[model]\nname = \"gpt-5\"\n"
    )
    text, added = install.merge_codex_mcp_server(existing)
    assert added is True
    assert '[mcp_servers.other-tool]' in text
    assert 'command = "other-tool-bin"' in text
    assert '[model]' in text
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
        "[[hooks.Stop]]\n"
        "[[hooks.Stop.hooks]]\n"
        'type = "command"\n'
        'command = "my-other-hook.sh"\n'
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


def test_register_harness_codex_writes_config_toml(
    tmp_path: Path, codex_home: Path
) -> None:
    _init_project(tmp_path)
    assert _register(tmp_path, harness="codex") == 0

    config = codex_home / "config.toml"
    assert config.exists()
    text = config.read_text(encoding="utf-8")
    assert install.codex_mcp_present(text)
    assert install.codex_hook_present(text)


def test_register_harness_codex_is_idempotent(
    tmp_path: Path, codex_home: Path
) -> None:
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
    assert '[mcp_servers.other-tool]' in text
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


def test_register_harness_codex_diff_mode_does_not_write(
    tmp_path: Path, codex_home: Path
) -> None:
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
    assert install.codex_mcp_present(
        (codex_home / "config.toml").read_text(encoding="utf-8")
    )
