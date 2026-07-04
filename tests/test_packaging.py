"""Tests for 3a packaging: strata.mcp.server module placement and entry point.

Verifies that:
- ``import strata.mcp.server`` works.
- The ``main()`` function is callable (it's the console-script entry point).
- ``python -m strata.mcp.server`` can be imported cleanly (i.e. the
  ``if __name__ == "__main__"`` guard doesn't execute on plain import).
- The ``strata-mcp`` console script is on PATH after install.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# Test: strata.mcp.server importable
# ---------------------------------------------------------------------------


def test_strata_mcp_server_importable() -> None:
    """``import strata.mcp.server`` must succeed without crashing."""
    import strata.mcp.server  # noqa: F401


# ---------------------------------------------------------------------------
# Test: main() is callable
# ---------------------------------------------------------------------------


def test_main_is_callable() -> None:
    """``strata.mcp.server.main`` must be a callable (the console-script target)."""
    import strata.mcp.server as mod

    assert callable(mod.main), "strata.mcp.server.main must be a callable"


# ---------------------------------------------------------------------------
# Test: __main__ module importable via python -m strata.mcp.server
# ---------------------------------------------------------------------------


def test_strata_mcp_server_module_importable() -> None:
    """``strata.mcp.server`` can be re-imported cleanly (simulates python -m import)."""
    # Remove any cached version and re-import fresh.
    for key in list(sys.modules.keys()):
        if "strata.mcp" in key:
            del sys.modules[key]

    # Should not raise.
    mod = importlib.import_module("strata.mcp.server")
    assert hasattr(mod, "main")
    assert hasattr(mod, "mcp")


# ---------------------------------------------------------------------------
# Test: strata-mcp console script on PATH
# ---------------------------------------------------------------------------


def test_strata_mcp_console_script_alongside_python() -> None:
    """``strata-mcp`` must install into the same bin dir as the running Python.

    We test the entry point via the *installing Python's* bin dir rather than
    the ambient ``PATH``. ``shutil.which`` searches ``PATH`` and returns ``None``
    in a fresh venv that hasn't been activated (e.g. CI invoking
    ``/path/to/venv/bin/pytest`` directly without sourcing ``activate``), even
    though the console script is correctly installed. We want to verify the
    pyproject ``[project.scripts]`` wiring, not coincidental PATH state.
    """
    script = Path(sys.executable).parent / "strata-mcp"
    assert script.is_file(), (
        f"strata-mcp not installed alongside {sys.executable}. "
        "Run `pip install -e '.[dev]'` to install the console script."
    )


# ---------------------------------------------------------------------------
# Test: strata.mcp package has __init__.py
# ---------------------------------------------------------------------------


def test_strata_mcp_package_has_init() -> None:
    """``strata.mcp`` package must be importable as a package."""
    import strata.mcp  # noqa: F401

    assert hasattr(strata.mcp, "__path__"), "strata.mcp must be a package (not a module)"


# ---------------------------------------------------------------------------
# Test: old mcp_server module no longer exists
# ---------------------------------------------------------------------------


def test_old_mcp_server_module_gone() -> None:
    """The old ``mcp_server.strata_mcp`` module must no longer exist."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("mcp_server.strata_mcp")


# ---------------------------------------------------------------------------
# Tests: wheel completeness — data dirs bundled, mcp a core dependency.
#
# V1.3 dogfooding found `pipx install strata` produced a broken install:
# migrations/ and templates/ lived at the repo root (absent from the wheel)
# and the `mcp` SDK sat in an optional extra while `strata-mcp` is an
# unconditional console script. These are the tripwires.
# ---------------------------------------------------------------------------


def test_migrations_bundled_as_package_data() -> None:
    """``strata/_migrations`` must ship inside the package with the SQL files."""
    from importlib.resources import files

    mig_dir = files("strata") / "_migrations"
    names = [entry.name for entry in mig_dir.iterdir()]
    assert "0001_initial.sql" in names
    assert "0002_drop_fleet_tables.sql" in names


def test_templates_bundled_as_package_data() -> None:
    """``strata/_templates`` must ship inside the package with the starter fleets."""
    from importlib.resources import files

    tpl_dir = files("strata") / "_templates"
    names = {entry.name for entry in tpl_dir.iterdir()}
    assert {"minimal.yaml", "dev-team.yaml"} <= names, (
        f"starter templates missing from package data: {names}"
    )


def test_bundled_templates_are_valid_fleet_configs() -> None:
    """Every starter template must pass FleetConfig validation.

    Dogfooding found ``minimal.yaml`` shipped with ``default_skill`` outside
    an empty ``permitted_skills`` — an invalid fleet that made ``strata-mcp``
    crash on first start in a freshly registered project.
    """
    from strata.fleet_config import FleetConfig

    tpl_dir = Path(__file__).parent.parent / "src" / "strata" / "_templates"
    templates = sorted(tpl_dir.glob("*.yaml"))
    assert templates, f"no starter templates found in {tpl_dir}"
    for template in templates:
        FleetConfig.load(template)  # raises FleetConfigError if invalid


def test_mcp_is_a_core_dependency() -> None:
    """``mcp`` must be in [project] dependencies, not an optional extra.

    ``strata-mcp`` is an unconditional console script; if its imports live
    behind an extra, a plain ``pipx install strata`` ships a broken binary.
    """
    import tomllib

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    with pyproject.open("rb") as fh:
        meta = tomllib.load(fh)
    core_deps = [dep.split("[")[0].split(" ")[0] for dep in meta["project"]["dependencies"]]
    assert "mcp" in core_deps, "the mcp SDK must be a core dependency (strata-mcp imports it)"
