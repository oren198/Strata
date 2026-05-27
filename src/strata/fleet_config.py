"""Fleet configuration — the in-memory mirror of ``fleet.yaml``.

This module is the single owner of ``fleet.yaml`` ↔ in-memory state under
ADR 0002. It loads, validates, and mutates fleet configuration, ensuring
``fleet.yaml`` is always the source of truth.

- :class:`FleetConfig` is the top-level model and public API.
- All mutations acquire an in-process lock, write atomically, and refresh
  in-memory state from the rendered output.
- Validation raises :class:`FleetConfigError` on the first failure; the
  ``kind`` attribute identifies which invariant was violated.

Vocabulary follows CONTEXT.md verbatim: stratum, scope, edge, fleet.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class FleetConfigError(Exception):
    """Raised when a fleet.yaml invariant is violated.

    ``kind`` is a stable token identifying the invariant; ``message`` is
    human-readable and always names the offending item.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class Stratum(BaseModel):
    """A horizontal layer of scopes."""

    id: str
    name: str
    ordinal: int


class Scope(BaseModel):
    """A bounded region of the fleet for which memory is relevant and authoritative."""

    id: str
    name: str
    stratum_id: str
    status: Literal["active", "archived"] = "active"
    default_skill: str | None = None
    permitted_skills: list[str] | None = None


class Edge(BaseModel):
    """A directed link between two scopes.

    ``from_`` maps to the YAML key ``from`` (a Python keyword) via the alias.
    """

    from_: Annotated[str, Field(alias="from")]
    to: str

    model_config = {"populate_by_name": True}


