"""Bootstrap configuration for the Strata fleet.

Loads a YAML fleet definition (strata, scopes, edges) and applies it to a
:class:`~strata.record_store.RecordStore`.  The operation is idempotent: safe
to run twice against the same database.

Vocabulary follows CONTEXT.md exactly: stratum, scope, edge — never level,
group, or relation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, Field

from strata.record_store import RecordStore  # noqa: E402

# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class StratumDef(BaseModel):
    """Definition of a single stratum layer from the fleet YAML."""

    id: str
    name: str
    ordinal: int


class ScopeDef(BaseModel):
    """Definition of a single scope from the fleet YAML."""

    id: str
    name: str
    stratum_id: str


class EdgeDef(BaseModel):
    """Definition of a directed edge between two scopes from the fleet YAML.

    ``from_`` maps to the YAML key ``from`` (a Python keyword) via the field
    alias.  The model is configured to accept both ``from_`` and ``from`` as
    input names.
    """

    from_: Annotated[str, Field(alias="from")]
    to: str

    model_config = {"populate_by_name": True}


class FleetConfig(BaseModel):
    """The complete fleet definition loaded from a YAML file."""

    strata: list[StratumDef]
    scopes: list[ScopeDef]
    edges: list[EdgeDef]


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


class BootstrapResult(BaseModel):
    """Summary of what was created versus what already existed during bootstrap."""

    strata_created: list[str]
    strata_existing: list[str]
    scopes_created: list[str]
    scopes_existing: list[str]
    edges_created: list[str]  # edge IDs assigned by the record store
    edges_existing: list[str]  # "from_scope_id->to_scope_id" pairs already present


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_fleet_config(yaml_path: str | Path) -> FleetConfig:
    """Parse *yaml_path* and return a validated :class:`FleetConfig`.

    Args:
        yaml_path: Path to the fleet YAML file.

    Returns:
        Parsed and validated :class:`FleetConfig`.

    Raises:
        FileNotFoundError: If *yaml_path* does not exist.
        pydantic.ValidationError: If the YAML does not match the expected
            schema (clear field-level error messages from pydantic).
    """
    path = Path(yaml_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return FleetConfig.model_validate(raw)


def apply_fleet_config(store: RecordStore, config: FleetConfig) -> BootstrapResult:
    """Apply *config* to *store*, creating any missing strata, scopes, and edges.

    Idempotency rules
    -----------------
    - Stratum: if an existing stratum has the same ``id`` *and* the same
      ``name``/``ordinal``, it is skipped (noted in ``strata_existing``).
      If the id exists but the name or ordinal differs, :exc:`ValueError` is
      raised (config drift).
    - Scope: same pattern — skip if matching, raise on drift (name or
      stratum_id mismatch).
    - Edge: if ``(from_scope_id, to_scope_id)`` already exists (detected via
      ``sqlite3.IntegrityError`` on the UNIQUE constraint), the pair is
      recorded in ``edges_existing``.

    Validation at apply time
    ------------------------
    - Every scope's ``stratum_id`` must reference a stratum defined in this
      config (or already in the DB after prior strata have been applied).
      If not, :exc:`ValueError` is raised before any DB write for that scope.
    - Every edge's ``from`` and ``to`` must reference scopes defined in this
      config (or already in the DB).  If not, :exc:`ValueError` is raised.

    Args:
        store:  An open :class:`RecordStore` to apply the config to.
        config: The fleet configuration to apply.

    Returns:
        A :class:`BootstrapResult` summarising the outcome.

    Raises:
        ValueError: On config drift (name/ordinal mismatch) or missing
            stratum/scope references in the config.
    """
    result = BootstrapResult(
        strata_created=[],
        strata_existing=[],
        scopes_created=[],
        scopes_existing=[],
        edges_created=[],
        edges_existing=[],
    )

    # Build a lookup of stratum IDs defined in this config so we can validate
    # scope references at apply time (before any writes happen for that scope).
    config_stratum_ids = {s.id for s in config.strata}

    # ------------------------------------------------------------------
    # 1. Strata
    # ------------------------------------------------------------------
    existing_strata = {s.id: s for s in store.list_strata()}

    for stratum_def in config.strata:
        if stratum_def.id in existing_strata:
            existing = existing_strata[stratum_def.id]
            if existing.name != stratum_def.name or existing.ordinal != stratum_def.ordinal:
                raise ValueError(
                    f"Config drift on stratum {stratum_def.id!r}: "
                    f"DB has name={existing.name!r}, ordinal={existing.ordinal}; "
                    f"config has name={stratum_def.name!r}, ordinal={stratum_def.ordinal}."
                )
            result.strata_existing.append(stratum_def.id)
        else:
            store.create_stratum(name=stratum_def.name, ordinal=stratum_def.ordinal)
            result.strata_created.append(stratum_def.id)

    # Refresh the stratum set (includes any that were just created) so scope
    # validation can check against the DB as well.
    db_stratum_ids = {s.id for s in store.list_strata()}

    # ------------------------------------------------------------------
    # 2. Scopes
    # ------------------------------------------------------------------
    for scope_def in config.scopes:
        # Validate that the referenced stratum exists (either in config or DB).
        sid = scope_def.stratum_id
        if sid not in db_stratum_ids and sid not in config_stratum_ids:
            raise ValueError(
                f"Scope {scope_def.id!r} references stratum {sid!r} "
                "which is not defined in the fleet config."
            )
        if sid not in db_stratum_ids:
            raise ValueError(
                f"Scope {scope_def.id!r} references stratum {sid!r} "
                "which was not found in the database after applying strata."
            )

        existing_scope = store.get_scope(scope_def.id)
        if existing_scope is not None:
            name_drift = existing_scope.name != scope_def.name
            strat_drift = existing_scope.stratum_id != scope_def.stratum_id
            if name_drift or strat_drift:
                raise ValueError(
                    f"Config drift on scope {scope_def.id!r}: "
                    f"DB has name={existing_scope.name!r}, "
                    f"stratum_id={existing_scope.stratum_id!r}; "
                    f"config has name={scope_def.name!r}, "
                    f"stratum_id={scope_def.stratum_id!r}."
                )
            result.scopes_existing.append(scope_def.id)
        else:
            # create_scope generates a random ID; we need to insert with the
            # YAML-specified ID.  We use the store's connection directly,
            # matching the store's own INSERT pattern.
            store._conn.execute(
                "INSERT INTO scopes (id, name, stratum_id) VALUES (?, ?, ?)",
                (scope_def.id, scope_def.name, scope_def.stratum_id),
            )
            store._conn.commit()
            result.scopes_created.append(scope_def.id)

    # ------------------------------------------------------------------
    # 3. Edges
    # ------------------------------------------------------------------
    # Collect all scope IDs available in the DB after above writes.
    db_scope_ids = {s.id for s in store.list_scopes()}
    config_scope_ids = {s.id for s in config.scopes}

    for edge_def in config.edges:
        # Validate scope references.
        for ref_id, direction in ((edge_def.from_, "from"), (edge_def.to, "to")):
            if ref_id not in db_scope_ids and ref_id not in config_scope_ids:
                raise ValueError(
                    f"Edge references scope {ref_id!r} ('{direction}' side) "
                    "which is not defined in the fleet config."
                )
            if ref_id not in db_scope_ids:
                raise ValueError(
                    f"Edge references scope {ref_id!r} ('{direction}' side) "
                    "which was not found in the database after applying scopes."
                )

        pair_label = f"{edge_def.from_}->{edge_def.to}"
        try:
            edge = store.add_edge(
                from_scope_id=edge_def.from_,
                to_scope_id=edge_def.to,
            )
            result.edges_created.append(edge.id)
        except sqlite3.IntegrityError:
            # UNIQUE constraint: this (from, to) pair already exists.
            result.edges_existing.append(pair_label)

    return result
