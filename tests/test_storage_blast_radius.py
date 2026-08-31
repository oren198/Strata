"""No test may resolve storage onto a store outside its own tmp_path (#181).

The cwd guard added earlier pins `load_project_config`'s walk-up, but it is
not sufficient on its own: `get_settings()` is an `lru_cache` singleton whose
`db_path` / `summaries_dir` defaults are RELATIVE ("./strata.db",
"./summaries"). The singleton is built on first use — which can happen at
import time, before any fixture has changed directory — so the paths it hands
out resolve against whatever cwd the process started in.

On 2026-08-31 that is exactly what destroyed a real scope summary: a test run
inside the repo rewrote `summaries/g_arch.md` (17KB of real memory) with smoke
fixture content, and separately a route test overwrote the live `fleet.yaml`.

These tests assert the blast radius directly: whatever a test resolves, it
must sit under that test's own tmp_path and nowhere else.
"""

from __future__ import annotations

from pathlib import Path

from strata.project_config import resolve_storage_paths
from strata.settings import get_settings


def _assert_under_tmp(path_str: str, tmp_path: Path, label: str) -> None:
    resolved = Path(path_str).resolve()
    assert tmp_path.resolve() in resolved.parents or resolved == tmp_path.resolve(), (
        f"{label} resolved to {resolved}, which is outside this test's tmp_path "
        f"({tmp_path}). A test must never be able to reach a real store."
    )


def test_resolved_db_path_is_inside_this_tests_tmp_path(tmp_path):
    _assert_under_tmp(resolve_storage_paths().db_path, tmp_path, "db_path")


def test_resolved_summaries_dir_is_inside_this_tests_tmp_path(tmp_path):
    _assert_under_tmp(resolve_storage_paths().summaries_dir, tmp_path, "summaries_dir")


def test_resolved_fleet_yaml_is_inside_this_tests_tmp_path(tmp_path):
    _assert_under_tmp(resolve_storage_paths().fleet_yaml_path, tmp_path, "fleet_yaml_path")


def test_settings_singleton_itself_carries_no_ambient_relative_path(tmp_path):
    """The cached singleton is the hole — pin it, not just the walk-up."""
    settings = get_settings()
    _assert_under_tmp(settings.db_path, tmp_path, "settings.db_path")
    _assert_under_tmp(settings.summaries_dir, tmp_path, "settings.summaries_dir")


def test_the_developers_own_checkout_is_never_a_resolution_target(tmp_path):
    """Belt and braces: name the failure mode that actually happened."""
    repo_root = Path(__file__).resolve().parent.parent
    for label, value in (
        ("db_path", resolve_storage_paths().db_path),
        ("summaries_dir", resolve_storage_paths().summaries_dir),
        ("fleet_yaml_path", resolve_storage_paths().fleet_yaml_path),
    ):
        resolved = Path(value).resolve()
        assert repo_root not in resolved.parents and resolved != repo_root, (
            f"{label} resolved into the checkout at {resolved} — this is how a "
            f"test run overwrote real memory."
        )
