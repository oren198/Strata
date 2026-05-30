"""Root conftest.py — prepend the worktree's src/ to sys.path.

This ensures that pytest loads the strata package from this worktree's src/
directory rather than the editable install at /home/user/Strata/src, which
is the installed location and would shadow worktree changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Prepend this worktree's src/ so it shadows the system-installed strata package.
_worktree_src = str(Path(__file__).parent / "src")
if _worktree_src not in sys.path:
    sys.path.insert(0, _worktree_src)
