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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, field_validator

if TYPE_CHECKING:
    from strata.settings import Settings

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


def load_project_config(
    start: Path | None = None,
    *,
    searched_paths_out: list[Path] | None = None,
) -> ProjectConfig | None:
    """Walk up from *start* (default: cwd) looking for ``.strata/config.toml``.

    Returns :data:`None` if no config is found anywhere up to the filesystem
    root.  Returns a :class:`ProjectConfig` with all paths resolved absolute
    when a config is found.

    Raises:
        ProjectConfigError: When the config file exists but contains invalid
            TOML or is missing required fields.

    Args:
        start:              Directory from which to begin the walk.  Defaults
                            to the current working directory.
        searched_paths_out: When provided, populated with every
                            ``.strata/config.toml`` path the loader examined
                            during the walk.  Useful for error messages that
                            need to show the user where the loader looked.
    """
    if start is None:
        start = Path.cwd()

    current = start.resolve()

    while True:
        candidate = current / ".strata" / "config.toml"
        if searched_paths_out is not None:
            searched_paths_out.append(candidate)
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


# ---------------------------------------------------------------------------
# Storage-path resolution — the single source of truth (issue #44)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoragePaths:
    """The one resolved answer to "where does Strata state live?".

    Every entry point (CLI, FastAPI backend, MCP server) must obtain its
    storage paths through :func:`resolve_storage_paths` so they can never
    diverge (ROADMAP enduring principle: single source of truth).

    Attributes:
        db_path:         SQLite record-store path.
        summaries_dir:   Per-scope summary directory.
        fleet_yaml_path: Fleet YAML path.
        source:          ``"project"`` when ``.strata/config.toml`` won,
                         ``"env"`` when env-var settings were used.
        project_root:    The registered project root when ``source ==
                         "project"``; ``None`` otherwise.
    """

    db_path: str
    summaries_dir: str
    fleet_yaml_path: str
    source: Literal["project", "env"]
    project_root: Path | None


def resolve_storage_paths(
    settings: Settings | None = None,
    *,
    start: Path | None = None,
) -> StoragePaths:
    """Resolve storage paths: project config wins, env settings are the fallback.

    Precedence (ADR 0005 Decision 2, extended to all entry points by #44):

    1. ``.strata/config.toml`` discovered by walking up from *start* (cwd).
    2. Env-var-driven :class:`~strata.settings.Settings` defaults.

    Args:
        settings: Optional pre-built settings (tests / ``create_app``).
                  When None, the cached :func:`~strata.settings.get_settings`
                  singleton is used for the fallback values.
        start:    Directory to begin the config walk-up from (default: cwd).
    """
    project = load_project_config(start)
    if project is not None:
        return StoragePaths(
            db_path=str(project.db),
            summaries_dir=str(project.summaries_dir),
            fleet_yaml_path=str(project.fleet_yaml),
            source="project",
            project_root=project.project_root,
        )

    if settings is None:
        from strata.settings import get_settings  # local import — avoid cycle

        settings = get_settings()
    return StoragePaths(
        db_path=settings.db_path,
        summaries_dir=settings.summaries_dir,
        fleet_yaml_path=settings.fleet_yaml_path,
        source="env",
        project_root=None,
    )
