"""Strata record store — the persistence layer for the append-only contribution log.

This module owns all SQLite access.  It is the authoritative source of truth for:
  - The **record**: the append-only, immutable log of every contribution ever
    accepted into a scope (see CONTEXT.md § Record).
  - Judgments: the scope-manager's verdict on each contribution.

Fleet configuration (strata, scopes, edges) is no longer stored here.  Under
ADR 0002 it lives in ``fleet.yaml`` and is held in memory by
:class:`~strata.fleet_config.FleetConfig`.  The ``scope_id`` column in
``contributions`` is an unvalidated string; scope-existence and active-status
checks are enforced at the application layer via the in-memory
:class:`~strata.fleet_config.FleetConfig`.

Design decisions
----------------
- Pure Python, synchronous ``sqlite3`` (stdlib).  No ORM, no SQLAlchemy.
- Foreign-key enforcement is turned on for every connection via
  ``PRAGMA foreign_keys = ON``.
- ``RecordStore`` is a context-manager-friendly class: open on construct,
  close via ``.close()`` or ``__exit__``.
- IDs are short, prefixed, human-readable in logs:
    ``c_<6hex>``  contributions
    ``j_<6hex>``  judgments

Vocabulary throughout follows CONTEXT.md verbatim.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------


def _new_contribution_id() -> str:
    # 8 bytes (16 hex chars): token_hex(3) had ~50% collision odds by ~5k
    # rows (birthday bound on 16.7M values), and a collision is an
    # IntegrityError → failed contribute.
    return f"c_{secrets.token_hex(8)}"


def _new_judgment_id() -> str:
    return f"j_{secrets.token_hex(8)}"


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContributorRef:
    """Provenance metadata for a contribution.

    Captures the contributing agent's ``(scope, skill, session, timestamp)``
    triple as required by CONTEXT.md § Provenance.
    """

    scope_id: str
    skill: str
    session_id: str
    ts: str


@dataclass(frozen=True)
class Contribution:
    """An agent's submission of memory to a scope's scope-manager.

    The ``proposed_classification`` is the contributor's hint; the scope-manager
    may reclassify it in either direction when recording a judgment.

    This record is **never** updated after it is appended — it is part of the
    append-only log (see CONTEXT.md § Record).
    """

    id: str
    scope_id: str
    content: str
    proposed_classification: Literal["directive", "context"]
    subject: str | None
    supersedes: str | None
    contributor: ContributorRef
    created_at: str


@dataclass(frozen=True)
class Judgment:
    """The scope-manager's verdict on a contribution.

    Exactly one judgment per contribution; a second attempt raises an
    ``IntegrityError`` (UNIQUE constraint on ``contribution_id``).
    """

    id: str
    contribution_id: str
    decision: Literal["accept_as_directive", "accept_as_context", "decline"]
    judged_by: str
    notes: str | None
    created_at: str


# ---------------------------------------------------------------------------
# RecordStore
# ---------------------------------------------------------------------------


class RecordStore:
    """Thin context-manager wrapper around a SQLite connection for the record store.

    Opens the connection on construction; the caller is responsible for calling
    ``.close()`` (or using the ``with`` statement).

    All writes enforce referential integrity via ``PRAGMA foreign_keys = ON``.

    Example::

        with RecordStore("./strata.db") as rs:
            c = rs.append_contribution(scope_id="g_arch", ...)
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = str(Path(db_path).expanduser())
        # check_same_thread=False: FastAPI runs a sync generator dependency
        # and the endpoint body in separate threadpool threads, so the
        # connection created in get_record_store may legally be used from a
        # different thread within the same request. Each RecordStore is
        # request-scoped and never used from two threads at once, which is
        # the condition that makes this safe (SQLite serialized mode).
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # journal_mode=WAL is NOT set here: it is persistent in the database
        # file and is applied once by run_migrations (issue #39 — re-issuing
        # it per connection needs exclusive access and raised "database is
        # locked" under concurrent requests). busy_timeout makes residual
        # write contention wait instead of failing immediately.
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.commit()

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> RecordStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # Contributions (the record)
    # ------------------------------------------------------------------

    def append_contribution(
        self,
        *,
        scope_id: str,
        content: str,
        proposed_classification: Literal["directive", "context"],
        subject: str | None,
        supersedes: str | None,
        contributor: ContributorRef,
    ) -> Contribution:
        """Append a contribution to the scope's immutable record and return it.

        This is the raw record append — classification stored here is the
        *proposed* one; the scope-manager's final verdict is recorded
        separately via :meth:`record_judgment`.

        The caller is responsible for validating that *scope_id* exists and
        is active (via the in-memory :class:`~strata.fleet_config.FleetConfig`)
        before calling this method.

        Args:
            scope_id:                The target scope (unvalidated string; no FK).
            content:                 The full text of the contribution.
            proposed_classification: Contributor's hint: ``'directive'`` or
                                     ``'context'``.
            subject:                 Optional short subject line.
            supersedes:              Optional ID of a prior contribution this
                                     one supersedes.
            contributor:             Provenance — the contributing agent's
                                     ``(scope, skill, session, timestamp)``.

        Returns:
            The newly appended :class:`Contribution`.

        Raises:
            sqlite3.IntegrityError: If *supersedes* references a non-existent
                contribution.
        """
        contribution_id = _new_contribution_id()
        self._conn.execute(
            """
            INSERT INTO contributions (
                id, scope_id, content, proposed_classification,
                subject, supersedes,
                contributor_scope_id, contributor_skill,
                contributor_session_id, contributor_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contribution_id,
                scope_id,
                content,
                proposed_classification,
                subject,
                supersedes,
                contributor.scope_id,
                contributor.skill,
                contributor.session_id,
                contributor.ts,
            ),
        )
        self._conn.commit()
        return self._fetch_contribution(contribution_id)

    def list_contributions(
        self,
        *,
        scope_id: str,
        limit: int | None = None,
    ) -> list[Contribution]:
        """Return contributions for *scope_id* ordered by ``created_at`` ascending.

        Args:
            scope_id: Filter to this scope's record.
            limit:    Maximum number of rows to return (``None`` = all).
                      When set, the *newest* ``limit`` rows are returned —
                      a recency window — still ordered oldest-first.

        Returns:
            Ordered (oldest-first) list of :class:`Contribution` objects.
        """
        base = """
            SELECT id, scope_id, content, proposed_classification,
                   subject, supersedes,
                   contributor_scope_id, contributor_skill,
                   contributor_session_id, contributor_ts,
                   created_at
            FROM contributions
            WHERE scope_id = ?
        """
        params: list[object] = [scope_id]
        if limit is not None:
            # The *newest* N rows (a "recent contributions" window for the
            # scope-manager), still presented oldest-first. ORDER ASC + LIMIT
            # would return the oldest N — the manager would judge against a
            # permanently stale window. rowid breaks same-second ties.
            sql = base + " ORDER BY created_at DESC, rowid DESC LIMIT ?"
            params.append(limit)
            rows = self._conn.execute(sql, params).fetchall()
            rows = list(reversed(rows))
        else:
            sql = base + " ORDER BY created_at ASC, rowid ASC"
            rows = self._conn.execute(sql, params).fetchall()
        return [_contribution_from_row(row) for row in rows]

    def _fetch_contribution(self, contribution_id: str) -> Contribution:
        row = self._conn.execute(
            """
            SELECT id, scope_id, content, proposed_classification,
                   subject, supersedes,
                   contributor_scope_id, contributor_skill,
                   contributor_session_id, contributor_ts,
                   created_at
            FROM contributions WHERE id = ?
            """,
            (contribution_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Contribution not found: {contribution_id!r}")
        return _contribution_from_row(row)

    # ------------------------------------------------------------------
    # Judgments
    # ------------------------------------------------------------------

    def record_judgment(
        self,
        *,
        contribution_id: str,
        decision: Literal["accept_as_directive", "accept_as_context", "decline"],
        judged_by: str,
        notes: str | None = None,
    ) -> Judgment:
        """Record the scope-manager's verdict on a contribution.

        Only one judgment is allowed per contribution; a second call raises an
        ``IntegrityError`` (UNIQUE constraint on ``contribution_id``).

        Args:
            contribution_id: The contribution being judged.
            decision:        One of ``'accept_as_directive'``,
                             ``'accept_as_context'``, or ``'decline'``.
            judged_by:       Identifier of the scope-manager (agent session or
                             system component) issuing the judgment.
            notes:           Optional free-text rationale.

        Returns:
            The newly recorded :class:`Judgment`.

        Raises:
            sqlite3.IntegrityError: If *contribution_id* does not exist (FK)
                or already has a judgment (UNIQUE).
        """
        judgment_id = _new_judgment_id()
        self._conn.execute(
            """
            INSERT INTO judgments (id, contribution_id, decision, judged_by, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (judgment_id, contribution_id, decision, judged_by, notes),
        )
        self._conn.commit()
        return self._fetch_judgment(judgment_id)

    def list_judgments(self, *, scope_id: str) -> list[Judgment]:
        """Return all judgments for contributions belonging to *scope_id*.

        Joins ``judgments`` against ``contributions`` to filter by scope.
        Results are ordered by ``judgments.created_at`` ascending.

        Args:
            scope_id: The scope whose contribution judgments to retrieve.

        Returns:
            Ordered list of :class:`Judgment` objects.
        """
        rows = self._conn.execute(
            """
            SELECT j.id, j.contribution_id, j.decision, j.judged_by, j.notes, j.created_at
            FROM judgments j
            JOIN contributions c ON j.contribution_id = c.id
            WHERE c.scope_id = ?
            ORDER BY j.created_at ASC
            """,
            (scope_id,),
        ).fetchall()
        return [Judgment(**dict(row)) for row in rows]

    def _fetch_judgment(self, judgment_id: str) -> Judgment:
        row = self._conn.execute(
            """
            SELECT id, contribution_id, decision, judged_by, notes, created_at
            FROM judgments WHERE id = ?
            """,
            (judgment_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Judgment not found: {judgment_id!r}")
        return Judgment(**dict(row))


# ---------------------------------------------------------------------------
# Row → model helpers
# ---------------------------------------------------------------------------


def _contribution_from_row(row: sqlite3.Row) -> Contribution:
    """Map a ``sqlite3.Row`` from the contributions table to a :class:`Contribution`."""
    d = dict(row)
    contributor = ContributorRef(
        scope_id=d.pop("contributor_scope_id"),
        skill=d.pop("contributor_skill"),
        session_id=d.pop("contributor_session_id"),
        ts=d.pop("contributor_ts"),
    )
    return Contribution(**d, contributor=contributor)