class FleetConfig(BaseModel):
    """The complete fleet definition loaded from a YAML file.

    Instantiate via :meth:`FleetConfig.load` — the classmethod validates all
    8 load-time invariants from ADR 0002.  Direct construction skips
    validation; prefer ``load`` in production code.
    """

    strata: list[Stratum]
    scopes: list[Scope]
    edges: list[Edge]

    # File path and lock are set by ``load``; not part of the schema.
    _path: Path | None = None
    _lock: threading.Lock | None = None

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> FleetConfig:
        """Parse *path*, validate all 8 load-time invariants, and return a
        :class:`FleetConfig`.

        Args:
            path: Path to a ``fleet.yaml`` file.

        Returns:
            Validated :class:`FleetConfig` with ``_path`` and ``_lock`` set.

        Raises:
            FileNotFoundError: If *path* does not exist.
            FleetConfigError:  On the first invariant violation; ``kind`` names
                the check, ``message`` names the offending item.
        """
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        config = cls.model_validate(raw)
        _validate(config)
        object.__setattr__(config, "_path", path)
        object.__setattr__(config, "_lock", threading.Lock())
        return config

    # ------------------------------------------------------------------
    # Convenience queries
    # ------------------------------------------------------------------

    def get_scope(self, scope_id: str) -> Scope | None:
        """Return the scope with *scope_id*, or ``None``."""
        return next((s for s in self.scopes if s.id == scope_id), None)

    def active_scopes(self) -> list[Scope]:
        """Return only scopes with ``status == 'active'``."""
        return [s for s in self.scopes if s.status == "active"]

    # ------------------------------------------------------------------
    # Mutation API
    # ------------------------------------------------------------------

    def add_stratum(self, *, id: str, name: str, ordinal: int) -> None:
        """Add a new stratum to the fleet config and persist to disk.

        Raises:
            FleetConfigError: If the ID or ordinal duplicates an existing
                stratum (invariants 1 and 3).
        """
        assert self._path is not None and self._lock is not None
        with self._lock:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
            raw.setdefault("strata", [])
            raw.setdefault("scopes", [])
            raw.setdefault("edges", [])
            raw["strata"].append({"id": id, "name": name, "ordinal": ordinal})
            candidate = FleetConfig.model_validate(raw)
            _validate(candidate)
            _atomic_write(self._path, raw)
            refreshed = FleetConfig.model_validate(
                yaml.safe_load(self._path.read_text(encoding="utf-8"))
            )
            self.__dict__.update(refreshed.__dict__)

    def add_scope(
        self,
        *,
        id: str,
        name: str,
        stratum_id: str,
        status: Literal["active", "archived"] = "active",
        default_skill: str | None = None,
        permitted_skills: list[str] | None = None,
    ) -> None:
        """Add a new scope and persist to disk.

        Raises:
            FleetConfigError: On duplicate ID (invariant 2) or unknown
                stratum (invariant 4) or skill drift (invariant 8).
        """
        assert self._path is not None and self._lock is not None
        with self._lock:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
            raw.setdefault("strata", [])
            raw.setdefault("scopes", [])
            raw.setdefault("edges", [])
            entry: dict = {"id": id, "name": name, "stratum_id": stratum_id}
            if status != "active":
                entry["status"] = status
            if default_skill is not None:
                entry["default_skill"] = default_skill
            if permitted_skills is not None:
                entry["permitted_skills"] = permitted_skills
            raw["scopes"].append(entry)
            candidate = FleetConfig.model_validate(raw)
            _validate(candidate)
            _atomic_write(self._path, raw)
            refreshed = FleetConfig.model_validate(
                yaml.safe_load(self._path.read_text(encoding="utf-8"))
            )
            self.__dict__.update(refreshed.__dict__)

    def add_edge(self, *, from_scope_id: str, to_scope_id: str) -> None:
        """Add a directed edge and persist to disk.

        Raises:
            FleetConfigError: On self-loop (invariant 6), ±1 stratum
                violation (invariant 7), or unknown scope (invariant 5).
        """
        assert self._path is not None and self._lock is not None
        with self._lock:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
            raw.setdefault("strata", [])
            raw.setdefault("scopes", [])
            raw.setdefault("edges", [])
            raw["edges"].append({"from": from_scope_id, "to": to_scope_id})
            candidate = FleetConfig.model_validate(raw)
            _validate(candidate)
            _atomic_write(self._path, raw)
            refreshed = FleetConfig.model_validate(
                yaml.safe_load(self._path.read_text(encoding="utf-8"))
            )
            self.__dict__.update(refreshed.__dict__)

    def archive_scope(self, scope_id: str) -> None:
        """Set ``status: archived`` on *scope_id* and persist to disk.

        Raises:
            FleetConfigError: If *scope_id* does not exist.
        """
        assert self._path is not None and self._lock is not None
        with self._lock:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
            matched = False
            for entry in raw.get("scopes", []):
                if entry.get("id") == scope_id:
                    entry["status"] = "archived"
                    matched = True
                    break
            if not matched:
                raise FleetConfigError(
                    kind="scope_not_found",
                    message=f"Scope {scope_id!r} not found in fleet config.",
                )
            candidate = FleetConfig.model_validate(raw)
            _validate(candidate)
            _atomic_write(self._path, raw)
            refreshed = FleetConfig.model_validate(
                yaml.safe_load(self._path.read_text(encoding="utf-8"))
            )
            self.__dict__.update(refreshed.__dict__)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate(config: FleetConfig) -> None:
    """Validate all 8 load-time invariants from ADR 0002.

    Raises :class:`FleetConfigError` on the first failure.
    """
    # 1. Duplicate stratum IDs.
    stratum_ids: list[str] = [s.id for s in config.strata]
    seen: set[str] = set()
    for sid in stratum_ids:
        if sid in seen:
            raise FleetConfigError(
                kind="duplicate_stratum_id",
                message=f"Duplicate stratum ID: {sid!r}.",
            )
        seen.add(sid)

    # 2. Duplicate scope IDs.
    seen = set()
    for scope in config.scopes:
        if scope.id in seen:
            raise FleetConfigError(
                kind="duplicate_scope_id",
                message=f"Duplicate scope ID: {scope.id!r}.",
            )
        seen.add(scope.id)

    # 3. Duplicate stratum ordinals.
    seen_ordinals: set[int] = set()
    for stratum in config.strata:
        if stratum.ordinal in seen_ordinals:
            raise FleetConfigError(
                kind="duplicate_stratum_ordinal",
                message=f"Duplicate stratum ordinal {stratum.ordinal} on stratum {stratum.id!r}.",
            )
        seen_ordinals.add(stratum.ordinal)

    # Build lookup maps for subsequent checks.
    stratum_map: dict[str, Stratum] = {s.id: s for s in config.strata}
    scope_map: dict[str, Scope] = {s.id: s for s in config.scopes}

    # 4. Scope stratum_id references a defined stratum.
    for scope in config.scopes:
        if scope.stratum_id not in stratum_map:
            raise FleetConfigError(
                kind="unknown_stratum_ref",
                message=(
                    f"Scope {scope.id!r} references stratum {scope.stratum_id!r} "
                    "which is not defined in fleet.yaml."
                ),
            )

    # 5. Edge endpoints reference defined scopes.
    for edge in config.edges:
        if edge.from_ not in scope_map:
            raise FleetConfigError(
                kind="unknown_scope_ref",
                message=(
                    f"Edge from={edge.from_!r} references a scope not defined in fleet.yaml."
                ),
            )
        if edge.to not in scope_map:
            raise FleetConfigError(
                kind="unknown_scope_ref",
                message=(
                    f"Edge to={edge.to!r} references a scope not defined in fleet.yaml."
                ),
            )

    # 6. No self-loops.
    for edge in config.edges:
        if edge.from_ == edge.to:
            raise FleetConfigError(
                kind="self_loop",
                message=f"Self-loop forbidden: scope {edge.from_!r} references itself.",
            )

    # 7. ±1 stratum-distance constraint.
    for edge in config.edges:
        from_scope = scope_map[edge.from_]
        to_scope = scope_map[edge.to]
        from_ordinal = stratum_map[from_scope.stratum_id].ordinal
        to_ordinal = stratum_map[to_scope.stratum_id].ordinal
        distance = abs(from_ordinal - to_ordinal)
        if distance > 1:
            raise FleetConfigError(
                kind="stratum_distance_violation",
                message=(
                    f"Edge from {edge.from_!r} (ordinal {from_ordinal}) "
                    f"to {edge.to!r} (ordinal {to_ordinal}) spans {distance} strata; "
                    "edges must stay within ±1 stratum."
                ),
            )

    # 8. default_skill must be in permitted_skills when both are set.
    for scope in config.scopes:
        if (
            scope.default_skill is not None
            and scope.permitted_skills is not None
            and scope.default_skill not in scope.permitted_skills
        ):
            raise FleetConfigError(
                kind="skill_drift",
                message=(
                    f"Scope {scope.id!r}: default_skill {scope.default_skill!r} "
                    f"is not in permitted_skills {scope.permitted_skills!r}."
                ),
            )


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, data: object) -> None:
    """Render *data* as YAML and write atomically to *path*."""
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, path)
