"""Tests for `strata set-default-harness` (Task 4).

`strata set-default-harness NAME` records which harness `strata launch`
starts by default, in a `[launch]` table under `.strata/config.toml`. It:

- validates NAME against `strata.install.KNOWN_HARNESSES` (exit 2 + the
  valid list otherwise),
- requires a registered workspace (`.strata/config.toml` present — exit 1 +
  guidance otherwise),
- read-modify-writes the TOML textually so every other line in the file
  (including a pre-existing `[launch]` table's other keys) survives
  byte-for-byte, and
- is idempotent: re-running replaces the value without duplicating the
  table or the key.

`strata.install.read_default_harness` (text -> value) and
`strata.project_config.read_default_harness` (project_root -> value) are
covered directly too — Task 5 (`strata launch`) consumes the latter.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata import install  # noqa: E402
from strata.__main__ import cmd_set_default_harness  # noqa: E402
from strata.project_config import (  # noqa: E402
    read_default_harness as _read_project_default_harness,
)


def _init_project(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()


def _register(tmp_path: Path) -> None:
    (tmp_path / ".strata").mkdir()
    (tmp_path / ".strata" / "config.toml").write_text(install.CONFIG_TOML, encoding="utf-8")


def _set_default_harness(tmp_path: Path, name: str) -> int:
    return cmd_set_default_harness(argparse.Namespace(path=str(tmp_path), harness_name=name))


class TestSetDefaultHarnessCli:
    def test_unregistered_dir_exits_1_with_guidance(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_project(tmp_path)
        rc = _set_default_harness(tmp_path, "codex")
        assert rc == 1
        err = capsys.readouterr().err
        assert "strata register" in err

    def test_unknown_harness_exits_2_with_valid_list(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_project(tmp_path)
        _register(tmp_path)
        rc = _set_default_harness(tmp_path, "bogus-harness")
        assert rc == 2
        err = capsys.readouterr().err
        assert "bogus-harness" in err
        for h in install.KNOWN_HARNESSES:
            assert h in err

    def test_write_then_read_round_trip(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_project(tmp_path)
        _register(tmp_path)
        rc = _set_default_harness(tmp_path, "codex")
        assert rc == 0
        out = capsys.readouterr().out
        assert "default harness: codex" in out
        assert "strata launch" in out
        assert _read_project_default_harness(tmp_path) == "codex"

    def test_existing_content_preserved_byte_for_byte_outside_launch_table(
        self, tmp_path: Path
    ) -> None:
        _init_project(tmp_path)
        _register(tmp_path)
        config_path = tmp_path / ".strata" / "config.toml"
        original = config_path.read_text(encoding="utf-8")
        rc = _set_default_harness(tmp_path, "claude-code")
        assert rc == 0
        new_text = config_path.read_text(encoding="utf-8")
        assert new_text.startswith(original)

    def test_rerun_replaces_value_without_duplicating_table(self, tmp_path: Path) -> None:
        _init_project(tmp_path)
        _register(tmp_path)
        assert _set_default_harness(tmp_path, "claude-code") == 0
        assert _set_default_harness(tmp_path, "codex") == 0
        config_path = tmp_path / ".strata" / "config.toml"
        text = config_path.read_text(encoding="utf-8")
        assert text.count("[launch]") == 1
        assert text.count("default_harness") == 1
        assert 'default_harness = "codex"' in text
        assert _read_project_default_harness(tmp_path) == "codex"


class TestInstallSetDefaultHarnessTextual:
    def test_no_launch_table_appends_one(self) -> None:
        text = install.CONFIG_TOML
        new_text = install.set_default_harness(text, "codex")
        assert new_text.startswith(text)
        assert "[launch]" in new_text
        assert 'default_harness = "codex"' in new_text

    def test_existing_launch_table_other_keys_preserved(self) -> None:
        text = install.CONFIG_TOML + "\n[launch]\nother_key = 1\n"
        new_text = install.set_default_harness(text, "codex")
        assert "other_key = 1" in new_text
        assert 'default_harness = "codex"' in new_text
        assert new_text.count("[launch]") == 1

    def test_replaces_existing_value_in_place(self) -> None:
        text = install.CONFIG_TOML + '\n[launch]\ndefault_harness = "claude-code"\n'
        new_text = install.set_default_harness(text, "codex")
        assert new_text.count("[launch]") == 1
        assert new_text.count("default_harness") == 1
        assert 'default_harness = "codex"' in new_text
        assert 'default_harness = "claude-code"' not in new_text


class TestSetDefaultHarnessCrlf:
    """Regression: a CRLF-authored config.toml (Windows) must round-trip
    without duplicating the [launch] table and without the write path
    silently rewriting other lines' CRLF endings to bare LF (review fix)."""

    def test_crlf_launch_table_matched_not_duplicated(self) -> None:
        text = 'foo = 1\r\n\r\n[launch]\r\ndefault_harness = "claude-code"\r\n'
        new_text = install.set_default_harness(text, "codex")
        assert new_text.count("[launch]") == 1
        assert new_text.count("default_harness") == 1
        assert 'default_harness = "codex"' in new_text
        assert 'default_harness = "claude-code"' not in new_text

    def test_crlf_run_twice_stays_single_table_single_key(self) -> None:
        text = 'foo = 1\r\n\r\n[launch]\r\ndefault_harness = "claude-code"\r\n'
        once = install.set_default_harness(text, "codex")
        twice = install.set_default_harness(once, "codex")
        assert twice.count("[launch]") == 1
        assert twice.count("default_harness") == 1

    def test_crlf_other_bytes_preserved(self) -> None:
        text = 'foo = 1\r\n\r\n[launch]\r\ndefault_harness = "claude-code"\r\n'
        new_text = install.set_default_harness(text, "codex")
        assert new_text.startswith("foo = 1\r\n\r\n[launch]\r\n")

    def test_crlf_no_bare_lf_introduced(self) -> None:
        """Every newline in a CRLF-only input stays CRLF in the output."""
        text = 'foo = 1\r\n\r\n[launch]\r\ndefault_harness = "claude-code"\r\n'
        new_text = install.set_default_harness(text, "codex")
        # Strip every CRLF pair; nothing but the pair-halves should remain,
        # i.e. no lone "\n" survives once every "\r\n" is removed.
        assert "\n" not in new_text.replace("\r\n", "")

    def test_crlf_appends_new_key_with_crlf(self) -> None:
        text = "foo = 1\r\n\r\n[launch]\r\nother_key = 1\r\n"
        new_text = install.set_default_harness(text, "codex")
        assert new_text.count("[launch]") == 1
        assert 'default_harness = "codex"' in new_text
        assert "\n" not in new_text.replace("\r\n", "")

    def test_crlf_no_launch_table_appends_with_crlf(self) -> None:
        text = "foo = 1\r\nbar = 2\r\n"
        new_text = install.set_default_harness(text, "codex")
        assert new_text.startswith(text)
        assert "[launch]" in new_text
        assert 'default_harness = "codex"' in new_text
        assert "\n" not in new_text.replace("\r\n", "")


class TestReadDefaultHarness:
    def test_absent_config_returns_none(self, tmp_path: Path) -> None:
        assert _read_project_default_harness(tmp_path) is None

    def test_registered_but_no_launch_table_returns_none(self, tmp_path: Path) -> None:
        _register(tmp_path)
        assert _read_project_default_harness(tmp_path) is None

    def test_malformed_toml_returns_none(self) -> None:
        assert install.read_default_harness("not [ valid toml") is None
