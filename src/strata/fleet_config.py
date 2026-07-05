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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class EntitlementView:
    """A scope's entitlement surface, derived from ``fleet.yaml`` (ADR 0006 D2).

    Groups every scope in the fleet relative to one judged scope:

    - ``chain`` — the scope itself plus its inter-stratum ancestors (root
      first, scope last). Entitled for both directives and context — this is
      the binding surface.
    - ``descendants`` — every active scope below the judged scope (its
      authority region), any depth. Entitled: evidence proposed upward from
      below is the normal, legitimate inflow the scope-manager exists to
      judge — the evidence→ratification channel (philosophy Concept 3), and
      ADR 0006 D1 permits exactly these agents to write here. Without this
      group the rendered ENTITLEMENT block would instruct the judge to
      decline the very flow D1 legitimizes.
    - ``referenced_peers`` — active scopes referenced one hop away via an
      intra-stratum edge from any scope on ``chain`` (edges where the
      target's stratum ordinal equals the source's). Entitled for context
      only, never a directive at the contributor's request (CONTEXT.md
      § Intra-stratum edge). No transitive peer-of-peer traversal — only
      edges whose source is itself on ``chain`` count.
    - ``others`` — every remaining scope in the fleet, **including archived
      scopes** (archived chain members excepted — the chain is structural).
      The judge distinguishes fleet-internal origins from external material
      by exact name matching against this enumeration; an archived scope
      that vanished from the list would read as external and slip past the
      admission rule. Not entitled: material substantively originating from
      these scopes must not enter the judged scope.

    This is the single source of truth for entitlement grouping (ROADMAP
    principle 8): :mod:`strata.scope_manager` renders it into the judge's
    user message, and ADR 0006 D3's peer-reference composition reuses the
    same grouping.
    """

    chain: list[Scope]
    descendants: list[Scope]
    referenced_peers: list[Scope]
    others: list[Scope]


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

    def inter_stratum_parent(self, scope_id: str) -> Scope | None:
        """Return the single inter-stratum parent of *scope_id*, or ``None`` for root scopes.

        Edges are written child→parent (from=child, to=parent) in fleet.yaml.
        A parent has a *lower* stratum ordinal than its child (per ADR 0002,
        ordinal 0 is the broadest stratum). An edge to a scope with a *higher*
        ordinal is a descendant reference, not a parent reference, and is
        never followed here. Peer (same-ordinal) edges are likewise ignored.
        """
        stratum_map = {s.id: s for s in self.strata}
        scope_map = {s.id: s for s in self.scopes}

        current = scope_map.get(scope_id)
        if current is None:
            return None

        current_ordinal = stratum_map[current.stratum_id].ordinal

        for edge in self.edges:
            if edge.from_ != scope_id:
                continue
            target = scope_map.get(edge.to)
            if target is None:
                continue
            target_ordinal = stratum_map[target.stratum_id].ordinal
            if target_ordinal < current_ordinal:
                return target

        return None

    def inter_stratum_ancestors(self, scope_id: str) -> list[Scope]:
        """Return the ancestor chain from root (L0) down to *scope_id*'s parent.

        Follows inter-stratum-only edges (child→parent convention in fleet.yaml).
        Returns an empty list when *scope_id* is a root scope (no inter-stratum
        parent).  The requested scope itself is NOT included — callers append it.
        """
        ancestors: list[Scope] = []
        current_id = scope_id
        while True:
            parent = self.inter_stratum_parent(current_id)
            if parent is None:
                break
            ancestors.append(parent)
            current_id = parent.id
        # Chain is built deepest-first; reverse to get root-first order.
        ancestors.reverse()
        return ancestors

    def entitlement_view(self, scope_id: str) -> EntitlementView:
        """Compute *scope_id*'s entitlement surface (ADR 0006 D2).

        Args:
            scope_id: The scope the view is relative to (the scope about to
                be judged).

        Returns:
            An :class:`EntitlementView` grouping the fleet's scopes into
            ``chain`` (this scope + inter-stratum ancestors), ``descendants``
            (active scopes below this scope — entitled upward-evidence
            sources), ``referenced_peers`` (one hop via intra-stratum edges
            from any chain scope), and ``others`` (everything else,
            archived scopes included).
        """
        stratum_map = {s.id: s for s in self.strata}
        scope_map = {s.id: s for s in self.scopes}

        scope = scope_map.get(scope_id)
        ancestors = self.inter_stratum_ancestors(scope_id)
        chain = [*ancestors, *([scope] if scope is not None else [])]
        chain_ids = {s.id for s in chain}

        # Descendants: every active scope whose own ancestor chain passes
        # through the judged scope (any depth). These are the agents ADR 0006
        # D1 permits to propose upward into this scope, so the judge must see
        # them as entitled evidence sources, never as foreign material.
        descendant_ids: set[str] = set()
        descendants: list[Scope] = []
        for candidate in self.scopes:
            if candidate.id in chain_ids or candidate.status != "active":
                continue
            if any(a.id == scope_id for a in self.inter_stratum_ancestors(candidate.id)):
                descendant_ids.add(candidate.id)
                descendants.append(candidate)

        referenced_peer_ids: list[str] = []
        seen: set[str] = set()
        for edge in self.edges:
            if edge.from_ not in chain_ids or edge.to in chain_ids or edge.to in seen:
                continue
            from_scope = scope_map.get(edge.from_)
            target_scope = scope_map.get(edge.to)
            if from_scope is None or target_scope is None or target_scope.status != "active":
                continue
            from_ordinal = stratum_map[from_scope.stratum_id].ordinal
            target_ordinal = stratum_map[target_scope.stratum_id].ordinal
            if target_ordinal != from_ordinal:
                continue
            seen.add(edge.to)
            referenced_peer_ids.append(edge.to)

        referenced_peers = [scope_map[sid] for sid in referenced_peer_ids]

        # Everything else — including archived scopes, so every fleet name
        # the judge might meet in prose appears in exactly one group and an
        # archived origin cannot masquerade as external material.
        others = [
            s
            for s in self.scopes
            if s.id not in chain_ids and s.id not in seen and s.id not in descendant_ids
        ]

        return EntitlementView(
            chain=chain,
            descendants=descendants,
            referenced_peers=referenced_peers,
            others=others,
        )

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
    """Validate all load-time invariants from ADR 0002 (8 original) and ADR 0004 (1 new).

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
                message=(f"Edge from={edge.from_!r} references a scope not defined in fleet.yaml."),
            )
        if edge.to not in scope_map:
            raise FleetConfigError(
                kind="unknown_scope_ref",
                message=(f"Edge to={edge.to!r} references a scope not defined in fleet.yaml."),
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

    # 9. Each scope may have at most one inter-stratum-parent edge (i.e. at
    #    most one outgoing edge whose target has a strictly lower stratum
    #    ordinal).  Multiple such edges would create ambiguity about which
    #    scope carries the authoritative parent perspective (ADR 0004 D4).
    inter_stratum_parent_count: dict[str, int] = {}
    for edge in config.edges:
        from_scope = scope_map[edge.from_]
        to_scope = scope_map[edge.to]
        from_ordinal = stratum_map[from_scope.stratum_id].ordinal
        to_ordinal = stratum_map[to_scope.stratum_id].ordinal
        if to_ordinal < from_ordinal:
            inter_stratum_parent_count[edge.from_] = (
                inter_stratum_parent_count.get(edge.from_, 0) + 1
            )
    for scope_id, count in inter_stratum_parent_count.items():
        if count > 1:
            raise FleetConfigError(
                kind="multiple_inter_stratum_parents",
                message=(
                    f"Scope {scope_id!r} has {count} inter-stratum-parent edges; "
                    "each scope may have at most one."
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
