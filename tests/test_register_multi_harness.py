"""Tests for `strata register` fanning out over all detected harnesses (Task 2).

`strata register` used to wire exactly one harness (Claude Code by default,
or `--harness codex`). It now resolves a *set* of harnesses to wire:

- explicit `--harness` flags (repeatable) -> exactly those,
- no flags -> `strata.install.detect_harnesses()`,
- detection empty -> `["claude-code"]` plus a one-line notice (so a bare CI
  machine keeps today's behaviour).

Each resolved harness gets its own `== NAME ==` header line before its wiring
block; the common `.strata/` scaffolding still runs exactly once.

Mirrors the fixtures/idioms of tests/test_register_codex.py and
tests/test_install.py.

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

_NO_HARNESS_NOTICE = "no harness detected on this machine — wiring claude-code (the default)"


def _init_project(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()


def _register(
    tmp_path: Path,
    *,
    harness: list[str] | None = None,
    diff: bool = False,
) -> int:
    return cmd_register(
        argparse.Namespace(
            path=str(tmp_path),
            diff=diff,
            bootstrap_venv=False,
            harness=harness,
        )
    )


def _unregister(
    tmp_path: Path,
    *,
    harness: list[str] | None = None,
    dry_run: bool = False,
    purge_data: bool = False,
) -> int:
    return cmd_unregister(
        argparse.Namespace(
            path=str(tmp_path),
            dry_run=dry_run,
            purge_data=purge_data,
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
# (a) both-harness machine, plain `register` -> both wired
# ---------------------------------------------------------------------------


def test_plain_register_wires_every_detected_harness(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _init_project(tmp_path)
    monkeypatch.setattr(install, "detect_harnesses", lambda: ["claude-code", "codex"])

    assert _register(tmp_path) == 0

    settings = tmp_path / ".claude" / "settings.json"
    assert settings.exists()
    assert install.mcp_server_present(
        __import__("json").loads(settings.read_text(encoding="utf-8"))
    )

    codex_config = codex_home / "config.toml"
    assert codex_config.exists()
    assert install.codex_mcp_present(codex_config.read_text(encoding="utf-8"))

    out = capsys.readouterr().out
    assert "== claude-code ==" in out
    assert "== codex ==" in out


def test_plain_register_prints_headers_even_single_harness(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _init_project(tmp_path)
    monkeypatch.setattr(install, "detect_harnesses", lambda: ["claude-code"])

    assert _register(tmp_path) == 0

    out = capsys.readouterr().out
    assert "== claude-code ==" in out
    assert "== codex ==" not in out


# ---------------------------------------------------------------------------
# (b) --harness codex only -> .claude/settings.json untouched
# ---------------------------------------------------------------------------


def test_explicit_harness_codex_only_leaves_claude_settings_untouched(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_project(tmp_path)
    # Detection would find both if consulted — explicit flags must win outright.
    monkeypatch.setattr(install, "detect_harnesses", lambda: ["claude-code", "codex"])

    assert _register(tmp_path, harness=["codex"]) == 0

    assert not (tmp_path / ".claude" / "settings.json").exists()
    codex_config = codex_home / "config.toml"
    assert install.codex_mcp_present(codex_config.read_text(encoding="utf-8"))


def test_explicit_harness_claude_code_only_leaves_codex_untouched(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_project(tmp_path)
    monkeypatch.setattr(install, "detect_harnesses", lambda: ["claude-code", "codex"])

    assert _register(tmp_path, harness=["claude-code"]) == 0

    assert (tmp_path / ".claude" / "settings.json").exists()
    assert not (codex_home / "config.toml").exists()


# ---------------------------------------------------------------------------
# (c) bare machine -> claude-code wired + notice in output
# ---------------------------------------------------------------------------


def test_bare_machine_falls_back_to_claude_code_with_notice(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _init_project(tmp_path)
    monkeypatch.setattr(install, "detect_harnesses", lambda: [])

    assert _register(tmp_path) == 0

    assert (tmp_path / ".claude" / "settings.json").exists()
    assert not (codex_home / "config.toml").exists()

    out = capsys.readouterr().out
    assert _NO_HARNESS_NOTICE in out
    assert "== claude-code ==" in out


# ---------------------------------------------------------------------------
# (d) re-run is idempotent per harness (all skip lines)
# ---------------------------------------------------------------------------


def test_rerun_both_harnesses_is_idempotent(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _init_project(tmp_path)
    monkeypatch.setattr(install, "detect_harnesses", lambda: ["claude-code", "codex"])

    assert _register(tmp_path) == 0
    capsys.readouterr()  # discard first-run output

    assert _register(tmp_path) == 0
    out = capsys.readouterr().out

    # Common scaffolding: skipped on the second run.
    assert "kept user's" in out
    settings = tmp_path / ".claude" / "settings.json"
    codex_config = codex_home / "config.toml"
    before_settings = settings.read_text(encoding="utf-8")
    before_codex = codex_config.read_text(encoding="utf-8")

    assert _register(tmp_path) == 0

    assert settings.read_text(encoding="utf-8") == before_settings
    assert codex_config.read_text(encoding="utf-8") == before_codex


# ---------------------------------------------------------------------------
# --diff reports per harness the same way
# ---------------------------------------------------------------------------


def test_diff_mode_reports_both_harnesses_without_writing(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _init_project(tmp_path)
    monkeypatch.setattr(install, "detect_harnesses", lambda: ["claude-code", "codex"])

    assert _register(tmp_path, diff=True) == 0

    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert not (codex_home / "config.toml").exists()

    out = capsys.readouterr().out
    assert "== claude-code ==" in out
    assert "== codex ==" in out


# ---------------------------------------------------------------------------
# Task 3: `strata unregister` symmetric — reverses every WIRED harness by
# default (not every detected one); explicit --harness narrows the same way.
# ---------------------------------------------------------------------------


# (a) register both -> plain unregister reverses both
# ---------------------------------------------------------------------------


def test_plain_unregister_reverses_every_wired_harness(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_project(tmp_path)
    monkeypatch.setattr(install, "detect_harnesses", lambda: ["claude-code", "codex"])
    assert _register(tmp_path) == 0

    assert _unregister(tmp_path, purge_data=True) == 0

    settings = tmp_path / ".claude" / "settings.json"
    if settings.exists():
        data = __import__("json").loads(settings.read_text(encoding="utf-8"))
        assert install.mcp_server_present(data) is False
        assert install.stop_hook_present(data) is False

    codex_config = codex_home / "config.toml"
    assert codex_config.exists()
    codex_text = codex_config.read_text(encoding="utf-8")
    assert install.codex_mcp_present(codex_text) is False
    assert install.codex_hook_present(codex_text) is False


def test_plain_unregister_round_trips_preexisting_user_content(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """register -> unregister on both harnesses restores pre-existing user
    content byte-identically (reuses the round-trip idiom from
    tests/test_register_codex.py).
    """
    _init_project(tmp_path)
    monkeypatch.setattr(install, "detect_harnesses", lambda: ["claude-code", "codex"])

    codex_home.mkdir(parents=True)
    original_codex = '[model]\nname = "gpt-5"\n\n[mcp_servers.other-tool]\ncommand = "other-bin"\n'
    (codex_home / "config.toml").write_text(original_codex, encoding="utf-8")

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    import json as _json

    original_settings = {
        "theme": "dark",
        "mcpServers": {"other-tool": {"command": "other-tool-bin"}},
    }
    (claude_dir / "settings.json").write_text(
        _json.dumps(original_settings, indent=2) + "\n", encoding="utf-8"
    )

    assert _register(tmp_path) == 0
    assert _unregister(tmp_path, purge_data=True) == 0

    assert (codex_home / "config.toml").read_text(encoding="utf-8") == original_codex
    on_disk_settings = _json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    assert on_disk_settings["theme"] == "dark"
    assert on_disk_settings["mcpServers"] == {"other-tool": {"command": "other-tool-bin"}}


# (b) --harness codex leaves claude wiring intact
# ---------------------------------------------------------------------------


def test_explicit_unregister_harness_codex_leaves_claude_wiring_intact(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_project(tmp_path)
    monkeypatch.setattr(install, "detect_harnesses", lambda: ["claude-code", "codex"])
    assert _register(tmp_path) == 0

    assert _unregister(tmp_path, harness=["codex"]) == 0

    settings = tmp_path / ".claude" / "settings.json"
    data = __import__("json").loads(settings.read_text(encoding="utf-8"))
    assert install.mcp_server_present(data) is True
    assert install.stop_hook_present(data) is True

    codex_config = codex_home / "config.toml"
    codex_text = codex_config.read_text(encoding="utf-8")
    assert install.codex_mcp_present(codex_text) is False
    assert install.codex_hook_present(codex_text) is False


# (c) unregister on a never-registered dir: skip lines, exit 0
# ---------------------------------------------------------------------------


def test_unregister_never_registered_dir_is_all_skip_lines(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _init_project(tmp_path)
    monkeypatch.setattr(install, "detect_harnesses", lambda: ["claude-code", "codex"])

    assert _unregister(tmp_path) == 0

    out = capsys.readouterr().out
    assert "nothing to do" in out.lower()


def test_unregister_named_but_unwired_harness_skips_with_notice(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A harness named explicitly but never wired prints a skip line, exit 0."""
    _init_project(tmp_path)
    monkeypatch.setattr(install, "detect_harnesses", lambda: ["claude-code"])
    assert _register(tmp_path, harness=["claude-code"]) == 0

    rc = _unregister(tmp_path, harness=["codex"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "codex" in out.lower()
    assert "not wired" in out.lower() or "nothing to do" in out.lower()
    # claude-code wiring untouched — codex-only unregister was requested.
    settings = tmp_path / ".claude" / "settings.json"
    data = __import__("json").loads(settings.read_text(encoding="utf-8"))
    assert install.mcp_server_present(data) is True


def test_unregister_corrupt_settings_json_is_not_downgraded_to_a_skip(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A corrupt .claude/settings.json must still surface as a real problem
    (exit 1) via the marker-driven default resolution — not be silently
    misreported as "nothing wired to reverse" (exit 0).
    """
    _init_project(tmp_path)
    monkeypatch.setattr(install, "detect_harnesses", lambda: [])
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("{not valid json", encoding="utf-8")

    rc = _unregister(tmp_path)

    assert rc == 1
    err = capsys.readouterr().err
    assert "not valid json" in err.lower()
