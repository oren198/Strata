"""Per-project configuration loader for Strata (ADR 0005 Decision 2).

Each foreign project gets ``.strata/config.toml`` at its root, written by
``strata register``.  The loader walks up from the current working directory
looking for this file so that the MCP server can discover project paths
without any environment variables being pre-set.

When a project config is found it takes precedence over the env-var-driven
:class:`~strata.settings.Settings` for the three storage paths
(``db_path``, ``fleet_yaml``, ``summaries_dir``).  The env vars remain
operative as fallbacks for development use-cases where Strata is run from
its own repository without a per-project config.

Example ``.strata/config.toml``::

    db = ".strata/strata.db"
    fleet_yaml = ".strata/fleet.yaml"
    summaries_dir = ".strata/summaries"

Vocabulary follows CONTEXT.md: scope, stratum, fleet, contribution,
scope-manager.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Error type (mirrors FleetConfigError pattern)
# ---------------------------------------------------------------------------


class ProjectConfigError(Exception):
    """Raised when ``.strata/config.toml`` exists but is malformed or invalid.

    Attributes:
        kind:    Short machine-readable category (e.g. ``"missing_field"``,
                 ``"bad_toml"``, ``"invalid_path"``).
        message: Human-readable description of the problem.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message

    def __str__(self) -> str:
        return f"[{self.kind}] {self.message}"


# ---------------------------------------------------------------------------
# Pydantic model
# ---------------------------------------------------------------------------


class ProjectConfig(BaseModel):
    """Resolved configuration read from ``.strata/config.toml``.

    All path fields are stored as *absolute* :class:`~pathlib.Path` objects,
    resolved relative to :attr:`project_root` at load time.

    Attributes:
        db:            Absolute path to the SQLite record store.
        fleet_yaml:    Absolute path to the fleet YAML file.
        summaries_dir: Absolute path to the per-scope summary directory.
        project_root:  Absolute path to the project root (the directory
                       containing ``.strata/config.toml``).  Not persisted to
                       the TOML file; set by :func:`load_project_config`.
    """

    db: Path
    fleet_yaml: Path
    summaries_dir: Path
    project_root: Path

    @field_validator("db", "fleet_yaml", "summaries_dir", "project_root", mode="before")
    @classmethod
    def _coerce_path(cls, v: Any) -> Path:
        return Path(v)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_project_config(start: Path | None = None) -> ProjectConfig | None:
    """Walk up from *start* (default: cwd) looking for ``.strata/config.toml``.

    Returns :data:`None` if no config is found anywhere up to the filesystem
    root.  Returns a :class:`ProjectConfig` with all paths resolved absolute
    when a config is found.

    Raises:
        ProjectConfigError: When the config file exists but contains invalid
            TOML or is missing required fields.

    Args:
        start: Directory from which to begin the walk.  Defaults to the
               current working directory.
    """
    if start is None:
        start = Path.cwd()

    current = start.resolve()

    while True:
        candidate = current / ".strata" / "config.toml"
        if candidate.exists():
            return _parse_config(candidate, project_root=current)

        parent = current.parent
        if parent == current:
            # Reached the filesystem root without finding the file.
            return None
        current = parent


def _parse_config(config_path: Path, project_root: Path) -> ProjectConfig:
    """Parse *config_path* and return an absolute :class:`ProjectConfig`.

    Args:
        config_path:  Absolute path to the ``.strata/config.toml`` file.
        project_root: Absolute path of the directory that contains ``.strata/``.

    Raises:
        ProjectConfigError: On TOML parse errors or missing/invalid fields.
    """
    try:
        raw_bytes = config_path.read_bytes()
    except OSError as exc:
        raise ProjectConfigError(
            "read_error",
            f"Cannot read {config_path}: {exc}",
        ) from exc

    try:
        data: dict[str, Any] = tomllib.loads(raw_bytes.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProjectConfigError(
            "bad_toml",
            f"TOML parse error in {config_path}: {exc}",
        ) from exc

    # Validate required fields.
    required = ("db", "fleet_yaml", "summaries_dir")
    missing = [f for f in required if f not in data]
    if missing:
        raise ProjectConfigError(
            "missing_field",
            f"Missing required field(s) in {config_path}: {', '.join(missing)}",
        )

    # Resolve paths: relative paths are resolved against project_root.
    resolved: dict[str, Path] = {}
    for field in required:
        raw_value = data[field]
        if not isinstance(raw_value, str):
            raise ProjectConfigError(
                "invalid_path",
                f"Field {field!r} in {config_path} must be a string, "
                f"got {type(raw_value).__name__}",
            )
        p = Path(raw_value)
        if not p.is_absolute():
            p = project_root / p
        resolved[field] = p.resolve()

    return ProjectConfig(
        db=resolved["db"],
        fleet_yaml=resolved["fleet_yaml"],
        summaries_dir=resolved["summaries_dir"],
        project_root=project_root,
    )
