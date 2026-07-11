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


def _split_statements(sql: str) -> list[str]:
    """Split *sql* into individual statements, in file order.

    Migration files are plain DDL/DML, but statements can still legitimately
    contain semicolons inside string literals or comments (e.g. a default
    value or a descriptive comment), and more than one statement can share a
    single line. Naively splitting on ``;`` would cut string literals in
    half and miss same-line statement boundaries, so instead we accumulate
    characters and use :func:`sqlite3.complete_statement` — the same
    technique the ``sqlite3`` CLI uses — to find real statement boundaries
    wherever they fall, including mid-line.
    """
    statements: list[str] = []
    buffer = ""
    for char in sql:
        buffer += char
        if char == ";" and sqlite3.complete_statement(buffer):
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


# Every transaction-control form SQLite accepts (BEGIN, BEGIN DEFERRED,
# BEGIN IMMEDIATE TRANSACTION, COMMIT, COMMIT TRANSACTION, END, END
# TRANSACTION, ...) starts with one of these three keywords, and no
# legitimate migration DDL/DML statement does — so classification is by
# first token rather than a whitelist of exact forms.
_TRANSACTION_CONTROL_FIRST_TOKENS = {"BEGIN", "COMMIT", "END"}


def _leading_token(statement: str) -> str:
    """Return *statement*'s first SQL token, skipping leading whitespace/comments.

    Scans forward character by character instead of using a regex, so it
    never has to reason about string literals: it stops at the first
    non-whitespace, non-comment character, which for any legitimate SQL
    statement is the start of the real token — always before any string
    literal the statement might contain later on. This is what makes it
    safe against a literal containing ``--`` or ``BEGIN``: that text is
    never inspected because scanning stops at the token before reaching it.
    """
    i, n = 0, len(statement)
    while i < n:
        if statement[i].isspace():
            i += 1
        elif statement[i : i + 2] == "--":
            newline = statement.find("\n", i)
            i = n if newline == -1 else newline + 1
        elif statement[i : i + 2] == "/*":
            close = statement.find("*/", i + 2)
            i = n if close == -1 else close + 2
        else:
            break
    j = i
    while j < n and (statement[j].isalnum() or statement[j] == "_"):
        j += 1
    return statement[i:j]


def _is_transaction_control(statement: str) -> bool:
    """Return True if *statement*'s first token is ``BEGIN``, ``COMMIT``, or ``END``.

    A statement returned by :func:`_split_statements` may have leading
    comments glued to it (e.g. 0002's file-level comment block ends right
    before its ``BEGIN;``, so ``sqlite3.complete_statement`` treats them as
    one statement) — :func:`_leading_token` skips those safely before the
    comparison, so a comment-prefixed ``BEGIN``/``COMMIT`` is still
    recognised and dropped. Otherwise it would slip through as literal SQL
    and collide with the transaction ``run_migrations`` itself opens.
    """
    return _leading_token(statement).upper() in _TRANSACTION_CONTROL_FIRST_TOKENS


def _statements_for_migration(sql: str) -> list[str]:
    """Return the statements to execute for a migration file's SQL.

    Strips any ``BEGIN``/``COMMIT``/``END`` the file carries on its own —
    see ``_TRANSACTION_CONTROL_FIRST_TOKENS`` — since ``run_migrations`` now
    wraps the whole file (script + tracking row) in one explicit
    transaction.
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
