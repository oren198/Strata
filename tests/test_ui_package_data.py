"""Console UI vendored as package data in src/strata/_ui/ (issue #65).

The FastAPI backend serves the Console from a static directory. Resolving it
by walking up from ``__file__`` to the repo root works only in editable
installs — a wheel install (pipx, ADR 0005) has no ``ui/`` anywhere, so the
Console silently has nothing to serve. Same failure class the wheel-smoke CI
leg exists for; same fix as ``_skills/`` / ``_migrations/`` / ``_templates/``.

Verifies that:
1. importlib.resources can find the ``_ui`` directory.
2. Every Console asset ships inside it.
3. ``strata.app._UI_DIR`` (the static-mount source) resolves inside the
   installed package, not the repo root.
"""

from __future__ import annotations

import importlib.resources
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Every file the Console needs — index.html plus the assets it loads.
_UI_FILES = [
    "index.html",
    "app.jsx",
    "atoms.jsx",
    "graph.jsx",
    "scope-detail.jsx",
    "settings.jsx",
    "tweaks-panel.jsx",
    "store.js",
    "atlas.css",
]


def test_ui_directory_accessible_via_importlib() -> None:
    """importlib.resources.files('strata') / '_ui' must be a directory."""
    ref = importlib.resources.files("strata") / "_ui"
    assert ref.is_dir(), (
        "strata/_ui not found via importlib.resources. "
        "Check pyproject.toml include patterns and that src/strata/_ui/ exists."
    )


@pytest.mark.parametrize("filename", _UI_FILES)
def test_ui_asset_ships_in_package(filename: str) -> None:
    """Each Console asset must ship inside the package."""
    ref = importlib.resources.files("strata") / "_ui" / filename
    assert ref.is_file(), f"strata/_ui/{filename} missing — the Console cannot serve it."


def test_static_mount_source_is_package_data() -> None:
    """The directory app.py mounts at /ui must live inside the package.

    A repo-root path here means the mount is empty in a wheel install.
    """
    import strata
    from strata.app import _UI_DIR

    pkg_dir = Path(strata.__file__).resolve().parent
    assert pkg_dir in _UI_DIR.resolve().parents, (
        f"_UI_DIR ({_UI_DIR}) resolves outside the strata package ({pkg_dir}); "
        "it will not ship in the wheel"
    )
