"""V1 → V1.2 fleet config exporter.

Reads the ``strata``, ``scopes``, and ``edges`` tables from a V1 SQLite DB
and writes a ``fleet.yaml`` that round-trips through :class:`FleetConfig.load`.

This module is intentionally side-effect-free with respect to the source DB:
it only issues SELECT queries, never calls ``run_migrations``, and never
modifies or drops any table.

Usage::

    from strata.fleet_export import export_fleet, TablesAbsentError

    try:
        result = export_fleet(db_path="./strata.db", out_path=Path("fleet.yaml"))
    except TablesAbsentError:
        ...

Vocabulary follows CONTEXT.md: stratum, scope, edge, fleet.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml


class TablesAbsentError(Exception):
    """Raised when the V1 fleet tables are not present in the source DB."""


@dataclass(frozen=True)
class ExportResult:
    """Summary of a completed export."""

    strata_count: int
    scopes_count: int
    edges_count: int
    out_path: Path


def _v1_tables_present(conn: sqlite3.Connection) -> bool:
    """Return True only if all three V1 fleet tables exist."""
    rows = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name IN ('strata', 'scopes', 'edges')
        """
    ).fetchall()
    return {r[0] for r in rows} == {"strata", "scopes", "edges"}


def _read_v1(conn: sqlite3.Connection) -> dict:
    """Return a raw dict suitable for ``FleetConfig.model_validate``.

    Rows are ordered deterministically: strata by ordinal, scopes and edges by
    their primary key / natural order.
    """
    strata_rows = conn.execute("SELECT id, name, ordinal FROM strata ORDER BY ordinal").fetchall()

    scopes_rows = conn.execute("SELECT id, name, stratum_id FROM scopes ORDER BY id").fetchall()

    edges_rows = conn.execute(
        "SELECT from_scope_id, to_scope_id FROM edges ORDER BY from_scope_id, to_scope_id"
    ).fetchall()

    return {
        "strata": [{"id": r[0], "name": r[1], "ordinal": r[2]} for r in strata_rows],
        "scopes": [{"id": r[0], "name": r[1], "stratum_id": r[2]} for r in scopes_rows],
        # Map V1 column names to the V1.2 YAML keys "from"/"to".
        "edges": [{"from": r[0], "to": r[1]} for r in edges_rows],
    }


def export_fleet(
    db_path: str,
    out_path: Path,
    *,
    force: bool = False,
) -> ExportResult:
    """Export V1 fleet tables to a ``fleet.yaml`` at *out_path*.

    Args:
        db_path:  Path to the V1 SQLite DB file.
        out_path: Destination for ``fleet.yaml``.
        force:    If False (default), refuse to overwrite an existing file.

    Returns:
        :class:`ExportResult` with counts and the resolved output path.

    Raises:
        TablesAbsentError: If the V1 fleet tables are not found in *db_path*.
        FileExistsError:   If *out_path* exists and *force* is False.
        FleetConfigError:  If the exported data fails :class:`FleetConfig.load`
                           validation (surfaces load-time invariant violations).
    """
    from strata.fleet_config import FleetConfig, _validate

    if not Path(db_path).exists():
        # sqlite3.connect would silently create an empty DB file at the typo'd
        # path — a side effect this read-only exporter must never have.
        raise FileNotFoundError(f"No SQLite database at {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if not _v1_tables_present(conn):
            raise TablesAbsentError(db_path)
        raw = _read_v1(conn)
    finally:
        conn.close()

    if out_path.exists() and not force:
        raise FileExistsError(out_path)

    # Validate before writing so we never leave a partial file on disk.
    candidate = FleetConfig.model_validate(raw)
    _validate(candidate)

    # Atomic write: tmp → rename.
    tmp = Path(str(out_path) + ".tmp")
    tmp.write_text(
        yaml.dump(raw, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    os.replace(tmp, out_path)

    return ExportResult(
        strata_count=len(raw["strata"]),
        scopes_count=len(raw["scopes"]),
        edges_count=len(raw["edges"]),
        out_path=out_path,
    )
