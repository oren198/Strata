"""Thin CLI wrapper around ``strata.migrator.run_migrations``.

Kept for ``make migrate`` compatibility. Real implementation lives in
``src/strata/migrator.py`` so that the ``strata`` console script and the
FastAPI lifespan can import it without ``scripts/`` being on ``sys.path``.

Usage:
    python scripts/run_migrations.py [--db PATH]

Configuration (env-var-first, CLI arg overrides):
    STRATA_DB_PATH  — path to the SQLite database (default: ./strata.db)
"""

from __future__ import annotations

import argparse
import os
import sys

from strata.migrator import run_migrations


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply Strata SQLite migrations.")
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="Path to the SQLite database (overrides STRATA_DB_PATH env var).",
    )
    args = parser.parse_args()
    db_path = args.db or os.environ.get("STRATA_DB_PATH", "./strata.db")
    print(f"Using database: {db_path}")
    try:
        applied = run_migrations(db_path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    if applied:
        print(f"Applied {len(applied)} migration(s):")
        for name in applied:
            print(f"  · {name}")
    else:
        print("No pending migrations.")


if __name__ == "__main__":
    main()
