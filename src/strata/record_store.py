"""Strata record store — the persistence layer for the append-only contribution log
and fleet configuration (scopes, strata, edges).

This module owns all SQLite access.  It is the authoritative source of truth for:
  - The **record**: the append-only, immutable log of every contribution ever
    accepted into a scope (see CONTEXT.md § Record).
  - Fleet configuration: strata, scopes, and the inter/intra-stratum edges
    between them.
  - Judgments: the scope-manager's verdict on each contribution.

Design decisions
----------------
- Pure Python, synchronous ``sqlite3`` (stdlib).  No ORM, no SQLAlchemy.
- Foreign-key enforcement is turned on for every connection via
  ``PRAGMA foreign_keys = ON``.
- ``RecordStore`` is a context-manager-friendly class: open on construct,
  close via ``.close()`` or ``__exit__``.
- IDs are short, prefixed, human-readable in logs:
    ``L<n>``      strata      (e.g. "L1")
    ``g_<6hex>``  scopes      (groups in UI vocabulary)
    ``e_<6hex>``  edges
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


def _new_scope_id() -> str:
    return f"g_{secrets.token_hex(3)}"


def _new_edge_id() -> str:
    return f"e_{secrets.token_hex(3)}"


def _new_contribution_id() -> str:
    return f"c_{secrets.token_hex(3)}"


def _new_judgment_id() -> str:
    return f"j_{secrets.token_hex(3)}"


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stratum:
    """A horizontal layer of scopes.

    ``ordinal`` establishes the broadcast order of directives: 0 is the
    broadest stratum (directives from here bind everyone), higher ordinals are
    progressively narrower.
    """

    id: str
    name: str
    ordinal: int
    created_at: str


@dataclass(frozen=True)
class Scope:
    """A bounded region of the fleet for which memory is relevant and authoritative.

    Every scope belongs to exactly one stratum.
    """

    id: str
    name: str
    stratum_id: str
    created_at: str


@dataclass(frozen=True)
class Edge:
    """A directed link between two scopes.

    Edges may span at most one stratum (``abs(from.ordinal - to.ordinal) <= 1``).
    Self-loops are forbidden.  Intra-stratum edges are peer references; inter-
    stratum edges connect a scope to its parent or child stratum.
    """

    id: str
    from_scope_id: str
    to_scope_id: str
    created_at: str


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
            layer = rs.create_stratum(name="executive", ordinal=0)
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = str(Path(db_path).expanduser())
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
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
    # Strata
    # ------------------------------------------------------------------

    def create_stratum(self, *, name: str, ordinal: int) -> Stratum:
        """Insert a new stratum and return it.

        A stratum's ``ordinal`` must be unique; SQLite will raise an
        ``IntegrityError`` on collision (UNIQUE constraint on ``ordinal``).

        The ID is derived from the ordinal as ``L<ordinal>`` so that log
        entries are self-explanatory (e.g. ``L0`` for the broadest stratum).

        Args:
            name:    Human-readable label for this stratum layer.
            ordinal: Position in the broadcast order (0 = broadest).

        Returns:
            The newly created :class:`Stratum`.

        Raises:
            sqlite3.IntegrityError: If *ordinal* is already in use.
        """
        stratum_id = f"L{ordinal}"
        self._conn.execute(
            "INSERT INTO strata (id, name, ordinal) VALUES (?, ?, ?)",
            (stratum_id, name, ordinal),
        )
        self._conn.commit()
        return self._fetch_stratum(stratum_id)

    def list_strata(self) -> list[Stratum]:
        """Return all strata ordered by ``ordinal`` ascending (0 = broadest first)."""
        rows = self._conn.execute(
            "SELECT id, name, ordinal, created_at FROM strata ORDER BY ordinal"
        ).fetchall()
        return [Stratum(**dict(row)) for row in rows]

    def _fetch_stratum(self, stratum_id: str) -> Stratum:
        row = self._conn.execute(
            "SELECT id, name, ordinal, created_at FROM strata WHERE id = ?",
            (stratum_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Stratum not found: {stratum_id!r}")
        return Stratum(**dict(row))

    # ------------------------------------------------------------------
    # Scopes
    # ------------------------------------------------------------------

    def create_scope(self, *, name: str, stratum_id: str) -> Scope:
        """Insert a new scope in the given stratum and return it.

        Args:
            name:       Human-readable label for the scope.
            stratum_id: ID of an existing stratum this scope belongs to.

        Returns:
            The newly created :class:`Scope`.

        Raises:
            sqlite3.IntegrityError: If *stratum_id* does not reference an
                existing stratum (FK constraint).
        """
        scope_id = _new_scope_id()
        self._conn.execute(
            "INSERT INTO scopes (id, name, stratum_id) VALUES (?, ?, ?)",
            (scope_id, name, stratum_id),
        )
        self._conn.commit()
        return self._fetch_scope(scope_id)

    def get_scope(self, scope_id: str) -> Scope | None:
        """Return the scope with *scope_id*, or ``None`` if it does not exist."""
        row = self._conn.execute(
            "SELECT id, name, stratum_id, created_at FROM scopes WHERE id = ?",
            (scope_id,),
        ).fetchone()
        if row is None:
            return None
        return Scope(**dict(row))

    def list_scopes(self) -> list[Scope]:
        """Return all scopes (unordered)."""
        rows = self._conn.execute("SELECT id, name, stratum_id, created_at FROM scopes").fetchall()
        return [Scope(**dict(row)) for row in rows]

    def _fetch_scope(self, scope_id: str) -> Scope:
        scope = self.get_scope(scope_id)
        if scope is None:
            raise KeyError(f"Scope not found: {scope_id!r}")
        return scope

    # ------------------------------------------------------------------
    # Edges
    # ------------------------------------------------------------------

    def add_edge(self, *, from_scope_id: str, to_scope_id: str) -> Edge:
        """Add a directed edge between two scopes and return it.

        Enforces two structural rules from ADR 0001:
          1. **No self-loops** — ``from_scope_id != to_scope_id``.
          2. **±1 stratum constraint** — the two scopes must be on the same
             stratum or adjacent strata (``|from.ordinal - to.ordinal| <= 1``).

        Args:
            from_scope_id: Source scope.
            to_scope_id:   Target scope.

        Returns:
            The newly created :class:`Edge`.

        Raises:
            ValueError:            On self-loop or stratum distance > 1.
            KeyError:              If either scope does not exist.
            sqlite3.IntegrityError: On duplicate edge (UNIQUE constraint).
        """
        if from_scope_id == to_scope_id:
            raise ValueError(
                f"Self-loop forbidden: scope {from_scope_id!r} cannot reference itself."
            )
        from_scope = self._fetch_scope(from_scope_id)
        to_scope = self._fetch_scope(to_scope_id)

        from_stratum = self._fetch_stratum(from_scope.stratum_id)
        to_stratum = self._fetch_stratum(to_scope.stratum_id)

        if abs(from_stratum.ordinal - to_stratum.ordinal) > 1:
            raise ValueError(
                f"Edge from {from_scope_id!r} (stratum ordinal {from_stratum.ordinal}) "
                f"to {to_scope_id!r} (stratum ordinal {to_stratum.ordinal}) spans more "
                f"than one stratum layer (distance="
                f"{abs(from_stratum.ordinal - to_stratum.ordinal)}).  "
                "Edges must stay within ±1 stratum."
            )

        edge_id = _new_edge_id()
        self._conn.execute(
            "INSERT INTO edges (id, from_scope_id, to_scope_id) VALUES (?, ?, ?)",
            (edge_id, from_scope_id, to_scope_id),
        )
        self._conn.commit()
        return self._fetch_edge(edge_id)

    def list_edges(self) -> list[Edge]:
        """Return all edges (unordered)."""
        rows = self._conn.execute(
            "SELECT id, from_scope_id, to_scope_id, created_at FROM edges"
        ).fetchall()
        return [Edge(**dict(row)) for row in rows]

    def _fetch_edge(self, edge_id: str) -> Edge:
        row = self._conn.execute(
            "SELECT id, from_scope_id, to_scope_id, created_at FROM edges WHERE id = ?",
            (edge_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Edge not found: {edge_id!r}")
        return Edge(**dict(row))

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

        Args:
            scope_id:                The target scope.
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
            sqlite3.IntegrityError: If *scope_id* does not exist, or if
                *supersedes* references a non-existent contribution.
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

        Returns:
            Ordered list of :class:`Contribution` objects.
        """
        sql = """
            SELECT id, scope_id, content, proposed_classification,
                   subject, supersedes,
                   contributor_scope_id, contributor_skill,
                   contributor_session_id, contributor_ts,
                   created_at
            FROM contributions
            WHERE scope_id = ?
            ORDER BY created_at ASC
        """
        params: list[object] = [scope_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
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
