"""Migration runner for the Strata record store.

Discovers migration files in ``migrations/`` (relative to the project root,
i.e. one directory above this script) in lexicographic order, tracks which
ones have been applied in a ``_migrations`` table, and applies pending
migrations in order inside individual transactions.  Re-running is
idempotent: already-applied migrations are skipped.

Configuration (env-var-first, CLI arg overrides):
    STRATA_DB_PATH  — path to the SQLite database (default: ./strata.db)
    --db <path>     — CLI override

Vocabulary follows CONTEXT.md: scope, stratum, contribution, record, etc.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path


def _migrations_dir() -> Path:
    """Return the absolute path to the migrations/ folder.

    Resolves relative to the project root (one level above *this* script's
    directory), so the runner works regardless of the caller's cwd.
    """
    return Path(__file__).parent.parent / "migrations"


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    """Create _migrations tracking table if it does not already exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
            name        TEXT PRIMARY KEY,
            applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def _applied_migrations(conn: sqlite3.Connection) -> set[str]:
    """Return the set of migration names already recorded in _migrations."""
    rows = conn.execute("SELECT name FROM _migrations").fetchall()
    return {row[0] for row in rows}


def run_migrations(db_path: str) -> None:
    """Apply all pending migrations from migrations/ to the database at *db_path*.

    Each migration file is applied inside its own transaction.  The migration's
    filename (basename only) is recorded in ``_migrations`` on success.

    Args:
        db_path: Filesystem path to the SQLite record-store database.
    """
    migrations_dir = _migrations_dir()
    if not migrations_dir.is_dir():
        print(f"ERROR: migrations directory not found: {migrations_dir}", file=sys.stderr)
        sys.exit(1)

    sql_files = sorted(migrations_dir.glob("*.sql"))
    if not sql_files:
        print("No migration files found.")
        return

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _ensure_migrations_table(conn)
        applied = _applied_migrations(conn)

        pending = [f for f in sql_files if f.name not in applied]
        if not pending:
            print(f"No pending migrations (checked {len(sql_files)} file(s)).")
            return

        for migration_file in pending:
            sql = migration_file.read_text(encoding="utf-8")
            print(f"Applying {migration_file.name} ... ", end="", flush=True)
            try:
                with conn:  # single transaction per file
                    conn.executescript(sql)
                    # executescript issues an implicit COMMIT, so record the
                    # migration in a separate statement afterwards.
                conn.execute(
                    "INSERT INTO _migrations (name) VALUES (?)",
                    (migration_file.name,),
                )
                conn.commit()
                print("done.")
            except sqlite3.Error as exc:
                print(f"FAILED.\nERROR applying {migration_file.name}: {exc}", file=sys.stderr)
                sys.exit(1)

        print(f"Applied {len(pending)} migration(s).")
    finally:
        conn.close()


def _resolve_db_path(args: argparse.Namespace) -> str:
    """Determine the DB path: env var first, CLI arg overrides."""
    path = os.environ.get("STRATA_DB_PATH", "./strata.db")
    if args.db:
        path = args.db
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply Strata SQLite migrations.")
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="Path to the SQLite database (overrides STRATA_DB_PATH env var).",
    )
    args = parser.parse_args()
    db_path = _resolve_db_path(args)
    print(f"Using database: {db_path}")
    run_migrations(db_path)


if __name__ == "__main__":
    main()
