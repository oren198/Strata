"""Tests for codex "wiredness" being project-scoped, not machine-scoped.

Controller ruling (final fix wave, harness-parity review): `_codex_wired`
used to check only `$CODEX_HOME/config.toml` — a machine-level file shared
by every project on the box. That let a stale/foreign codex config leak
into a project that never registered codex here:

- a plain `strata unregister` (no --harness) would resolve codex as wired
  and strip the user-level config.toml tables another project relies on,
  contradicting README's "a plain unregister should never touch a harness
  this project never registered" promise;
- `strata launch`'s "exactly one harness wired" fallback would resolve
  codex in a claude-unwired project on such a machine, even though this
  project never touched codex.

Fix: for the no-flags/no-recorded-default resolution path (shared by
`_wired_harnesses`, which both `cmd_unregister`'s default and
`_resolve_launch_harness`'s single-wired fallback call), codex counts as
wired only when BOTH the machine config tables are present AND the
project-local AGENTS.md carries the strata marker block — the project-side
evidence codex was registered *here*. Explicit `--harness codex` is
untouched: it bypasses `_wired_harnesses` entirely (Task 3's
unconditional-bypass behavior for named harnesses), so it still cleans up
a foreign machine config on request.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata import install  # noqa: E402
from strata.__main__ import (  # noqa: E402
    _resolve_launch_harness,
    _wired_harnesses,
    cmd_register,
    cmd_unregister,
)


def _init_project(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()


def _unregister(tmp_path: Path, *, harness: list[str] | None = None) -> int:
    return cmd_unregister(
        argparse.Namespace(path=str(tmp_path), dry_run=False, purge_data=False, harness=harness)
    )


@pytest.fixture()
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate $CODEX_HOME so tests never touch a real ~/.codex."""
    home = tmp_path / "codex_home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _write_machine_codex_config(codex_home: Path) -> None:
    """Simulate a machine-level codex config wired by *some other* project."""
    text, _ = install.merge_codex_mcp_server("")
    text, _ = install.merge_codex_freshness_hook(text)
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# (i) project without the AGENTS.md marker block + machine codex config
#     present -> codex is NOT wired here.
# ---------------------------------------------------------------------------


def test_codex_not_wired_without_project_agents_md_marker(tmp_path: Path, codex_home: Path) -> None:
    _init_project(tmp_path)
    _write_machine_codex_config(codex_home)
    # No AGENTS.md at all in this project.
    assert "codex" not in _wired_harnesses(tmp_path)


def test_plain_unregister_does_not_touch_foreign_machine_codex_config(
    tmp_path: Path, codex_home: Path
) -> None:
    _init_project(tmp_path)
    _write_machine_codex_config(codex_home)
    before = (codex_home / "config.toml").read_text(encoding="utf-8")

    rc = _unregister(tmp_path, harness=None)

    assert rc == 0
    assert (codex_home / "config.toml").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# (ii) same setup -> strata launch resolves claude-code, not codex, when
#      claude-code is also unwired.
# ---------------------------------------------------------------------------


def test_launch_resolves_claude_code_not_foreign_codex(tmp_path: Path, codex_home: Path) -> None:
    _init_project(tmp_path)
    _write_machine_codex_config(codex_home)
    # No .claude/settings.json -> claude-code unwired either.
    args = argparse.Namespace(harness=None)
    assert _resolve_launch_harness(args, tmp_path) == "claude-code"


# ---------------------------------------------------------------------------
# (iii) project WITH the AGENTS.md marker block -> default unregister still
#       reverses codex (the positive case; guards against over-fixing).
# ---------------------------------------------------------------------------


def test_default_unregister_still_reverses_codex_when_project_evidence_present(
    tmp_path: Path, codex_home: Path
) -> None:
    _init_project(tmp_path)
    # Register codex here for real, so both the machine config and the
    # project AGENTS.md marker exist and byte-match what register wrote.
    rc = cmd_register(
        argparse.Namespace(path=str(tmp_path), diff=False, bootstrap_venv=False, harness=["codex"])
    )
    assert rc == 0
    assert "codex" in _wired_harnesses(tmp_path)

    rc = _unregister(tmp_path, harness=None)

    assert rc == 0
    config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert not install.codex_mcp_present(config_text)
    assert not install.codex_hook_present(config_text)
    agents_text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert not install.agents_md_present(agents_text)
