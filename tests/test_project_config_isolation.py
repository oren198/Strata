"""The suite must never resolve storage against the developer's own store (#181).

`project_config.load_project_config` walks up from the current working
directory looking for `.strata/config.toml`. Run from a checkout that has
been registered against itself — which the README encourages for dogfooding —
every test that resolves storage paths without an explicit override finds the
developer's real config and serves their live store to the fixtures. That
produced 23 failures in tests/test_app.py alone on 2026-08-31, with assertion
text pointing at the developer's own scope ids.

These tests pin the guard: whatever ambient project config exists on the
machine running the suite, a test resolves nothing from it.
"""

from __future__ import annotations

from pathlib import Path

from strata.project_config import load_project_config, resolve_storage_paths


def test_no_ambient_project_config_is_visible_to_a_test():
    """The walk-up from the test's cwd must find nothing."""
    assert load_project_config() is None


def test_storage_resolves_through_env_not_a_discovered_project():
    """With no project config reachable, resolution uses the env fallback."""
    assert resolve_storage_paths().source == "env"


def test_cwd_is_not_the_developers_checkout():
    """Tests run somewhere private, so relative paths cannot touch the repo."""
    repo_root = Path(__file__).resolve().parent.parent
    assert repo_root not in Path.cwd().resolve().parents
    assert Path.cwd().resolve() != repo_root


def test_a_test_can_still_opt_into_its_own_project_config(tmp_path):
    """Isolation pins the ambient default; it does not block explicit use."""
    strata_dir = tmp_path / ".strata"
    strata_dir.mkdir()
    (strata_dir / "config.toml").write_text(
        'db = ".strata/strata.db"\n'
        'fleet_yaml = ".strata/fleet.yaml"\n'
        'summaries_dir = ".strata/summaries"\n',
        encoding="utf-8",
    )

    project = load_project_config(tmp_path)

    assert project is not None
    assert project.project_root == tmp_path
