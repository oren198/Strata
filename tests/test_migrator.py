"""End-to-end tests for the SQL migration runner against the bundled
``migrations/`` directory.

The unit-level tests in ``test_cli.py`` mock the runner; this module exercises
real SQL against a fresh SQLite file to catch data-preservation regressions
in the migrations themselves.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from strata.migrator import (
    _is_transaction_control,
    _leading_token,
    _split_statements,
    _statements_for_migration,
    run_migrations,
)


def _table_names(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def test_full_chain_drops_fleet_tables_and_preserves_record(tmp_path: Path) -> None:
    """0001 + 0002 applied in order: fleet tables gone, contributions + judgments preserved.

    Guards the rebuild-with-temp-backup pattern in 0002 against future regressions
    where dropping ``judgments`` ahead of the contributions rebuild loses data.
    """
    db_path = str(tmp_path / "migrate.db")

    # Apply 0001 first (alone), then seed sample data.
    migrations_dir = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"
    only_0001_dir = tmp_path / "migrations_step1"
    only_0001_dir.mkdir()
    (only_0001_dir / "0001_initial.sql").write_text(
        (migrations_dir / "0001_initial.sql").read_text()
    )
    applied = run_migrations(db_path, migrations_dir=only_0001_dir)
    assert applied == ["0001_initial.sql"]

    # Seed a contribution + judgment + supporting fleet rows.
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L0', 'Top', 0)")
    conn.execute("INSERT INTO scopes (id, name, stratum_id) VALUES ('g_a', 'A', 'L0')")
    conn.execute(
        """INSERT INTO contributions
        (id, scope_id, content, proposed_classification, subject, supersedes,
         contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts)
        VALUES ('c_1', 'g_a', 'hello', 'context', NULL, NULL,
                'g_a', 'tester', 's_1', '2026-05-27T00:00:00Z')"""
    )
    # A second contribution superseding the first — exercises the rebuilt
    # contributions.supersedes self-FK during the INSERT ... SELECT under
    # foreign_keys = ON.
    conn.execute(
        """INSERT INTO contributions
        (id, scope_id, content, proposed_classification, subject, supersedes,
         contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts)
        VALUES ('c_2', 'g_a', 'hello v2', 'directive', NULL, 'c_1',
                'g_a', 'tester', 's_1', '2026-05-27T01:00:00Z')"""
    )
    conn.execute(
        """INSERT INTO judgments
        (id, contribution_id, decision, judged_by, notes)
        VALUES ('j_1', 'c_1', 'accept_as_context', 'scope-manager', 'looks fine')"""
    )
    conn.commit()
    conn.close()

    # Now apply the remaining real migrations (0002 rebuild + 0003 + 0004),
    # all pending after the 0001-only seed above.
    applied = run_migrations(db_path, migrations_dir=migrations_dir)
    assert applied == [
        "0002_drop_fleet_tables.sql",
        "0003_judgment_attempts.sql",
        "0004_operator.sql",
        "0005_publication.sql",
        "0006_optional_skill.sql",
    ]

    # Fleet tables gone.
    tables = _table_names(db_path)
    assert "strata" not in tables
    assert "scopes" not in tables
    assert "edges" not in tables
    assert "judgments_backup" not in tables  # cleanup succeeded
    assert "contributions" in tables
    assert "judgments" in tables

    # Contributions and judgments preserved end-to-end, including the
    # supersedes self-reference (c_2 → c_1) carried through the rebuild.
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        contribs = conn.execute(
            "SELECT id, scope_id, content, supersedes FROM contributions ORDER BY id"
        ).fetchall()
        judgments = conn.execute(
            "SELECT id, contribution_id, decision FROM judgments ORDER BY id"
        ).fetchall()
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()

    assert contribs == [
        ("c_1", "g_a", "hello", None),
        ("c_2", "g_a", "hello v2", "c_1"),
    ]
    assert judgments == [("j_1", "c_1", "accept_as_context")]
    assert fk_violations == []


def test_0006_makes_skill_nullable_and_preserves_rows(tmp_path: Path) -> None:
    """0006 rebuilds contributions + publication_acts with skill nullable while
    preserving every existing row, self-reference, and dependent judgment/attempt
    (issue #121).

    Seeds real data through 0005 (skill NOT NULL still), then applies 0006 and
    checks: existing skill values survive verbatim, a skill-less row can now be
    inserted, and the self-referential + FK graph stays intact.
    """
    db_path = str(tmp_path / "skill.db")
    migrations_dir = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"

    # Apply 0001..0005 only (skill columns still NOT NULL there).
    through_0005 = tmp_path / "through_0005"
    through_0005.mkdir()
    for f in sorted(migrations_dir.glob("*.sql")):
        if f.name.startswith("0006"):
            continue
        (through_0005 / f.name).write_text(f.read_text())
    run_migrations(db_path, migrations_dir=through_0005)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    # A supersession chain (c_2 -> c_1), a judgment and a failed attempt.
    conn.execute(
        """INSERT INTO contributions
        (id, scope_id, content, proposed_classification, subject, supersedes,
         contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts)
        VALUES ('c_1', 'g_a', 'v1', 'directive', NULL, NULL,
                'g_a', 'code-writer', 's_1', '2026-07-21T00:00:00Z')"""
    )
    conn.execute(
        """INSERT INTO contributions
        (id, scope_id, content, proposed_classification, subject, supersedes,
         contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts)
        VALUES ('c_2', 'g_a', 'v2', 'directive', NULL, 'c_1',
                'g_a', 'code-writer', 's_1', '2026-07-21T01:00:00Z')"""
    )
    conn.execute(
        "INSERT INTO judgments (id, contribution_id, decision, judged_by) VALUES (?, ?, ?, ?)",
        ("j_1", "c_1", "accept_as_directive", "scope-manager"),
    )
    conn.execute(
        "INSERT INTO judgment_attempts (id, contribution_id, error_class) VALUES (?, ?, ?)",
        ("ja_1", "c_2", "ValueError"),
    )
    # A publish + withdraw chain (pub_2 withdraws pub_1) and a judgment.
    conn.execute(
        """INSERT INTO publication_acts
        (id, scope_id, act, kind, content, subject, anchors, withdraws, "trigger",
         proposer_scope_id, proposer_skill, proposer_session_id, proposer_ts)
        VALUES ('pub_1', 'g_a', 'publish', 'directive', 'x', NULL, '[]', NULL, NULL,
                'g_a', 'scope-manager', 's_1', '2026-07-21T00:00:00Z')"""
    )
    conn.execute(
        """INSERT INTO publication_acts
        (id, scope_id, act, kind, content, subject, anchors, withdraws, "trigger",
         proposer_scope_id, proposer_skill, proposer_session_id, proposer_ts)
        VALUES ('pub_2', 'g_a', 'withdraw', NULL, NULL, NULL, NULL, 'pub_1', NULL,
                'g_a', 'scope-manager', 's_1', '2026-07-21T02:00:00Z')"""
    )
    conn.execute(
        "INSERT INTO publication_judgments (id, act_id, decision, judged_by) VALUES (?, ?, ?, ?)",
        ("pubj_1", "pub_1", "accept", "scope-manager"),
    )
    conn.commit()
    conn.close()

    # Apply 0006.
    applied = run_migrations(db_path, migrations_dir=migrations_dir)
    assert applied == ["0006_optional_skill.sql"]

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        # Existing rows + self-references + skill values preserved verbatim.
        assert conn.execute(
            "SELECT id, supersedes, contributor_skill FROM contributions ORDER BY id"
        ).fetchall() == [("c_1", None, "code-writer"), ("c_2", "c_1", "code-writer")]
        assert conn.execute("SELECT id, contribution_id FROM judgments").fetchall() == [
            ("j_1", "c_1")
        ]
        assert conn.execute("SELECT id, contribution_id FROM judgment_attempts").fetchall() == [
            ("ja_1", "c_2")
        ]
        assert conn.execute(
            "SELECT id, withdraws, proposer_skill FROM publication_acts ORDER BY id"
        ).fetchall() == [("pub_1", None, "scope-manager"), ("pub_2", "pub_1", "scope-manager")]
        assert conn.execute("SELECT id, act_id FROM publication_judgments").fetchall() == [
            ("pubj_1", "pub_1")
        ]

        # Skill columns are now nullable — a skill-less row inserts cleanly.
        conn.execute(
            """INSERT INTO contributions
            (id, scope_id, content, proposed_classification, subject, supersedes,
             contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts)
            VALUES ('c_ns', 'g_a', 'no skill', 'context', NULL, NULL,
                    'g_a', NULL, 's_1', '2026-07-21T03:00:00Z')"""
        )
        conn.execute(
            """INSERT INTO publication_acts
            (id, scope_id, act, kind, content, subject, anchors, withdraws, "trigger",
             proposer_scope_id, proposer_skill, proposer_session_id, proposer_ts)
            VALUES ('pub_ns', 'g_a', 'publish', 'context', 'y', NULL, '[]', NULL, NULL,
                    'g_a', NULL, 's_1', '2026-07-21T04:00:00Z')"""
        )
        conn.commit()
        assert conn.execute(
            "SELECT contributor_skill FROM contributions WHERE id = 'c_ns'"
        ).fetchone() == (None,)
        assert conn.execute(
            "SELECT proposer_skill FROM publication_acts WHERE id = 'pub_ns'"
        ).fetchone() == (None,)

        # contributor_scope_id / proposer_scope_id stay NOT NULL — identity is
        # still mandatory (issue #121).
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO contributions
                (id, scope_id, content, proposed_classification, subject, supersedes,
                 contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts)
                VALUES ('c_bad', 'g_a', 'x', 'context', NULL, NULL,
                        NULL, NULL, 's_1', '2026-07-21T05:00:00Z')"""
            )

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_idempotent_reapply(tmp_path: Path) -> None:
    """Re-running migrations after a full apply is a no-op."""
    db_path = str(tmp_path / "idempotent.db")
    migrations_dir = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"

    first = run_migrations(db_path, migrations_dir=migrations_dir)
    assert first == [
        "0001_initial.sql",
        "0002_drop_fleet_tables.sql",
        "0003_judgment_attempts.sql",
        "0004_operator.sql",
        "0005_publication.sql",
        "0006_optional_skill.sql",
    ]

    second = run_migrations(db_path, migrations_dir=migrations_dir)
    assert second == []


