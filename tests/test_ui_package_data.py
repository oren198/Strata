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
import json
import os
import shutil
import subprocess
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
    "operator-actions.jsx",
    "scope-detail.jsx",
    "settings.jsx",
    "format.jsx",
    "fleet-edit.jsx",
    "declines.jsx",
    "freshness.jsx",
    "record-trail.jsx",
    "view-as.jsx",
    "tweaks-panel.jsx",
    "store.js",
    "atlas.css",
]

_JSX_FILES = [f for f in _UI_FILES if f.endswith(".jsx")]


def _find_babel_node_path() -> str | None:
    """Return a ``NODE_PATH`` entry with ``@babel/core`` + ``@babel/preset-react``
    importable from it, or ``None`` if neither ``node`` nor those packages are
    available in this environment.

    The Console deliberately has no build step (``index.html`` loads
    ``@babel/standalone`` in the browser, per its no-build JSX contract) — this
    is only an offline syntax check for the test suite, so it degrades to a
    skip rather than a failure when the environment doesn't have them; it
    never becomes a hard dependency of `pip install -e .[dev]`.
    """
    if shutil.which("node") is None:
        return None
    for candidate in (
        os.environ.get("STRATA_TEST_BABEL_NODE_PATH"),
        "/tmp/node_modules",
    ):
        if not candidate:
            continue
        base = Path(candidate)
        if (base / "@babel" / "core").is_dir() and (base / "@babel" / "preset-react").is_dir():
            return candidate
    return None


_BABEL_NODE_PATH = _find_babel_node_path()


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


@pytest.mark.skipif(
    _BABEL_NODE_PATH is None,
    reason="node + @babel/core + @babel/preset-react not available in this environment",
)
@pytest.mark.parametrize("filename", _JSX_FILES)
def test_jsx_parses_with_babel(filename: str) -> None:
    """Every Console .jsx file must be syntactically valid to Babel's JSX parser.

    Runs the same transform ``@babel/standalone`` performs in the browser
    (JSX + ``@babel/preset-react``), offline, through ``@babel/core`` — a
    real parse, not a regex heuristic. Source is piped over stdin so this
    works the same whether the package is an editable install or a wheel.
    """
    source = (importlib.resources.files("strata") / "_ui" / filename).read_text(encoding="utf-8")
    script = (
        "const babel = require('@babel/core');"
        "let code = '';"
        "process.stdin.setEncoding('utf8');"
        "process.stdin.on('data', d => code += d);"
        "process.stdin.on('end', () => {"
        "  try {"
        "    const filename = process.argv[1];"
        "    babel.transform(code, { presets: ['@babel/preset-react'], filename });"
        "    console.log('OK');"
        "  } catch (e) {"
        "    console.error(e.message);"
        "    process.exit(1);"
        "  }"
        "});"
    )
    env = {**os.environ, "NODE_PATH": _BABEL_NODE_PATH}
    result = subprocess.run(
        ["node", "-e", script, filename],
        input=source,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(
    _BABEL_NODE_PATH is None,
    reason="node + @babel/core + @babel/preset-react not available in this environment",
)
def test_record_trail_state_words_reports_refresh_pending_not_outage() -> None:
    """ADR 0014 pin 4, exercised as real JS: `stateWords` in record-trail.jsx
    must label a pending `manager-refresh` contribution "Refresh pending",
    never the plain "Awaiting judgment" a genuine stuck judgment gets — an
    operator scanning the Console's Record view must not count a pending
    refresh as a judge outage.

    Transpiles the real file with Babel and runs it under Node (the same
    transform `@babel/standalone` performs in the browser) rather than
    re-implementing the label logic in Python, so this test fails the moment
    the shipped function's behaviour changes, not just its source text.
    """
    source = (importlib.resources.files("strata") / "_ui" / "record-trail.jsx").read_text(
        encoding="utf-8"
    )
    script = (
        "const babel = require('@babel/core');"
        "let code = '';"
        "process.stdin.setEncoding('utf8');"
        "process.stdin.on('data', d => code += d);"
        "process.stdin.on('end', () => {"
        "  const filename = process.argv[1];"
        "  const { code: transformed } = babel.transform(code, {"
        "    presets: ['@babel/preset-react'], filename"
        "  });"
        "  global.window = {};"
        "  global.React = {};"
        "  (0, eval)(transformed);"
        "  const cases = ["
        "    [undefined, undefined, 'manager-refresh', 'Refresh pending'],"
        "    [undefined, undefined, undefined, 'Awaiting judgment'],"
        "    [{ state: 'pending' }, undefined, 'manager-refresh', 'Refresh pending'],"
        "    [{ state: 'pending' }, undefined, undefined, 'Awaiting judgment'],"
        "    [{ state: 'judge_failed' }, undefined, 'manager-refresh', 'Judgment failed'],"
        "    [{ state: 'judged', decision: 'accept_as_context' }, undefined, 'manager-refresh',"
        "      'Accepted as context'],"
        "  ];"
        "  const results = cases.map(([stateEntry, attempts, subject]) =>"
        "    global.window.stateWords(stateEntry, attempts, subject).label"
        "  );"
        "  console.log(JSON.stringify(results));"
        "});"
    )
    env = {**os.environ, "NODE_PATH": _BABEL_NODE_PATH}
    result = subprocess.run(
        ["node", "-e", script, "record-trail.jsx"],
        input=source,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    labels = json.loads(result.stdout)
    assert labels == [
        "Refresh pending",  # no state row yet, but this IS a manager-refresh notice
        "Awaiting judgment",  # no state row, ordinary contribution
        "Refresh pending",  # explicit pending state, manager-refresh subject
        "Awaiting judgment",  # explicit pending state, ordinary subject
        "Judgment failed",  # a refresh's judge call can still error like any other
        "Accepted as context",  # a refresh that WAS judged reads as judged, not pending
    ]
