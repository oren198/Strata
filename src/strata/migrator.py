"""Migration runner for the Strata record store.

Discovers SQL migration files in ``strata/_migrations/`` (bundled package
data, like ``_skills``), in lexicographic order; tracks which ones have been
applied in a ``_migrations`` table; applies pending migrations one
transaction per file. Idempotent — re-running applies nothing new.

Lives inside the ``strata`` package (rather than ``scripts/``) so that the
``strata`` console script and the FastAPI app's lifespan can import it
without needing the repo root on ``sys.path``.

Vocabulary follows CONTEXT.md.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _default_migrations_dir() -> Path:
    """Resolve the bundled ``strata/_migrations/`` directory.

    Migrations ship as package data next to this file, so the same path
    works for editable installs and wheel installs (pip/pipx).
    """
    return Path(__file__).resolve().parent / "_migrations"


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    """Create the ``_migrations`` tracking table if it doesn't already exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
            name        TEXT PRIMARY KEY,
            applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def _applied_migration_names(conn: sqlite3.Connection) -> set[str]:
    """Return the set of migration filenames already recorded as applied."""
    rows = conn.execute("SELECT name FROM _migrations").fetchall()
    return {row[0] for row in rows}


def run_migrations(db_path: str, *, migrations_dir: Path | None = None) -> list[str]:
    """Apply all pending migrations from ``migrations_dir`` to *db_path*.

    Args:
        db_path:        Filesystem path to the SQLite record-store DB.
        migrations_dir: Override the default migrations directory. Useful
                        for tests; otherwise leave None and the project's
                        ``migrations/`` is used.

    Returns:
        The list of migration filenames applied during this call, in the
        order they were applied. Empty list if nothing was pending.

    Raises:
        FileNotFoundError: If *migrations_dir* doesn't exist.
        sqlite3.Error: If applying any migration fails (the partial state
            is then in whatever the migration left behind — fix the
            migration, manually clean up, and re-run).
    """
    mig_dir = migrations_dir or _default_migrations_dir()
    if not mig_dir.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {mig_dir}")

    sql_files = sorted(mig_dir.glob("*.sql"))
    if not sql_files:
        return []

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _ensure_migrations_table(conn)
        already_applied = _applied_migration_names(conn)
        pending = [f for f in sql_files if f.name not in already_applied]
        if not pending:
            return []

        applied: list[str] = []
        for migration_file in pending:
            sql = migration_file.read_text(encoding="utf-8")
            with conn:  # transaction per file
                conn.executescript(sql)
            conn.execute(
                "INSERT INTO _migrations (name) VALUES (?)",
                (migration_file.name,),
            )
            conn.commit()
            applied.append(migration_file.name)
        return applied
    finally:
        conn.close()
