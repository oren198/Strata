"""Root conftest.py — prepend the worktree's src/ to sys.path.

This ensures that pytest loads the strata package from this worktree's src/
directory rather than the editable install at /home/user/Strata/src, which
is the installed location and would shadow worktree changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Prepend this worktree's src/ so it shadows the system-installed strata package.
_worktree_src = str(Path(__file__).parent / "src")
if _worktree_src not in sys.path:
    sys.path.insert(0, _worktree_src)


@pytest.fixture(autouse=True)
def _reset_lock_dir():
    """Reset ``strata.locks._lock_dir`` before and after every test.

    ``configure_lock_dir`` (issue #19, ADR 0012) sets a process-global that
    outlives the test that called it — any test invoking a store-init path
    (``strata.mcp.server._init_stores``, ``strata.app.create_app``'s
    lifespan, ``strata.stores.open_embedded_stores``, the freshness
    evaluator's ``_submit_judged_contribution``) leaves ``_lock_dir``
    pointing at that test's ``tmp_path`` for every test that runs after it
    in the same pytest process, silently making a later ``with
    scope_lock(...):`` open a lock file under a directory that may no
    longer exist. Left unreset, this is exactly the bug that put
    ``.locks/g_split.summary.lock`` / ``.locks/g_team.summary.lock`` at the
    repo root during a full-suite run (fix-round 2): a test that resolves
    storage paths against the real cwd, with no ``STRATA_DB_PATH`` /
    ``.strata/config.toml`` override, read whatever ``_lock_dir`` a prior
    test left behind — or, if none had run yet, called ``configure_lock_dir``
    itself against the real cwd, which then leaked into every test after it.
    Resetting to ``None`` on both sides of each test makes every test start
    from "not configured" (the default, no-flock-attempted state) and
    leaves nothing behind for the next test to inherit.
    """
    import strata.locks as locks

    locks._lock_dir = None
    yield
    locks._lock_dir = None


@pytest.fixture(autouse=True)
def _isolate_project_config_discovery(request: pytest.FixtureRequest, tmp_path, monkeypatch):
    """Never let ``resolve_storage_paths()`` discover a REAL ``.strata/config.toml``.

    :func:`strata.project_config.load_project_config` walks up from ``Path.cwd()``
    by default (no explicit ``start=``) looking for ``.strata/config.toml`` —
    and wins over any explicit ``Settings``/``fleet_yaml_path`` a test built for
    itself (``resolve_storage_paths``'s documented precedence). Every worktree
    under this repo nests inside a directory tree that now has a REAL, live
    ``.strata/config.toml`` at the repo root (the dogfood fleet an operator
    registered for their own day-to-day use) — so any test that builds a
    ``create_app``/``Settings`` with a tmp fleet path but never passes an
    explicit ``start=`` was silently resolving through to that real project
    instead, and a save-path test (``PUT /fleet``) landed a live write on the
    operator's actual ``fleet.yaml``, clobbering it, before this guard existed
    (the incident this fixture fixes).

    The fix pins the walk's default start directory at this test's own
    ``tmp_path`` (which never has a ``.strata/``) whenever a caller does not
    pass ``start=`` itself — an explicit ``start=`` (every test in
    ``test_project_config.py`` and ``test_v1_3_1_hardening.py`` passes one)
    is never touched, so real-discovery behavior stays fully testable on its
    own terms. Opt out with ``@pytest.mark.real_machine`` for a test that
    must exercise the genuine cwd-walk.
    """
    if request.node.get_closest_marker("real_machine") is not None:
        return

    import strata.project_config as project_config

    real_load_project_config = project_config.load_project_config
    guard_root = tmp_path / "_autouse_no_project_config_guard"

    def _guarded_load_project_config(start=None, **kwargs):
        if start is None:
            start = guard_root
        return real_load_project_config(start=start, **kwargs)

    monkeypatch.setattr(project_config, "load_project_config", _guarded_load_project_config)
