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
