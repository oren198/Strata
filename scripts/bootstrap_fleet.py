"""CLI wrapper for the Strata fleet bootstrap.

Loads a YAML fleet config and applies it to a record-store database,
creating any missing strata, scopes, and edges.  The operation is idempotent.

Configuration (env-var-first, CLI args override):
    STRATA_FLEET_CONFIG  — path to the fleet YAML  (default: ./fleet.yaml)
    STRATA_DB_PATH       — path to the SQLite DB   (default: ./strata.db)

Usage::

    python scripts/bootstrap_fleet.py [--config PATH] [--db PATH]

Vocabulary follows CONTEXT.md: stratum, scope, edge.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make scripts/ importable so run_migrations is importable even when this
# script is invoked directly (not through the installed package).
sys.path.insert(0, str(Path(__file__).parent))

from run_migrations import run_migrations  # noqa: E402

# Make the src tree importable when running without ``pip install -e .``.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.bootstrap import apply_fleet_config, load_fleet_config  # noqa: E402
from strata.record_store import RecordStore  # noqa: E402


def _resolve_config_path(args: argparse.Namespace) -> str:
    path = os.environ.get("STRATA_FLEET_CONFIG", "./fleet.yaml")
    if args.config:
        path = args.config
    return path


def _resolve_db_path(args: argparse.Namespace) -> str:
    path = os.environ.get("STRATA_DB_PATH", "./strata.db")
    if args.db:
        path = args.db
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Bootstrap a Strata fleet from a YAML config.")
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Path to the fleet YAML config (overrides STRATA_FLEET_CONFIG).",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="Path to the SQLite database (overrides STRATA_DB_PATH).",
    )
    args = parser.parse_args(argv)

    config_path = _resolve_config_path(args)
    db_path = _resolve_db_path(args)

    # Ensure the DB schema exists before opening the store.
    run_migrations(db_path)

    config = load_fleet_config(config_path)

    with RecordStore(db_path) as store:
        result = apply_fleet_config(store, config)

    print(f"Fleet bootstrapped from {config_path}:")
    print(f"  strata: {len(result.strata_created)} created, {len(result.strata_existing)} existing")
    print(f"  scopes: {len(result.scopes_created)} created, {len(result.scopes_existing)} existing")
    print(f"  edges:  {len(result.edges_created)} created, {len(result.edges_existing)} existing")


if __name__ == "__main__":
    main()
