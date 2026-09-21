"""#205 — the suite refuses to run against a `strata` that is not this tree's.

The shared venv is an editable install of the MAIN checkout, so a worktree's tests can
silently import (and spawn subprocesses that import) the wrong code. The root conftest
guards it; these tests pin the guard's decision and the subprocess environment.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _conftest():
    spec = importlib.util.spec_from_file_location(
        "_root_conftest_under_test", _ROOT / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_running_suite_imports_this_trees_strata() -> None:
    import strata

    assert Path(strata.__file__).resolve().is_relative_to(_ROOT), strata.__file__


def test_a_strata_under_the_repo_root_is_fine(tmp_path: Path) -> None:
    c = _conftest()
    assert c.strata_import_problem(str(tmp_path / "src/strata/__init__.py"), tmp_path) is None


def test_a_strata_from_another_checkout_fails_with_the_fix(tmp_path: Path) -> None:
    c = _conftest()
    other = tmp_path / "main-checkout" / "src" / "strata" / "__init__.py"
    root = tmp_path / "worktree"
    root.mkdir()
    problem = c.strata_import_problem(str(other), root)
    assert problem is not None
    assert str(other) in problem
    assert "PYTHONPATH=$PWD/src" in problem
    assert "install this tree" in problem


def _fake_site_packages(tmp_path: Path, direct_url: dict | None) -> tuple[Path, Path]:
    site = tmp_path / "venv" / "site-packages"
    (site / "strata").mkdir(parents=True)
    init = site / "strata" / "__init__.py"
    init.write_text("")
    dist = site / "strata-0.0.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: strata\nVersion: 0.0.0\n")
    if direct_url is not None:
        (dist / "direct_url.json").write_text(json.dumps(direct_url))
    return site, init


def test_a_non_editable_install_of_this_same_tree_does_not_fire(tmp_path: Path) -> None:
    """`pip install .` puts strata in site-packages; direct_url.json names this tree."""
    root = tmp_path / "worktree"
    root.mkdir()
    site, init = _fake_site_packages(tmp_path, {"url": root.as_uri(), "dir_info": {}})
    c = _conftest()
    assert c.strata_import_problem(str(init), root, search_path=[str(site)]) is None


def test_an_install_of_a_different_tree_fires(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    root.mkdir()
    other = tmp_path / "main"
    other.mkdir()
    site, init = _fake_site_packages(
        tmp_path, {"url": other.as_uri(), "dir_info": {"editable": True}}
    )
    c = _conftest()
    assert c.strata_import_problem(str(init), root, search_path=[str(site)]) is not None


def test_an_install_with_no_provenance_fires(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    root.mkdir()
    site, init = _fake_site_packages(tmp_path, None)
    c = _conftest()
    assert c.strata_import_problem(str(init), root, search_path=[str(site)]) is not None


def test_subprocesses_inherit_this_trees_src_first() -> None:
    """Children (`python -m strata`, hook scripts) must import the same tree as the suite."""
    first = os.environ.get("PYTHONPATH", "").split(os.pathsep)[0]
    assert Path(first).resolve() == (_ROOT / "src").resolve()


def test_a_subprocess_imports_this_trees_strata() -> None:
    import subprocess

    out = subprocess.run(
        [sys.executable, "-c", "import strata; print(strata.__file__)"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert Path(out).resolve().is_relative_to(_ROOT), out


def test_guard_aborts_the_session_when_strata_is_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _conftest()
    root = tmp_path / "worktree"
    root.mkdir()
    fake = type(sys)("strata")
    fake.__file__ = str(tmp_path / "main" / "src" / "strata" / "__init__.py")
    monkeypatch.setitem(sys.modules, "strata", fake)
    with pytest.raises(pytest.exit.Exception) as info:
        c.enforce_strata_import(root)
    assert "PYTHONPATH=$PWD/src" in str(info.value)
