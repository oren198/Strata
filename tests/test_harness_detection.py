"""Tests for :func:`strata.install.detect_harnesses` (harness detection)."""

from __future__ import annotations

from strata.install import detect_harnesses


def test_detects_nothing_on_bare_machine(tmp_path):
    assert detect_harnesses(home=tmp_path, path_env=str(tmp_path)) == []


def test_detects_claude_by_home_dir(tmp_path):
    (tmp_path / ".claude").mkdir()
    assert detect_harnesses(home=tmp_path, path_env=str(tmp_path)) == ["claude-code"]


def test_detects_both_claude_first(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    assert detect_harnesses(home=tmp_path, path_env=str(tmp_path)) == [
        "claude-code",
        "codex",
    ]


def test_detects_codex_by_binary_on_path(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex_bin = bin_dir / "codex"
    codex_bin.write_text("#!/bin/sh\n")
    codex_bin.chmod(0o755)
    assert detect_harnesses(home=tmp_path, path_env=str(bin_dir)) == ["codex"]
