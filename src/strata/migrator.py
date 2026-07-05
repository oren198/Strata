"""Migration runner for the Strata record store.

Discovers SQL migration files in ``strata/_migrations/`` (bundled package
data, like ``_skills``), in lexicographic order; tracks which ones have been
applied in a ``_migrations`` table; applies pending migrations one
transaction per file — the migration script *and* its ``_migrations``
tracking row commit or roll back together, atomically, so a crash at any
point during a migration can never leave the script's effects applied
without a tracking row (or vice versa). Idempotent — re-running applies
nothing new.

Each migration file is split into individual statements (comments and
semicolons inside string literals are respected via
:func:`sqlite3.complete_statement`) and any ``BEGIN``/``COMMIT`` statements
the file carries are stripped, since the runner now supplies the one real
transaction that wraps the whole file. This means ``conn.executescript``
is deliberately not used: it commits any pending transaction and runs in
autocommit mode, which would make the tracking row a separate transaction
from the script it tracks.

Lives inside the ``strata`` package (rather than ``scripts/``) so that the
``strata`` console script and the FastAPI app's lifespan can import it
without needing the repo root on ``sys.path``.

Vocabulary follows CONTEXT.md.
"""

from __future__ import annotations

import re
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


# Transaction-control statements a migration file might carry on its own
# (e.g. 0002 wraps its drop-and-rebuild in BEGIN…COMMIT). The runner now
# supplies the single real transaction for the whole file, so these are
# stripped rather than executed — nesting them would either error
# ("cannot start a transaction within a transaction") or, worse, prematurely
# commit the runner's own transaction.
_TRANSACTION_CONTROL_STATEMENTS = {
    "BEGIN",
    "BEGIN DEFERRED",
    "BEGIN DEFERRED TRANSACTION",
    "BEGIN IMMEDIATE",
    "BEGIN IMMEDIATE TRANSACTION",
    "BEGIN EXCLUSIVE",
    "BEGIN EXCLUSIVE TRANSACTION",
    "BEGIN TRANSACTION",
    "COMMIT",
    "COMMIT TRANSACTION",
    "END",
    "END TRANSACTION",
}


def _split_statements(sql: str) -> list[str]:
    """Split *sql* into individual statements, in file order.

    Migration files are plain DDL/DML, but statements can still legitimately
    contain semicolons inside string literals or comments (e.g. a default
    value or a descriptive comment). Naively splitting on ``;`` would cut
    those in half, so instead we accumulate lines and use
    :func:`sqlite3.complete_statement` — the same technique the ``sqlite3``
    CLI uses — to find real statement boundaries.
    """
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    # A trailing fragment with no closing ';' (or one that is only comments)
    # is either empty or genuinely malformed; either way, hand it to sqlite
    # verbatim rather than silently dropping it.
    trailing = buffer.strip()
    if trailing:
        statements.append(trailing)
    return statements


_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _is_transaction_control(statement: str) -> bool:
    """Return True if *statement* is a bare ``BEGIN``/``COMMIT``/``END``.

    A statement returned by :func:`_split_statements` may have leading
    comments glued to it (e.g. 0002's file-level comment block ends right
    before its ``BEGIN;``, so ``sqlite3.complete_statement`` treats them as
    one statement). Comments are stripped before comparing so a
    comment-prefixed ``BEGIN``/``COMMIT`` is still recognised and dropped —
    otherwise it would slip through as literal SQL and collide with the
    transaction ``run_migrations`` itself opens.
    """
    without_comments = _BLOCK_COMMENT_RE.sub("", statement)
    without_comments = _LINE_COMMENT_RE.sub("", without_comments)
    normalized = without_comments.strip().rstrip(";").strip().upper()
    return normalized in _TRANSACTION_CONTROL_STATEMENTS


def _statements_for_migration(sql: str) -> list[str]:
    """Return the statements to execute for a migration file's SQL.

    Strips any ``BEGIN``/``COMMIT`` the file carries on its own — see
    ``_TRANSACTION_CONTROL_STATEMENTS`` — since ``run_migrations`` now wraps
    the whole file (script + tracking row) in one explicit transaction.
    """
    statements = _split_statements(sql)
    return [s for s in statements if not _is_transaction_control(s)]


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
        sqlite3.Error: If applying any migration fails. The migration script
            and its ``_migrations`` tracking row execute in one explicit
            transaction, so a failure rolls back *both*: the DB is left
            exactly as it was before this migration started (no half-applied
            schema, no "applied but unrecorded" row). Fix the migration file
            and simply re-run — no manual cleanup needed.
    """
    mig_dir = migrations_dir or _default_migrations_dir()
    if not mig_dir.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {mig_dir}")

    sql_files = sorted(mig_dir.glob("*.sql"))
    if not sql_files:
        return []

    conn = sqlite3.connect(db_path)
    try:
        # WAL is set once here rather than per connection: journal_mode is
        # persistent in the database file, and re-issuing it on live
        # connections can require exclusive access (issue #39).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        _ensure_migrations_table(conn)
        already_applied = _applied_migration_names(conn)
        pending = [f for f in sql_files if f.name not in already_applied]
        if not pending:
            return []

        applied: list[str] = []
        for migration_file in pending:
            sql = migration_file.read_text(encoding="utf-8")
            statements = _statements_for_migration(sql)

            # One real transaction per file: the script's statements AND the
            # tracking INSERT commit together, or neither does. `conn.execute`
            # doesn't implicitly open a transaction for DDL (only for
            # INSERT/UPDATE/DELETE/REPLACE under the legacy transaction
            # handling this module relies on — see py311 sqlite3 docs), so we
            # open the transaction explicitly rather than depending on that.
            conn.execute("BEGIN")
            try:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO _migrations (name) VALUES (?)",
                    (migration_file.name,),
                )
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            applied.append(migration_file.name)
        return applied
    finally:
        conn.close()
