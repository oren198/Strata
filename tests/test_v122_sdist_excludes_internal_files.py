"""The strata-mem sdist never carries internal agent files (``.claude/``, ``.superpowers/``).

hatch's default sdist is every tracked file, so the project's own agent skills and review diffs
would ship in the release artifact unless the root pyproject.toml excludes them. The files stay in
git.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INTERNAL = (".claude", ".superpowers")


def test_the_root_pyproject_excludes_the_internal_agent_dirs_from_the_sdist() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sdist = config["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert sdist == {"exclude": ["/integrations", "/.claude", "/.superpowers"]}


def test_the_wheel_target_is_unchanged() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "exclude" not in wheel and wheel["packages"] == ["src/strata"]


@pytest.mark.skipif(
    importlib.util.find_spec("build") is None or importlib.util.find_spec("hatchling") is None,
    reason="needs the build and hatchling packages (the build runs without isolation, offline)",
)
def test_a_built_sdist_holds_neither_internal_dir(tmp_path: Path) -> None:
    """Build a real sdist from a copy of the packaging files plus both internal directories."""
    work = tmp_path / "tree"
    (work / "src" / "strata").mkdir(parents=True)
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        if (ROOT / name).exists():
            shutil.copy(ROOT / name, work / name)
    (work / "src" / "strata" / "__init__.py").write_text('__version__ = "0"\n', encoding="utf-8")
    (work / ".claude" / "skills" / "x").mkdir(parents=True)
    (work / ".claude" / "skills" / "x" / "Skill.md").write_text("x\n", encoding="utf-8")
    (work / ".superpowers").mkdir()
    (work / ".superpowers" / "review.diff").write_text("x\n", encoding="utf-8")
    out = tmp_path / "dist"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--no-isolation",
            "--outdir",
            str(out),
            str(work),
        ],
        check=True,
        capture_output=True,
    )
    (archive,) = out.glob("*.tar.gz")
    with tarfile.open(archive) as tar:
        names = [n.split("/", 1)[1] for n in tar.getnames() if "/" in n]
    assert "src/strata/__init__.py" in names
    assert not [n for n in names if n.split("/", 1)[0] in INTERNAL]