@pytest.mark.parametrize("decision", ["accept_as_directive", "accept_as_context", "decline"])
def test_judgment_check_constraint_survives_rebuild(tmp_path: Path, decision: str) -> None:
    """The CHECK constraint on ``judgments.decision`` is reinstated after 0002.

    Guards against the rebuild silently widening the schema.
    """
    db_path = str(tmp_path / f"check_{decision}.db")
    migrations_dir = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"
    run_migrations(db_path, migrations_dir=migrations_dir)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """INSERT INTO contributions
        (id, scope_id, content, proposed_classification, subject, supersedes,
         contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts)
        VALUES ('c_x', 'g_a', 'hi', 'context', NULL, NULL,
                'g_a', 'tester', 's_1', '2026-05-27T00:00:00Z')"""
    )
    conn.execute(
        "INSERT INTO judgments (id, contribution_id, decision, judged_by) VALUES (?, ?, ?, ?)",
        ("j_x", "c_x", decision, "scope-manager"),
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO judgments (id, contribution_id, decision, judged_by) VALUES (?, ?, ?, ?)",
            ("j_bad", "c_x", "not_a_valid_decision", "scope-manager"),
        )

    conn.close()


# ---------------------------------------------------------------------------
# Crash-mid-migration tests (issue #58): the migration script and its
# ``_migrations`` tracking row must commit or roll back together, atomically,
# so a crash at any point during a migration leaves the DB such that a plain
# re-run converges with no manual intervention.
# ---------------------------------------------------------------------------


