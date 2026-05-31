"""Tests for 3c project config loader: .strata/config.toml walk-up discovery.

Verifies:
1. Walk-up discovery from a deep subdir finds config at the root.
2. Missing config returns None.
3. Malformed TOML raises ProjectConfigError(kind="bad_toml").
4. Missing required field raises ProjectConfigError(kind="missing_field").
5. Field type validation — non-string path raises ProjectConfigError.
6. Paths are resolved absolute relative to project_root.
7. Absolute paths in config are kept as-is.
8. Walk stops at filesystem root when config not found.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.project_config import ProjectConfig, ProjectConfigError, load_project_config


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _write_config(strata_dir: Path, content: str) -> Path:
    """Write content to <strata_dir>/config.toml and return the path."""
    strata_dir.mkdir(parents=True, exist_ok=True)
    config = strata_dir / "config.toml"
    config.write_text(content, encoding="utf-8")
    return config


# ---------------------------------------------------------------------------
# Test 1: walk-up discovery from a deep subdir
# ---------------------------------------------------------------------------


def test_walk_up_discovers_config_from_deep_subdir(tmp_path: Path) -> None:
    """Walk-up from a deep subdir must find .strata/config.toml at the project root."""
    _write_config(
        tmp_path / ".strata",
        'db = ".strata/strata.db"\nfleet_yaml = ".strata/fleet.yaml"\nsummaries_dir = ".strata/summaries"\n',
    )

    # Start from a deep subdir.
    deep = tmp_path / "src" / "deeply" / "nested"
    deep.mkdir(parents=True)

    result = load_project_config(start=deep)

    assert result is not None
    assert result.project_root == tmp_path.resolve()
    assert result.db == (tmp_path / ".strata" / "strata.db").resolve()
    assert result.fleet_yaml == (tmp_path / ".strata" / "fleet.yaml").resolve()
    assert result.summaries_dir == (tmp_path / ".strata" / "summaries").resolve()


# ---------------------------------------------------------------------------
# Test 2: missing config returns None
# ---------------------------------------------------------------------------


def test_missing_config_returns_none(tmp_path: Path) -> None:
    """No .strata/config.toml anywhere up the tree → load_project_config returns None."""
    # Use a subdir of tmp_path; tmp_path itself has no .strata dir.
    subdir = tmp_path / "my_project"
    subdir.mkdir()

    result = load_project_config(start=subdir)
    assert result is None


# ---------------------------------------------------------------------------
# Test 3: malformed TOML raises ProjectConfigError(kind="bad_toml")
# ---------------------------------------------------------------------------


def test_malformed_toml_raises_project_config_error(tmp_path: Path) -> None:
    """Malformed TOML in .strata/config.toml raises ProjectConfigError(kind='bad_toml')."""
    _write_config(tmp_path / ".strata", "THIS IS NOT TOML @@@ !!!")

    with pytest.raises(ProjectConfigError) as exc_info:
        load_project_config(start=tmp_path)

    assert exc_info.value.kind == "bad_toml"


# ---------------------------------------------------------------------------
# Test 4: missing required field raises ProjectConfigError(kind="missing_field")
# ---------------------------------------------------------------------------


def test_missing_required_field_raises_project_config_error(tmp_path: Path) -> None:
    """Missing db field raises ProjectConfigError(kind='missing_field')."""
    _write_config(
        tmp_path / ".strata",
        'fleet_yaml = ".strata/fleet.yaml"\nsummaries_dir = ".strata/summaries"\n',
    )

    with pytest.raises(ProjectConfigError) as exc_info:
        load_project_config(start=tmp_path)

    assert exc_info.value.kind == "missing_field"
    assert "db" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test 5: non-string path field raises ProjectConfigError
# ---------------------------------------------------------------------------


def test_non_string_path_field_raises_project_config_error(tmp_path: Path) -> None:
    """A non-string value for a path field raises ProjectConfigError(kind='invalid_path')."""
    _write_config(
        tmp_path / ".strata",
        "db = 42\nfleet_yaml = \".strata/fleet.yaml\"\nsummaries_dir = \".strata/summaries\"\n",
    )

    with pytest.raises(ProjectConfigError) as exc_info:
        load_project_config(start=tmp_path)

    assert exc_info.value.kind == "invalid_path"


# ---------------------------------------------------------------------------
# Test 6: relative paths resolved absolute relative to project_root
# ---------------------------------------------------------------------------


def test_relative_paths_resolved_against_project_root(tmp_path: Path) -> None:
    """Relative paths in config.toml are resolved absolute against project_root."""
    _write_config(
        tmp_path / ".strata",
        'db = "data/records.db"\nfleet_yaml = "config/fleet.yaml"\nsummaries_dir = "summaries"\n',
    )

    result = load_project_config(start=tmp_path)

    assert result is not None
    assert result.db == (tmp_path / "data" / "records.db").resolve()
    assert result.fleet_yaml == (tmp_path / "config" / "fleet.yaml").resolve()
    assert result.summaries_dir == (tmp_path / "summaries").resolve()


# ---------------------------------------------------------------------------
# Test 7: absolute paths in config kept as-is
# ---------------------------------------------------------------------------


def test_absolute_paths_kept_absolute(tmp_path: Path) -> None:
    """Absolute paths in config.toml are used as-is (not prepended with project_root)."""
    abs_db = (tmp_path / "shared" / "strata.db").resolve()
    _write_config(
        tmp_path / ".strata",
        f'db = "{abs_db}"\n'
        f'fleet_yaml = "{tmp_path / "fleet.yaml"}"\n'
        f'summaries_dir = "{tmp_path / "summaries"}"\n',
    )

    result = load_project_config(start=tmp_path)

    assert result is not None
    assert result.db == abs_db


# ---------------------------------------------------------------------------
# Test 8: project_root is set on the returned object
# ---------------------------------------------------------------------------


def test_project_root_set_on_returned_config(tmp_path: Path) -> None:
    """project_root on the returned ProjectConfig must equal the directory containing .strata/."""
    _write_config(
        tmp_path / ".strata",
        'db = ".strata/strata.db"\nfleet_yaml = ".strata/fleet.yaml"\nsummaries_dir = ".strata/summaries"\n',
    )

    result = load_project_config(start=tmp_path)

    assert result is not None
    assert result.project_root == tmp_path.resolve()


# ---------------------------------------------------------------------------
# Test 9: config at current directory (not just ancestors)
# ---------------------------------------------------------------------------


def test_config_found_at_start_directory(tmp_path: Path) -> None:
    """load_project_config finds .strata/config.toml in the start directory itself."""
    _write_config(
        tmp_path / ".strata",
        'db = ".strata/strata.db"\nfleet_yaml = ".strata/fleet.yaml"\nsummaries_dir = ".strata/summaries"\n',
    )

    result = load_project_config(start=tmp_path)
    assert result is not None
    assert result.project_root == tmp_path.resolve()


# ---------------------------------------------------------------------------
# Test 10: all-fields-present minimal config
# ---------------------------------------------------------------------------


def test_minimal_valid_config_parses_ok(tmp_path: Path) -> None:
    """A minimal valid config with all three fields parses without error."""
    _write_config(
        tmp_path / ".strata",
        'db = ".strata/strata.db"\nfleet_yaml = ".strata/fleet.yaml"\nsummaries_dir = ".strata/summaries"\n',
    )

    result = load_project_config(start=tmp_path)
    assert result is not None
    assert isinstance(result, ProjectConfig)
