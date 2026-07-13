"""Shared embedded-store access for the CLI inspection commands (issue #64).

``strata scopes`` / ``strata summary`` / ``strata record`` used to be HTTP
clients of the Console backend (``strata start``).  Per ADR 0004 Decision 1
(embedded mode), the backend is the Console UI's server and nothing more —
every other consumer reads ``fleet.yaml`` and the SQLite/markdown stores
directly.  This module is the one small helper the three inspection commands
share to do that.

Deliberately NOT reused by ``strata.app`` (FastAPI, request-scoped stores via
``Depends``) or ``strata.mcp.server`` (long-lived process-global singletons,
see ``_init_stores``) — their store lifecycles differ from a one-shot CLI
invocation and unifying them is deferred to issue #83.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from strata.fleet_config import FleetConfig, FleetConfigError
from strata.migrator import run_migrations
from strata.project_config import resolve_storage_paths
from strata.record_store import RecordStore
from strata.summary_store import SummaryStore


class EmbeddedStoreError(Exception):
    """Raised when the embedded stores cannot be opened.

    ``message`` is already formatted for ``print(..., file=sys.stderr)``.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class EmbeddedStores:
    """Opened embedded-mode handles for one CLI invocation (ADR 0004 D1)."""

    fleet_config: FleetConfig
    record_store: RecordStore
    summary_store: SummaryStore

    def close(self) -> None:
        self.record_store.close()

    def __enter__(self) -> EmbeddedStores:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_embedded_stores() -> EmbeddedStores:
    """Resolve storage paths, apply pending migrations, and open the stores.

    Mirrors the embedded wiring in ``strata.mcp.server._init_stores``: load
    ``fleet.yaml`` first (so a missing/invalid config fails fast with an
    actionable message before touching the DB), migrate the SQLite DB, then
    open ``RecordStore`` and ``SummaryStore`` directly — no backend involved.

    Raises:
        EmbeddedStoreError: with a print-ready message when the fleet config
            is missing or fails validation.
    """
    paths = resolve_storage_paths()
    fleet_path = Path(paths.fleet_yaml_path)
    if not fleet_path.exists():
        raise EmbeddedStoreError(
            f"No fleet config found at {fleet_path}.\n"
            "  In a registered project: run `strata register` from the project root.\n"
            "  In the Strata repo: run `strata start` once to seed fleet.yaml, "
            "or set STRATA_FLEET_CONFIG."
        )
    try:
        fleet_config = FleetConfig.load(fleet_path)
    except FleetConfigError as exc:
        raise EmbeddedStoreError(f"Fleet config invalid [{exc.kind}]: {exc.message}") from exc

    run_migrations(paths.db_path)
    record_store = RecordStore(paths.db_path)
    summary_store = SummaryStore(paths.summaries_dir)
    return EmbeddedStores(
        fleet_config=fleet_config,
        record_store=record_store,
        summary_store=summary_store,
    )