def test_crash_mid_script_leaves_no_half_schema_and_no_tracking_row(tmp_path: Path) -> None:
    """A migration whose second statement is invalid SQL leaves nothing behind.

    The first statement (a CREATE TABLE) would succeed if run on its own,
    but the whole file now runs in one explicit transaction, so the second
    statement's failure rolls the first one back too — no half-applied
    schema — and the tracking row for this migration is never inserted.
    """
    db_path = str(tmp_path / "crash.db")
    bad_migrations_dir = tmp_path / "migrations_bad"
    bad_migrations_dir.mkdir()
    (bad_migrations_dir / "0001_bad.sql").write_text(
        "CREATE TABLE half_applied (id TEXT PRIMARY KEY);\nTHIS IS NOT VALID SQL;\n"
    )

    with pytest.raises(sqlite3.OperationalError):
        run_migrations(db_path, migrations_dir=bad_migrations_dir)

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        tracked = conn.execute(
            "SELECT name FROM _migrations WHERE name = '0001_bad.sql'"
        ).fetchall()
    finally:
        conn.close()

    # No half-schema: the CREATE TABLE that ran before the crash was rolled back.
    assert "half_applied" not in tables
    # No tracking row: the migration was never recorded as applied.
    assert tracked == []


def test_reapply_after_crash_converges(tmp_path: Path) -> None:
    """After a crashed migration file is fixed, a plain re-run applies it cleanly.

    No manual cleanup is needed: the crash left no tracking row and no
    partial schema (see the test above), so the fixed file is simply picked
    up as pending, exactly as if it had never been attempted.
    """
    db_path = str(tmp_path / "crash.db")
    migrations_dir = tmp_path / "migrations_bad"
    migrations_dir.mkdir()
    bad_file = migrations_dir / "0001_bad.sql"
    bad_file.write_text(
        "CREATE TABLE half_applied (id TEXT PRIMARY KEY);\nTHIS IS NOT VALID SQL;\n"
    )

    with pytest.raises(sqlite3.OperationalError):
        run_migrations(db_path, migrations_dir=migrations_dir)

    # An operator fixes the migration file; nothing else is touched.
    bad_file.write_text("CREATE TABLE half_applied (id TEXT PRIMARY KEY);\n")

    applied = run_migrations(db_path, migrations_dir=migrations_dir)
    assert applied == ["0001_bad.sql"]

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        tracked = conn.execute(
            "SELECT name FROM _migrations WHERE name = '0001_bad.sql'"
        ).fetchall()
    finally:
        conn.close()

    assert "half_applied" in tables
    assert tracked == [("0001_bad.sql",)]


