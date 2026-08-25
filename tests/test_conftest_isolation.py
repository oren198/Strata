"""Prove the autouse isolation guard in tests/conftest.py actually guards.

Regression coverage for the incident it fixes: before this guard existed,
a test that forgot to pin harness selection or ``$CODEX_HOME`` could fall
through to real-machine detection / the real ``~/.codex/config.toml``. These
tests never touch the developer's actual home directory — they stand a
decoy "real home" in for it (via ``Path.home``) and show where Codex config
resolution *would* land with and without the guard.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata import install  # noqa: E402


def test_detect_harnesses_returns_empty_by_default(tmp_path: Path) -> None:
    """The autouse guard patches detect_harnesses() to [] unless overridden."""
    assert install.detect_harnesses() == []


def test_codex_home_env_is_pinned_under_tmp_path_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The autouse guard points $CODEX_HOME at this test's own tmp_path."""
    import os

    codex_home = os.environ.get("CODEX_HOME")
    assert codex_home is not None
    assert Path(codex_home).is_relative_to(tmp_path)
    assert install.codex_config_path() == Path(codex_home) / "config.toml"


@pytest.mark.real_machine
def test_without_the_guard_codex_config_path_would_resolve_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-break the previously-leaking path: opt out of the guard via the
    marker, stand in a decoy directory for ``Path.home()`` (never the real
    one), and confirm that with no $CODEX_HOME pin, Codex config resolution
    falls back to that decoy home — exactly the mechanism that used to hit
    the real ~/.codex/config.toml before this fixture existed.
    """
    import os

    decoy_home = tmp_path / "decoy_real_home"
    decoy_home.mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(install.Path, "home", classmethod(lambda cls: decoy_home))

    # No CODEX_HOME guard active (marker opted this test out) -> falls back
    # to Path.home() / ".codex", i.e. the decoy "real home" stand-in.
    assert os.environ.get("CODEX_HOME") is None
    resolved = install.codex_config_path()
    assert resolved == decoy_home / ".codex" / "config.toml"
    assert not resolved.is_relative_to(tmp_path / "_autouse_codex_home_guard")


def test_with_the_guard_active_same_decoy_home_is_never_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same decoy-home setup as above, but WITHOUT opting out of the guard:
    the guard's $CODEX_HOME pin wins over Path.home(), so the decoy (a
    stand-in for the real home) is never touched.
    """
    decoy_home = tmp_path / "decoy_real_home"
    decoy_home.mkdir()
    monkeypatch.setattr(install.Path, "home", classmethod(lambda cls: decoy_home))

    resolved = install.codex_config_path()
    assert not resolved.is_relative_to(decoy_home)
