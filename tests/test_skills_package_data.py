"""Tests for 3e: skills vendored as package data in src/strata/_skills/.

Verifies that:
1. importlib.resources can find each skill directory.
2. Each skill directory contains a Skill.md file.
3. Skill.md contents are non-empty and contain a YAML front-matter header.
4. The strata-worker skill Skill.md no longer contains hardcoded filesystem paths
   (e.g. /home/user/Strata) — it's project-neutral.
"""

from __future__ import annotations

import importlib.resources
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# The three Strata-runtime skills shipped as package data.
_STRATA_SKILLS = ["strata", "strata-worker", "strata-inspect"]


# ---------------------------------------------------------------------------
# Test 1: skill directories accessible via importlib.resources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_name", _STRATA_SKILLS)
def test_skill_directory_accessible_via_importlib(skill_name: str) -> None:
    """importlib.resources.files('strata') / '_skills' / skill_name must be a directory."""
    ref = importlib.resources.files("strata") / "_skills" / skill_name
    assert ref.is_dir(), (
        f"strata/_skills/{skill_name} not found via importlib.resources. "
        "Check pyproject.toml include patterns and that src/strata/_skills/ exists."
    )


# ---------------------------------------------------------------------------
# Test 2: each skill directory contains Skill.md
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_name", _STRATA_SKILLS)
def test_skill_directory_contains_skill_md(skill_name: str) -> None:
    """Each strata/_skills/<skill> directory must contain a Skill.md file."""
    skill_md = importlib.resources.files("strata") / "_skills" / skill_name / "Skill.md"
    assert skill_md.is_file(), (
        f"strata/_skills/{skill_name}/Skill.md not found. "
        "Run `strata register` to install skills from package data."
    )


# ---------------------------------------------------------------------------
# Test 3: Skill.md files contain YAML front-matter header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_name", _STRATA_SKILLS)
def test_skill_md_has_yaml_front_matter(skill_name: str) -> None:
    """Each Skill.md must start with YAML front-matter (---) and include a name field."""
    skill_md = importlib.resources.files("strata") / "_skills" / skill_name / "Skill.md"
    content = skill_md.read_text(encoding="utf-8")

    assert content.startswith("---"), (
        f"strata/_skills/{skill_name}/Skill.md does not start with YAML front-matter (---)"
    )
    assert "name:" in content, (
        f"strata/_skills/{skill_name}/Skill.md missing 'name:' in front-matter"
    )


# ---------------------------------------------------------------------------
# Test 4: vendored skills are project-neutral (no hardcoded host paths)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_name", _STRATA_SKILLS)
def test_skill_md_has_no_hardcoded_host_paths(skill_name: str) -> None:
    """Vendored Skill.md must not reference hardcoded host filesystem paths."""
    skill_md = importlib.resources.files("strata") / "_skills" / skill_name / "Skill.md"
    content = skill_md.read_text(encoding="utf-8")

    # These are machine-specific paths that must not appear in package data.
    forbidden_patterns = ["/home/user/Strata", "/Users/", "C:\\Users\\"]
    for pattern in forbidden_patterns:
        assert pattern not in content, (
            f"strata/_skills/{skill_name}/Skill.md contains a hardcoded host path: {pattern!r}. "
            "Skills must be project-neutral (no absolute filesystem paths)."
        )


# ---------------------------------------------------------------------------
# Test 5: _skills __init__.py exists (package marker)
# ---------------------------------------------------------------------------


def test_strata_skills_package_importable() -> None:
    """strata._skills must be importable as a package."""
    import strata._skills  # noqa: F401

    assert hasattr(strata._skills, "__path__"), "strata._skills must be a package"


# ---------------------------------------------------------------------------
# Test 6: skill content is non-trivially sized
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_name", _STRATA_SKILLS)
def test_skill_md_content_is_substantial(skill_name: str) -> None:
    """Skill.md must have substantial content (> 100 bytes)."""
    skill_md = importlib.resources.files("strata") / "_skills" / skill_name / "Skill.md"
    content = skill_md.read_text(encoding="utf-8")
    assert len(content) > 100, (
        f"strata/_skills/{skill_name}/Skill.md appears truncated (< 100 bytes)"
    )