class _CrashOnStatement:
    """Wraps a real ``sqlite3.Connection``; raises the first time *trigger*
    appears in an executed statement.

    Used to simulate a crash at a precise point inside ``run_migrations``'s
    per-file transaction — e.g. exactly at the tracking ``INSERT``, after
    the migration script's own statements have already run but not yet
    committed.
    """

    def __init__(self, real: sqlite3.Connection, trigger: str) -> None:
        self._real = real
        self._trigger = trigger
        self._raised = False

    def execute(self, sql: str, parameters: tuple = ()) -> sqlite3.Cursor:
        if not self._raised and self._trigger in sql:
            self._raised = True
            raise sqlite3.OperationalError("simulated crash")
        return self._real.execute(sql, parameters)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def test_crash_at_tracking_insert_rolls_back_script_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Applied but unrecorded" is impossible: script + tracking row are one transaction.

    Before this fix, ``executescript`` committed the migration's own
    statements (or, for a self-wrapping file like 0002, its BEGIN…COMMIT
    committed) as soon as it returned, and the tracking ``INSERT`` was a
    *separate* transaction — a crash between the two would leave the
    script's schema changes applied with no tracking row. This test
    crashes exactly at the tracking ``INSERT`` and checks that the script's
    own effects are rolled back right along with it, and that a plain
    re-run afterwards converges.
    """
    db_path = str(tmp_path / "atomic.db")
    migrations_dir = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"

    real_connect = sqlite3.connect

    def fake_connect(path: str, *args: object, **kwargs: object) -> _CrashOnStatement:
        real_conn = real_connect(path, *args, **kwargs)
        return _CrashOnStatement(real_conn, trigger="INSERT INTO _migrations")

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    with pytest.raises(sqlite3.OperationalError):
        run_migrations(db_path, migrations_dir=migrations_dir)
    monkeypatch.undo()

    conn = real_connect(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        tracked = conn.execute("SELECT name FROM _migrations").fetchall()
    finally:
        conn.close()

    # 0001's own schema changes were rolled back along with the failed
    # tracking INSERT — no "applied but unrecorded" state.
    assert "strata" not in tables
    assert tracked == []

    # A plain re-run (no monkeypatch) converges cleanly with no manual
    # intervention.
    applied = run_migrations(db_path, migrations_dir=migrations_dir)
    assert applied == [
        "0001_initial.sql",
        "0002_drop_fleet_tables.sql",
        "0003_judgment_attempts.sql",
        "0004_operator.sql",
        "0005_publication.sql",
        "0006_optional_skill.sql",
    ]


# ---------------------------------------------------------------------------
# Statement-splitting / transaction-control classification tests (issue #76).
#
# Two latent defects, neither reachable from the shipped migration files:
#   1. The old line-based splitter accumulated whole lines, so a one-line
#      multi-statement input never split within the line.
#   2. The old comment-stripping regexes in _is_transaction_control did not
#      respect string literals, so a literal containing "--" or "BEGIN"
#      could cause a false classification.
# ---------------------------------------------------------------------------


def test_two_statements_on_one_line_both_apply(tmp_path: Path) -> None:
    """A single line carrying two statements is split and both execute.

    Before the character-level rewrite, ``_split_statements`` accumulated
    whole lines, so this one-liner came back as a single string and
    ``conn.execute`` raised "You can only execute one statement at a time."
    """
    db_path = str(tmp_path / "oneline.db")
    migrations_dir = tmp_path / "migrations_oneline"
    migrations_dir.mkdir()
    (migrations_dir / "0001_two_on_one_line.sql").write_text(
        "CREATE TABLE a (x TEXT); CREATE TABLE b (y TEXT);\n"
    )

    applied = run_migrations(db_path, migrations_dir=migrations_dir)
    assert applied == ["0001_two_on_one_line.sql"]

    tables = _table_names(db_path)
    assert "a" in tables
    assert "b" in tables


def test_string_literal_with_double_dash_and_begin_is_executed(tmp_path: Path) -> None:
    """A literal containing '--' or 'BEGIN' is executed, not dropped or misclassified.

    Guards against the old regex-based comment stripping in
    ``_is_transaction_control``, which didn't respect string literals and
    could misclassify a statement whose literal payload merely contains
    ``--`` or ``BEGIN`` as transaction control.
    """
    db_path = str(tmp_path / "literal.db")
    migrations_dir = tmp_path / "migrations_literal"
    migrations_dir.mkdir()
    (migrations_dir / "0001_literal.sql").write_text(
        "CREATE TABLE notes (id TEXT PRIMARY KEY, body TEXT);\n"
        "INSERT INTO notes (id, body) VALUES "
        "('n1', 'contains -- not a comment and BEGIN not a keyword');\n"
    )

    applied = run_migrations(db_path, migrations_dir=migrations_dir)
    assert applied == ["0001_literal.sql"]

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT id, body FROM notes").fetchall()
    finally:
        conn.close()
    assert rows == [("n1", "contains -- not a comment and BEGIN not a keyword")]


def test_is_transaction_control_ignores_string_literal_content() -> None:
    """Direct unit check: a literal's ``BEGIN``/``--`` never leaks into classification."""
    statement = "SELECT 'BEGIN this is not a real BEGIN, and -- not a comment either';"
    assert not _is_transaction_control(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE beginnings (id TEXT);",
        "INSERT INTO t (x) VALUES (1);",
        "SELECT 1;",
        "-- leading comment\nCREATE TABLE t (id TEXT);",
        "/* block comment */ DROP TABLE t;",
        "BEGINNING_OF_SOMETHING (id TEXT);",  # first token merely starts with BEGIN
        "ENDPOINT (id TEXT);",  # first token merely starts with END
    ],
)
def test_statement_with_non_control_first_token_is_never_transaction_control(
    statement: str,
) -> None:
    """A statement whose first token isn't BEGIN/COMMIT/END can never be tx control."""
    assert not _is_transaction_control(statement)


@pytest.mark.parametrize("keyword", ["BEGIN", "COMMIT", "END", "begin", "Commit", "end"])
def test_statement_with_control_first_token_is_always_transaction_control(keyword: str) -> None:
    """Every case variant of the three control keywords is classified as tx control,
    including when a comment is glued in front of it (see 0002's file-level comment
    block preceding its ``BEGIN;``)."""
    assert _is_transaction_control(f"{keyword};")
    assert _is_transaction_control(f"{keyword} TRANSACTION;")
    assert _is_transaction_control(f"  -- leading comment\n{keyword};")
    assert _is_transaction_control(f"/* block */ {keyword};")


def test_statements_for_migration_strips_transaction_control_from_0002() -> None:
    """No BEGIN/COMMIT statement from 0002's own file-level wrapper reaches the runner."""
    migrations_dir = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"
    sql = (migrations_dir / "0002_drop_fleet_tables.sql").read_text(encoding="utf-8")
    statements = _statements_for_migration(sql)
    assert statements  # sanity: the file has real DDL/DML left after stripping
    assert not any(_is_transaction_control(s) for s in statements)
    firsts = {_leading_token(s).upper() for s in statements}
    assert "BEGIN" not in firsts
    assert "COMMIT" not in firsts


def _old_split_statements(sql: str) -> list[str]:
    """Reimplementation of the pre-issue-#76 line-based splitter.

    ``_split_statements`` used to accumulate whole *lines* (rather than
    characters) before checking :func:`sqlite3.complete_statement`. Kept
    here only to pin the new character-level splitter's output against the
    old one for every shipped migration file below — none of them has more
    than one statement per line, so the two splitters must agree exactly.
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
    trailing = buffer.strip()
    if trailing:
        statements.append(trailing)
    return statements


def _shipped_migration_files() -> list[Path]:
    migrations_dir = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"
    return sorted(migrations_dir.glob("*.sql"))


@pytest.mark.parametrize("migration_file", _shipped_migration_files(), ids=lambda p: p.name)
def test_split_statements_matches_pre_76_line_based_splitter_for_shipped_migrations(
    migration_file: Path,
) -> None:
    """Regression pin: the char-level splitter agrees with the old line-based one.

    Every shipped migration file is one-statement-per-line, so re-splitting
    with the new character-driven ``_split_statements`` must reproduce
    exactly the statement list the old line-based splitter produced —
    confirming issue #76's fix is behavior-preserving for real migrations.
    """
    sql = migration_file.read_text(encoding="utf-8")
    assert _split_statements(sql) == _old_split_statements(sql)
