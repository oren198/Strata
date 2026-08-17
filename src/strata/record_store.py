"""Strata record store — the persistence layer for the append-only contribution log.

This module owns all SQLite access.  It is the authoritative source of truth for:
  - The **record**: the append-only, immutable log of every contribution ever
    accepted into a scope (see CONTEXT.md § Record).
  - Judgments: the scope-manager's verdict on each contribution, plus the
    failed-judgment *events* that are deliberately not verdicts — carrying
    the mechanical ``judge_failed`` marker (issue #118) when the judge run
    ended in failure, so "attempted, judge errored" is legible on a read
    surface instead of looking like "no verdict yet".
  - The operator's own record (``operator_acts``) and retirement events
    (``retirements``) — ADR 0008. Two separate tables because the operator
    acts in two capacities: writing the operator stratum itself (not judged,
    never enters a scope's record) versus correcting a scope's native memory
    in person (a judgment, lives in that scope's own record). See
    :mod:`strata.operator` for the primitives built on top of these tables.
  - The publication channel's own record (``publication_acts`` +
    ``publication_judgments``) — ADR 0007. Every publish/withdraw act on a
    scope's curated outward face, distinct from its contribution record; see
    :mod:`strata.publication` for the primitives built on top of these
    tables.

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
    ``c_<6hex>``   contributions
    ``j_<6hex>``   judgments
    ``op_<6hex>``  operator acts (ADR 0008)
    ``ret_<6hex>`` retirement events (ADR 0008)
    ``pub_<6hex>`` publication acts (ADR 0007) — a publish act's id doubles
                   as its published item's id
    ``pubj_<6hex>`` publication judgments (ADR 0007)

Vocabulary throughout follows CONTEXT.md verbatim.
"""

from __future__ import annotations

import json
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


def _new_judgment_attempt_id() -> str:
    return f"ja_{secrets.token_hex(8)}"


def _new_operator_act_id() -> str:
    # ADR 0008 D1: operator-stratum act ids are prefixed op_ so they are
    # never mistaken for a contribution id (c_) when they appear as a
    # `supersedes`/`retires` reference — operator-stratum acts never enter a
    # scope's record and vice versa (two capacities, two records).
    return f"op_{secrets.token_hex(8)}"


def _new_retirement_id() -> str:
    return f"ret_{secrets.token_hex(8)}"


def _new_publication_act_id() -> str:
    # ADR 0007 D1/D2: publication acts are prefixed pub_ — this id doubles as
    # a published item's own id once accepted (see strata.publication).
    return f"pub_{secrets.token_hex(8)}"


def _new_publication_judgment_id() -> str:
    return f"pubj_{secrets.token_hex(8)}"


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContributorRef:
    """Provenance metadata for a contribution.

    Captures the contributing agent's ``(scope, skill, session, timestamp)``
    provenance as required by CONTEXT.md § Provenance. ``skill`` is optional
    (issue #121): agent identity is scope + session, and a skill carries a
    body or it is omitted — so a skill-less binding records ``skill=None``
    rather than a bare placeholder name.
    """

    scope_id: str
    skill: str | None
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


#: The mechanical failed-judgment marker (issue #118).  Written on a
#: :class:`JudgmentAttempt` when the scope-manager's ``judge()`` call failed and
#: its own corrective re-asks are exhausted, so the judge run is over and the
#: contribution is stranded until an explicit re-judge.  Mechanical: no judge or
#: LLM decides this, and it is NOT a verdict — see :class:`JudgmentAttempt`.
JUDGE_FAILED = "judge_failed"


#: The engine default for the recency window (ADR 0011 D2): how many of the
#: newest contributions :meth:`RecordStore.list_recent_contributions` returns
#: to the judgment and refresh paths.  Deployments override it via
#: ``Settings.recency_window_size`` (``STRATA_RECENCY_WINDOW_SIZE``); this
#: constant is the fallback for library callers with no settings in hand.
RECENCY_WINDOW_SIZE = 20


#: The engine default for a record page (issue #130): how many contributions
#: one :meth:`RecordStore.page_record` page carries when the caller does not
#: name a size.  20 matches the recency window the judgment path already reads
#: (:data:`RECENCY_WINDOW_SIZE`) — the same "how much recent record is worth
#: looking at" answer, asked by a reader instead of a judge.  Deployments
#: override it via ``Settings.record_page_size``
#: (``STRATA_RECORD_PAGE_SIZE``); this constant is the fallback for library
#: callers with no settings in hand.
RECORD_PAGE_SIZE = 20


@dataclass(frozen=True)
class JudgmentAttempt:
    """A record of the scope-manager's judgment *failing* on a contribution.

    An event, never a verdict (issue #57): a verdict is an exercise of scope
    authority and only the scope-manager (or operator) may write one, so a
    ``judge()`` failure — API outage, malformed model output — is recorded
    here rather than fabricated as a ``decline``.  Append-only: a contribution
    may accumulate several attempts and still, later, gain exactly one
    judgment once a re-judge succeeds.

    ``outcome`` is :data:`JUDGE_FAILED` when this attempt ended the judge run
    (issue #118), and ``None`` when nothing asserts that it did — every row
    written before the marker existed, so an orphan from before #118 is never
    retroactively claimed to have failed terminally.
    """

    id: str
    contribution_id: str
    error_class: str
    message: str | None
    attempted_at: str
    outcome: Literal["judge_failed"] | None = None


@dataclass(frozen=True)
class ContributionState:
    """One contribution's judgment state, derived for read surfaces (issue #118).

    Not a stored row — a read-time join over the contribution, its judgment,
    and its judgment attempts, so every host renders the same three states
    instead of each re-deriving them:

    - ``judged`` — a verdict exists; ``decision`` carries it.
    - ``judge_failed`` — no verdict, and an attempt carries the mechanical
      :data:`JUDGE_FAILED` marker: the judge was attempted and errored, and
      nothing further will happen without an explicit re-judge.
      ``error_class`` / ``error_message`` / ``failed_at`` describe the last
      such failure.
    - ``pending`` — no verdict and no marker: judgment is in flight, has never
      been attempted, or the contribution is a pre-#118 orphan whose attempts
      predate the marker.  ``failed_attempts`` still counts its attempts
      (issue #57), so an orphan reads as "pending, N failed attempts" rather
      than being retroactively declared terminal.

    ``failed_attempts`` counts every attempt event on the contribution,
    marked or not — including attempts that a later successful re-judge
    superseded, which is why it is meaningful even in the ``judged`` state.
    """

    contribution_id: str
    state: Literal["judged", "judge_failed", "pending"]
    decision: Literal["accept_as_directive", "accept_as_context", "decline"] | None
    failed_attempts: int
    error_class: str | None
    error_message: str | None
    failed_at: str | None


@dataclass(frozen=True)
class RecentContribution:
    """One row of a scope's recency window (ADR 0011 D2).

    The (contribution, state, judgment-notes) triple the scope-manager's
    RECENT CONTRIBUTIONS digest is built from — assembled by
    :meth:`RecordStore.list_recent_contributions`, which is the only read that
    carries the judgment's notes and the contribution's state together.

    ``state`` and ``decision`` mean exactly what they mean on
    :class:`ContributionState`; ``judgment_notes`` is the verdict explanation
    the judge wrote when this row was judged — written once and stored
    immutably, so nothing in the digest can drift. Both ``decision`` and
    ``judgment_notes`` are ``None`` for a ``pending`` or ``judge_failed`` row,
    which the window routinely contains — including the contribution currently
    under judgment, appended to the record before the window is read.
    """

    contribution: Contribution
    state: Literal["judged", "judge_failed", "pending"]
    decision: Literal["accept_as_directive", "accept_as_context", "decline"] | None
    judgment_notes: str | None


@dataclass(frozen=True)
class RecordEntry:
    """Everything the record holds on ONE contribution (issue #130).

    The by-id read of a scope's record: the contribution, its derived
    :class:`ContributionState`, its verdict if it has one, and every
    judgment-attempt event on it.  Assembled by
    :meth:`RecordStore.get_record_entry`.

    Exists so "did the contribution I submitted get judged, and what did the
    scope-manager say?" costs one lookup instead of the whole record.
    ``judgment`` is ``None`` in the ``pending`` and ``judge_failed`` states —
    a verdict is the only thing that carries the judge's notes, and neither
    state has one.
    """

    contribution: Contribution
    state: ContributionState
    judgment: Judgment | None
    judgment_attempts: list[JudgmentAttempt]


@dataclass(frozen=True)
class RecordPage:
    """One page of a scope's record, newest first (issue #130).

    The record is append-only and only ever grows, so a whole-record read is
    unbounded by construction.  A page carries the same four parallel lists a
    whole-record read does — with ``judgments``, ``judgment_attempts``, and
    ``contribution_states`` restricted to the page's own contributions, so a
    page is internally complete and nothing on it refers to a row the caller
    cannot see.

    Paging is **cursor-based**, not offset-based, and that is load-bearing
    rather than stylistic.  Pages descend from the newest contribution, and
    the record grows at that end: under an offset, a contribution appended
    mid-walk shifts every later page down one and the page boundary silently
    repeats a row.  A cursor anchors on the last (oldest) row of the page just
    returned and asks for rows strictly older than it, so a concurrent append
    lands above the walk and cannot disturb it.  The anchor is the composite
    ``(created_at, rowid)`` of that row, addressed by its contribution id —
    ``rowid`` breaks same-second ties, which a bare timestamp cursor would
    straddle.

    ``next_before_id`` is the cursor for the following page: the id of this
    page's last contribution, or ``None`` once the record is exhausted.
    ``total`` counts the scope's whole record, not the page.

    ``contributions`` (and ``contribution_states``, which parallels it) are
    newest-first; ``judgments`` and ``judgment_attempts`` are supporting rows
    joined by contribution id and stay in their own chronological order, which
    is how a failure history reads.
    """

    contributions: list[Contribution]
    judgments: list[Judgment]
    judgment_attempts: list[JudgmentAttempt]
    contribution_states: list[ContributionState]
    limit: int
    total: int
    next_before_id: str | None


@dataclass(frozen=True)
class OperatorAct:
    """One act on the operator stratum itself (ADR 0008 D1).

    The operator's OWN record: publishing, superseding, or retiring a piece
    of operator memory attached above ``target_scope_id``. Not judged — the
    operator's stratum authority is not delegated, so there is no judgment
    row here, only the act. ``kind``/``content`` are ``None`` only for
    ``act == "retire"`` (retiring removes an item; no new memory enters).
    ``supersedes``/``retires`` reference a prior :class:`OperatorAct` id
    (``op_``-prefixed) — never a contribution id: operator-stratum acts
    never enter a scope's record (ADR 0008 D4).
    """

    id: str
    act: Literal["publish", "supersede", "retire"]
    target_scope_id: str
    kind: Literal["directive", "context"] | None
    content: str | None
    subject: str | None
    supersedes: str | None
    retires: str | None
    created_at: str


@dataclass(frozen=True)
class Retirement:
    """A retirement event in a *scope's own* record (ADR 0008 D4 / CONTEXT.md § Retirement).

    Recorded when the operator retires a native directive inside a scope's
    summary WITHOUT a replacement — no new memory enters, so no contribution
    row is fabricated. ``retired_by`` carries operator provenance
    (``"operator"``); the shape is reusable by a future scope-manager
    explicit-retire.
    """

    id: str
    scope_id: str
    directive_id: str
    retired_by: str
    reason: str | None
    created_at: str


@dataclass(frozen=True)
class PublicationAct:
    """One act on a scope's publication — its curated outward face (ADR 0007 D1/D2).

    Mirrors :class:`Contribution` but acts on the OUTWARD face, never the
    internal summary. ``act == "publish"`` introduces a new published item
    (``kind``/``content``/``subject``/``anchors`` all populated, ``withdraws``
    ``None``); ``act == "withdraw"`` removes one (those four fields ``None``,
    ``withdraws`` naming the publish act's id being removed). ``anchors`` is
    the list of anchor strings (``directive:<id>`` or ``subject:<text>``,
    ADR 0007 D1) — ``None`` for withdraw. ``trigger`` is ``None`` for an
    agent-proposed or operator-in-person act; for a MECHANICALLY propagated
    withdrawal (ADR 0007 D3) it carries the record id of the internal change
    that caused it (a contribution id or an operator retirement id).
    ``proposer`` mirrors :class:`ContributorRef` — this act's provenance.
    """

    id: str
    scope_id: str
    act: Literal["publish", "withdraw"]
    kind: Literal["directive", "context"] | None
    content: str | None
    subject: str | None
    anchors: list[str] | None
    withdraws: str | None
    trigger: str | None
    proposer: ContributorRef
    created_at: str


@dataclass(frozen=True)
class PublicationJudgment:
    """The scope-manager's verdict on a publish/withdraw act (ADR 0007 D2).

    Exactly one judgment per JUDGED act; a second attempt raises an
    ``IntegrityError`` (UNIQUE constraint on ``act_id``). Mechanical
    propagation withdrawals (:func:`~strata.publication.propagate_directive_removals`)
    get no row here — see the migration's header comment.
    """

    id: str
    act_id: str
    decision: Literal["accept", "decline"]
    judged_by: str
    reasoning: str | None
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

    def get_contribution(self, contribution_id: str) -> Contribution | None:
        """Return the contribution with *contribution_id*, or ``None`` if absent.

        Unlike :meth:`_fetch_contribution` (which raises on a missing id
        because it only ever looks up an id it just inserted), this is the
        public lookup used by re-judge (issue #57), where a client-supplied id
        may legitimately not exist and a clean ``None`` beats a ``KeyError``.
        """
        try:
            return self._fetch_contribution(contribution_id)
        except KeyError:
            return None

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

    def get_judgment(self, contribution_id: str) -> Judgment | None:
        """Return the judgment for *contribution_id*, or ``None`` if unjudged.

        There is at most one judgment per contribution (UNIQUE constraint), so
        this is the idempotency check re-judge keys off (issue #57): a
        contribution that already carries a verdict is never re-judged.
        """
        row = self._conn.execute(
            """
            SELECT id, contribution_id, decision, judged_by, notes, created_at
            FROM judgments WHERE contribution_id = ?
            """,
            (contribution_id,),
        ).fetchone()
        if row is None:
            return None
        return Judgment(**dict(row))

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

    # ------------------------------------------------------------------
    # Judgment attempts (failed-judgment events — issue #57)
    # ------------------------------------------------------------------

    def record_judgment_attempt(
        self,
        *,
        contribution_id: str,
        error_class: str,
        message: str | None = None,
        outcome: Literal["judge_failed"] | None = None,
    ) -> JudgmentAttempt:
        """Record a failed scope-manager judgment as an event on the contribution.

        This is an event, never a verdict (issue #57): the ``judgment_attempts``
        table has no decision column, so a failure can never be mistaken for a
        ``decline``.  Append-only — a contribution may accumulate several
        attempts across re-judge retries.

        Args:
            contribution_id: The contribution whose judgment failed.
            error_class:     The failing exception's class name (e.g.
                             ``'AuthenticationError'``, ``'ValueError'``).
            message:         Optional free-text detail (the exception message).
            outcome:         :data:`JUDGE_FAILED` to mark this attempt as the
                             end of the judge run (issue #118) — mechanical, no
                             judge involved.  ``None`` (the default) records the
                             attempt without claiming the run ended.

        Returns:
            The newly recorded :class:`JudgmentAttempt`.

        Raises:
            sqlite3.IntegrityError: If *contribution_id* does not exist (FK), or
                *outcome* is neither ``None`` nor :data:`JUDGE_FAILED` (CHECK).
        """
        attempt_id = _new_judgment_attempt_id()
        self._conn.execute(
            """
            INSERT INTO judgment_attempts (id, contribution_id, error_class, message, outcome)
            VALUES (?, ?, ?, ?, ?)
            """,
            (attempt_id, contribution_id, error_class, message, outcome),
        )
        self._conn.commit()
        row = self._conn.execute(
            """
            SELECT id, contribution_id, error_class, message, attempted_at, outcome
            FROM judgment_attempts WHERE id = ?
            """,
            (attempt_id,),
        ).fetchone()
        return JudgmentAttempt(**dict(row))

    def list_judgment_attempts(self, *, scope_id: str) -> list[JudgmentAttempt]:
        """Return all judgment-attempt events for contributions in *scope_id*.

        Joins ``judgment_attempts`` against ``contributions`` to filter by
        scope, ordered by ``attempted_at`` ascending — the forensic view of
        which contributions failed judgment and how many times (issue #57).
        """
        rows = self._conn.execute(
            """
            SELECT a.id, a.contribution_id, a.error_class, a.message,
                   a.attempted_at, a.outcome
            FROM judgment_attempts a
            JOIN contributions c ON a.contribution_id = c.id
            WHERE c.scope_id = ?
            ORDER BY a.attempted_at ASC
            """,
            (scope_id,),
        ).fetchall()
        return [JudgmentAttempt(**dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # Contribution states (the derived read surface — issue #118)
    # ------------------------------------------------------------------

    def list_contribution_states(self, *, scope_id: str) -> list[ContributionState]:
        """Return each contribution's judgment state for *scope_id*, oldest first.

        The library answer to issue #118: hosts rendering a record (the CLI's
        ``strata record``, the Console's record and activity surfaces) need to
        tell "the judge errored on this" apart from "no verdict yet", and
        deriving that from three parallel lists is a join every host would
        otherwise reinvent — and get subtly different.

        Reads only; the record is untouched.  See :class:`ContributionState` for
        what each state means.
        """
        contributions = self.list_contributions(scope_id=scope_id)
        judgments = {j.contribution_id: j for j in self.list_judgments(scope_id=scope_id)}
        attempts_by_contribution: dict[str, list[JudgmentAttempt]] = {}
        for attempt in self.list_judgment_attempts(scope_id=scope_id):
            attempts_by_contribution.setdefault(attempt.contribution_id, []).append(attempt)

        return [
            _derive_contribution_state(
                contribution.id,
                judgments.get(contribution.id),
                attempts_by_contribution.get(contribution.id, []),
            )
            for contribution in contributions
        ]

    def page_record(
        self,
        *,
        scope_id: str,
        limit: int = RECORD_PAGE_SIZE,
        before_id: str | None = None,
    ) -> RecordPage:
        """Return one page of *scope_id*'s record, newest first (issue #130).

        The bounded counterpart to the four whole-record reads
        (:meth:`list_contributions`, :meth:`list_judgments`,
        :meth:`list_judgment_attempts`, :meth:`list_contribution_states`),
        which a host otherwise has to call in full and zip itself.  The record
        only ever grows, so on a long-lived scope those reads return the entire
        log every time — the cost this page exists to bound.  The whole record
        stays reachable by paging until ``next_before_id`` is ``None``; nothing
        is hidden, only chunked.

        Retrieval and presentation are both newest-first: the reader of a
        forensic record almost always wants what just happened, and the first
        page with no cursor is exactly that.  (This is where the page differs
        from :meth:`list_recent_contributions`, which retrieves newest-first
        but flips to oldest-first because a *digest* renders that way.)

        Ordering is ``created_at DESC, rowid DESC``, and paging walks it by
        cursor rather than by offset — see :class:`RecordPage` for why an
        offset is unsafe at the growing end of an append-only log.  The cursor
        resolves *before_id* to that row's ``(created_at, rowid)`` and returns
        rows strictly older than it, so same-second ties are decided by
        ``rowid`` instead of being straddled.

        Args:
            scope_id:  The scope whose record to page.
            limit:     Page size.  Defaults to :data:`RECORD_PAGE_SIZE`;
                       deployments pass ``settings.record_page_size``.
            before_id: Cursor — return contributions strictly older than this
                       one.  ``None`` (the default) starts at the newest.
                       Pass the previous page's ``next_before_id``.

        Returns:
            One :class:`RecordPage`.  A cursor on the record's oldest row is
            not an error: it yields an empty page with ``next_before_id``
            ``None``.

        Raises:
            ValueError: If *limit* is below 1, or *before_id* is not a
                contribution in this scope's record — a stale or foreign
                cursor is told so rather than silently paging from the top.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit!r}")

        total = self._conn.execute(
            "SELECT COUNT(*) FROM contributions WHERE scope_id = ?",
            (scope_id,),
        ).fetchone()[0]

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
        if before_id is not None:
            anchor = self._conn.execute(
                "SELECT created_at, rowid FROM contributions WHERE id = ? AND scope_id = ?",
                (before_id, scope_id),
            ).fetchone()
            if anchor is None:
                raise ValueError(f"before_id is not a contribution in {scope_id!r}: {before_id!r}")
            base += " AND (created_at < ? OR (created_at = ? AND rowid < ?))"
            params.extend([anchor["created_at"], anchor["created_at"], anchor["rowid"]])
        # One row beyond the page: its presence is what distinguishes "more to
        # come" from "exhausted", without a second count against the cursor.
        base += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit + 1)

        rows = self._conn.execute(base, params).fetchall()
        has_more = len(rows) > limit
        contributions = [_contribution_from_row(row) for row in rows[:limit]]

        contribution_ids = [c.id for c in contributions]
        judgments = self._judgments_for(contribution_ids)
        judgment_attempts = self._judgment_attempts_for(contribution_ids)
        judgment_by_contribution = {j.contribution_id: j for j in judgments}
        attempts_by_contribution: dict[str, list[JudgmentAttempt]] = {}
        for attempt in judgment_attempts:
            attempts_by_contribution.setdefault(attempt.contribution_id, []).append(attempt)

        return RecordPage(
            contributions=contributions,
            judgments=judgments,
            judgment_attempts=judgment_attempts,
            contribution_states=[
                _derive_contribution_state(
                    c.id,
                    judgment_by_contribution.get(c.id),
                    attempts_by_contribution.get(c.id, []),
                )
                for c in contributions
            ],
            limit=limit,
            total=total,
            next_before_id=contributions[-1].id if has_more else None,
        )

    def get_record_entry(self, contribution_id: str) -> RecordEntry | None:
        """Return the record's whole entry for *contribution_id*, or ``None`` if absent.

        The by-id read of the record (issue #130): one contribution with its
        state, its verdict, and its judgment-attempt events.  Answers "what
        happened to the contribution I submitted?" without paging the scope's
        record looking for it — the read a contributor makes after a submission
        whose outcome it never saw.

        ``None`` for an unknown id, matching :meth:`get_contribution` and
        :meth:`get_judgment`: a caller-supplied id may legitimately not exist.
        """
        contribution = self.get_contribution(contribution_id)
        if contribution is None:
            return None
        judgment = self.get_judgment(contribution_id)
        judgment_attempts = self._judgment_attempts_for([contribution_id])
        return RecordEntry(
            contribution=contribution,
            state=_derive_contribution_state(contribution_id, judgment, judgment_attempts),
            judgment=judgment,
            judgment_attempts=judgment_attempts,
        )

    def _judgments_for(self, contribution_ids: list[str]) -> list[Judgment]:
        """Return the judgments on *contribution_ids*, oldest verdict first."""
        if not contribution_ids:
            return []
        placeholders = ", ".join("?" for _ in contribution_ids)
        rows = self._conn.execute(
            f"""
            SELECT id, contribution_id, decision, judged_by, notes, created_at
            FROM judgments
            WHERE contribution_id IN ({placeholders})
            ORDER BY created_at ASC, rowid ASC
            """,
            contribution_ids,
        ).fetchall()
        return [Judgment(**dict(row)) for row in rows]

    def _judgment_attempts_for(self, contribution_ids: list[str]) -> list[JudgmentAttempt]:
        """Return the judgment-attempt events on *contribution_ids*, oldest first."""
        if not contribution_ids:
            return []
        placeholders = ", ".join("?" for _ in contribution_ids)
        rows = self._conn.execute(
            f"""
            SELECT id, contribution_id, error_class, message, attempted_at, outcome
            FROM judgment_attempts
            WHERE contribution_id IN ({placeholders})
            ORDER BY attempted_at ASC, rowid ASC
            """,
            contribution_ids,
        ).fetchall()
        return [JudgmentAttempt(**dict(row)) for row in rows]

    def list_recent_contributions(
        self,
        *,
        scope_id: str,
        limit: int,
    ) -> list[RecentContribution]:
        """Return the newest *limit* contributions of *scope_id* with their state.

        The windowed read ADR 0011 D2's recency digest needs: no existing read
        carries a contribution's judgment notes and its state together.
        :meth:`list_contributions` has the window but no verdict;
        :meth:`list_contribution_states` has the state but neither the notes nor
        a window — it joins the scope's WHOLE record, which is the cost this
        window exists to avoid.

        Retrieval is newest-first (the same ``ORDER BY ... DESC LIMIT``
        recency window :meth:`list_contributions` uses, for the same reason:
        ascending order plus a limit would pin the manager to a permanently
        stale slice), and the result is returned **oldest-first** — the order
        the digest renders in.

        Args:
            scope_id: The scope whose record to window.
            limit:    How many of the newest contributions to return.

        Returns:
            Oldest-first list of :class:`RecentContribution` rows.
        """
        rows = self._conn.execute(
            """
            SELECT c.id, c.scope_id, c.content, c.proposed_classification,
                   c.subject, c.supersedes,
                   c.contributor_scope_id, c.contributor_skill,
                   c.contributor_session_id, c.contributor_ts,
                   c.created_at,
                   j.decision AS decision, j.notes AS judgment_notes,
                   (SELECT COUNT(*) FROM judgment_attempts a
                     WHERE a.contribution_id = c.id AND a.outcome = ?) AS failed_marks
            FROM contributions c
            LEFT JOIN judgments j ON j.contribution_id = c.id
            WHERE c.scope_id = ?
            ORDER BY c.created_at DESC, c.rowid DESC
            LIMIT ?
            """,
            (JUDGE_FAILED, scope_id, limit),
        ).fetchall()
        return [_recent_contribution_from_row(row) for row in reversed(rows)]

    # ------------------------------------------------------------------
    # Operator acts (the operator's own record — ADR 0008 D1)
    # ------------------------------------------------------------------

    def append_operator_act(
        self,
        *,
        act: Literal["publish", "supersede", "retire"],
        target_scope_id: str,
        kind: Literal["directive", "context"] | None,
        content: str | None,
        subject: str | None = None,
        supersedes: str | None = None,
        retires: str | None = None,
    ) -> OperatorAct:
        """Append one operator-stratum act to the operator's own record.

        This is the append-only log of everything the operator does ON the
        operator stratum (ADR 0008 D1) — never judged, never mixed into any
        scope's own record. ``kind``/``content`` must both be ``None`` for
        ``act == "retire"`` (no new memory enters on a retire) and both
        non-``None`` otherwise.

        Args:
            act:             ``'publish'``, ``'supersede'``, or ``'retire'``.
            target_scope_id: The attachment scope — the operator layer's
                             reach point, per ADR 0008 D2.
            kind:            ``'directive'`` or ``'context'``; ``None`` only
                             for ``act == 'retire'``.
            content:         Verbatim operator memory text; ``None`` only for
                             ``act == 'retire'``.
            subject:         Optional short subject line.
            supersedes:      For ``act == 'supersede'``: the ``op_`` id of the
                             operator item being replaced.
            retires:         For ``act == 'retire'``: the ``op_`` id of the
                             operator item being removed.

        Returns:
            The newly appended :class:`OperatorAct`.

        Raises:
            sqlite3.IntegrityError: If *supersedes* or *retires* references a
                non-existent operator act.
        """
        act_id = _new_operator_act_id()
        self._conn.execute(
            """
            INSERT INTO operator_acts (
                id, act, target_scope_id, kind, content, subject, supersedes, retires
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (act_id, act, target_scope_id, kind, content, subject, supersedes, retires),
        )
        self._conn.commit()
        return self._fetch_operator_act(act_id)

    def list_operator_acts(self, *, target_scope_id: str | None = None) -> list[OperatorAct]:
        """Return operator acts ordered by ``created_at`` ascending.

        Args:
            target_scope_id: When given, filter to acts attached at this
                scope only. ``None`` returns the operator's entire record
                (ADR 0008 D5 — the operator reads everything).
        """
        if target_scope_id is not None:
            rows = self._conn.execute(
                """
                SELECT id, act, target_scope_id, kind, content, subject,
                       supersedes, retires, created_at
                FROM operator_acts
                WHERE target_scope_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (target_scope_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT id, act, target_scope_id, kind, content, subject,
                       supersedes, retires, created_at
                FROM operator_acts
                ORDER BY created_at ASC, rowid ASC
                """
            ).fetchall()
        return [OperatorAct(**dict(row)) for row in rows]

    def _fetch_operator_act(self, act_id: str) -> OperatorAct:
        row = self._conn.execute(
            """
            SELECT id, act, target_scope_id, kind, content, subject,
                   supersedes, retires, created_at
            FROM operator_acts WHERE id = ?
            """,
            (act_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Operator act not found: {act_id!r}")
        return OperatorAct(**dict(row))

    # ------------------------------------------------------------------
    # Retirements (retirement events in a SCOPE's own record — ADR 0008 D4)
    # ------------------------------------------------------------------

    def append_retirement(
        self,
        *,
        scope_id: str,
        directive_id: str,
        retired_by: str,
        reason: str | None = None,
    ) -> Retirement:
        """Append a retirement event to *scope_id*'s own record.

        Used when a directive is removed from a scope summary WITHOUT a
        replacement (no new memory enters) — no contribution row is
        fabricated (CONTEXT.md § Retirement; ADR 0008 D4).

        Args:
            scope_id:     The scope whose summary the directive is retired
                          from.
            directive_id: The contribution id of the directive being retired.
            retired_by:   Provenance of the retiring authority (``"operator"``
                          for an ADR 0008 correction).
            reason:       Optional free-text rationale.

        Returns:
            The newly appended :class:`Retirement`.
        """
        retirement_id = _new_retirement_id()
        self._conn.execute(
            """
            INSERT INTO retirements (id, scope_id, directive_id, retired_by, reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            (retirement_id, scope_id, directive_id, retired_by, reason),
        )
        self._conn.commit()
        row = self._conn.execute(
            """
            SELECT id, scope_id, directive_id, retired_by, reason, created_at
            FROM retirements WHERE id = ?
            """,
            (retirement_id,),
        ).fetchone()
        return Retirement(**dict(row))

    def list_retirements(self, *, scope_id: str | None = None) -> list[Retirement]:
        """Return retirement events ordered by ``created_at`` ascending.

        Args:
            scope_id: When given, filter to retirements from this scope's
                record only. ``None`` returns every retirement event
                (ADR 0008 D5 — the operator reads everything).
        """
        if scope_id is not None:
            rows = self._conn.execute(
                """
                SELECT id, scope_id, directive_id, retired_by, reason, created_at
                FROM retirements
                WHERE scope_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (scope_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT id, scope_id, directive_id, retired_by, reason, created_at
                FROM retirements
                ORDER BY created_at ASC, rowid ASC
                """
            ).fetchall()
        return [Retirement(**dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # Publication acts + judgments (ADR 0007 D1/D2) — the publication
    # channel's own record, distinct from a scope's contribution record.
    # ------------------------------------------------------------------

    def append_publication_act(
        self,
        *,
        scope_id: str,
        act: Literal["publish", "withdraw"],
        kind: Literal["directive", "context"] | None,
        content: str | None,
        subject: str | None,
        anchors: list[str] | None,
        withdraws: str | None,
        trigger: str | None,
        proposer: ContributorRef,
    ) -> PublicationAct:
        """Append a publish or withdraw act to *scope_id*'s publication record.

        This is the raw record append — the caller judges (or, for
        mechanical propagation, mechanically decides) separately and records
        the verdict via :meth:`record_publication_judgment` (agent-proposed
        and judged-propagation acts only; mechanical propagation appends no
        judgment row — see the migration's header comment).

        Args:
            scope_id:  The publishing scope.
            act:       ``'publish'`` or ``'withdraw'``.
            kind:      ``'directive'`` or ``'context'``; ``None`` for withdraw.
            content:   Verbatim outward wording; ``None`` for withdraw.
            subject:   Optional short label; ``None`` for withdraw.
            anchors:   The anchor strings supporting this item (ADR 0007 D1);
                       ``None`` for withdraw. Stored as a JSON array.
            withdraws: For ``act == 'withdraw'``: the ``pub_`` id of the
                       published item being removed. ``None`` for publish.
            trigger:   For a mechanically propagated withdrawal (ADR 0007
                       D3): the record id of the triggering internal change.
                       ``None`` otherwise.
            proposer:  Provenance — mirrors :class:`ContributorRef`.

        Returns:
            The newly appended :class:`PublicationAct`.

        Raises:
            sqlite3.IntegrityError: If *withdraws* references a non-existent
                publication act.
        """
        act_id = _new_publication_act_id()
        anchors_json = json.dumps(anchors) if anchors is not None else None
        self._conn.execute(
            """
            INSERT INTO publication_acts (
                id, scope_id, act, kind, content, subject, anchors, withdraws, "trigger",
                proposer_scope_id, proposer_skill, proposer_session_id, proposer_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                act_id,
                scope_id,
                act,
                kind,
                content,
                subject,
                anchors_json,
                withdraws,
                trigger,
                proposer.scope_id,
                proposer.skill,
                proposer.session_id,
                proposer.ts,
            ),
        )
        self._conn.commit()
        return self._fetch_publication_act(act_id)

    def list_publication_acts(self, *, scope_id: str) -> list[PublicationAct]:
        """Return publication acts for *scope_id* ordered by ``created_at`` ascending."""
        rows = self._conn.execute(
            """
            SELECT id, scope_id, act, kind, content, subject, anchors, withdraws,
                   "trigger", proposer_scope_id, proposer_skill, proposer_session_id,
                   proposer_ts, created_at
            FROM publication_acts
            WHERE scope_id = ?
            ORDER BY created_at ASC, rowid ASC
            """,
            (scope_id,),
        ).fetchall()
        return [_publication_act_from_row(row) for row in rows]

    def get_publication_act(self, act_id: str) -> PublicationAct | None:
        """Return the publication act with *act_id*, or ``None`` if absent."""
        try:
            return self._fetch_publication_act(act_id)
        except KeyError:
            return None

    def _fetch_publication_act(self, act_id: str) -> PublicationAct:
        row = self._conn.execute(
            """
            SELECT id, scope_id, act, kind, content, subject, anchors, withdraws,
                   "trigger", proposer_scope_id, proposer_skill, proposer_session_id,
                   proposer_ts, created_at
            FROM publication_acts WHERE id = ?
            """,
            (act_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Publication act not found: {act_id!r}")
        return _publication_act_from_row(row)

    def record_publication_judgment(
        self,
        *,
        act_id: str,
        decision: Literal["accept", "decline"],
        judged_by: str,
        reasoning: str | None = None,
    ) -> PublicationJudgment:
        """Record the scope-manager's verdict on a publish/withdraw act.

        Only one judgment is allowed per act; a second call raises an
        ``IntegrityError`` (UNIQUE constraint on ``act_id``). Never called
        for a mechanically propagated withdrawal (ADR 0007 D3) — that act
        carries a ``trigger`` instead and gets no judgment row.

        Args:
            act_id:    The publication act being judged.
            decision:  ``'accept'`` or ``'decline'``.
            judged_by: Identifier of the judging authority (``"scope-manager"``
                       for the normal judged path).
            reasoning: Optional free-text rationale.

        Returns:
            The newly recorded :class:`PublicationJudgment`.

        Raises:
            sqlite3.IntegrityError: If *act_id* does not exist (FK) or
                already has a judgment (UNIQUE).
        """
        judgment_id = _new_publication_judgment_id()
        self._conn.execute(
            """
            INSERT INTO publication_judgments (id, act_id, decision, judged_by, reasoning)
            VALUES (?, ?, ?, ?, ?)
            """,
            (judgment_id, act_id, decision, judged_by, reasoning),
        )
        self._conn.commit()
        return self._fetch_publication_judgment(judgment_id)

    def get_publication_judgment(self, act_id: str) -> PublicationJudgment | None:
        """Return the judgment for *act_id*, or ``None`` if unjudged (or mechanical)."""
        row = self._conn.execute(
            """
            SELECT id, act_id, decision, judged_by, reasoning, created_at
            FROM publication_judgments WHERE act_id = ?
            """,
            (act_id,),
        ).fetchone()
        if row is None:
            return None
        return PublicationJudgment(**dict(row))

    def list_publication_judgments(self, *, scope_id: str) -> list[PublicationJudgment]:
        """Return all publication judgments for acts belonging to *scope_id*."""
        rows = self._conn.execute(
            """
            SELECT j.id, j.act_id, j.decision, j.judged_by, j.reasoning, j.created_at
            FROM publication_judgments j
            JOIN publication_acts a ON j.act_id = a.id
            WHERE a.scope_id = ?
            ORDER BY j.created_at ASC
            """,
            (scope_id,),
        ).fetchall()
        return [PublicationJudgment(**dict(row)) for row in rows]

    def _fetch_publication_judgment(self, judgment_id: str) -> PublicationJudgment:
        row = self._conn.execute(
            """
            SELECT id, act_id, decision, judged_by, reasoning, created_at
            FROM publication_judgments WHERE id = ?
            """,
            (judgment_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Publication judgment not found: {judgment_id!r}")
        return PublicationJudgment(**dict(row))


# ---------------------------------------------------------------------------
# Row → model helpers
# ---------------------------------------------------------------------------


def _derive_contribution_state(
    contribution_id: str,
    judgment: Judgment | None,
    attempts: list[JudgmentAttempt],
) -> ContributionState:
    """Derive one contribution's :class:`ContributionState` from its verdict and attempts.

    The single derivation behind every state-carrying read
    (:meth:`RecordStore.list_contribution_states`,
    :meth:`RecordStore.page_record`, :meth:`RecordStore.get_record_entry`), so
    a whole-record read, a page, and a by-id lookup can never disagree about
    what a contribution's state is.
    """
    # A verdict always wins: a contribution that failed twice and was then
    # re-judged successfully is judged, not judge_failed.
    if judgment is not None:
        state: Literal["judged", "judge_failed", "pending"] = "judged"
    elif any(a.outcome == JUDGE_FAILED for a in attempts):
        state = "judge_failed"
    else:
        state = "pending"
    last_failure = next(
        (a for a in reversed(attempts) if a.outcome == JUDGE_FAILED),
        None,
    )
    return ContributionState(
        contribution_id=contribution_id,
        state=state,
        decision=judgment.decision if judgment is not None else None,
        failed_attempts=len(attempts),
        error_class=last_failure.error_class if last_failure is not None else None,
        error_message=last_failure.message if last_failure is not None else None,
        failed_at=last_failure.attempted_at if last_failure is not None else None,
    )


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


def _recent_contribution_from_row(row: sqlite3.Row) -> RecentContribution:
    """Map a row of the recency-window join to a :class:`RecentContribution`.

    The state derivation mirrors :meth:`RecordStore.list_contribution_states`
    exactly — a verdict always wins, a ``judge_failed`` marker comes next, and
    everything else is ``pending`` — so the two surfaces can never disagree
    about what a contribution's state is.
    """
    d = dict(row)
    decision = d.pop("decision")
    judgment_notes = d.pop("judgment_notes")
    failed_marks = d.pop("failed_marks")
    contributor = ContributorRef(
        scope_id=d.pop("contributor_scope_id"),
        skill=d.pop("contributor_skill"),
        session_id=d.pop("contributor_session_id"),
        ts=d.pop("contributor_ts"),
    )
    if decision is not None:
        state: Literal["judged", "judge_failed", "pending"] = "judged"
    elif failed_marks:
        state = "judge_failed"
    else:
        state = "pending"
    return RecentContribution(
        contribution=Contribution(**d, contributor=contributor),
        state=state,
        decision=decision,
        judgment_notes=judgment_notes,
    )


def _publication_act_from_row(row: sqlite3.Row) -> PublicationAct:
    """Map a ``sqlite3.Row`` from the publication_acts table to a :class:`PublicationAct`."""
    d = dict(row)
    proposer = ContributorRef(
        scope_id=d.pop("proposer_scope_id"),
        skill=d.pop("proposer_skill"),
        session_id=d.pop("proposer_session_id"),
        ts=d.pop("proposer_ts"),
    )
    anchors_json = d.pop("anchors")
    anchors = json.loads(anchors_json) if anchors_json is not None else None
    trigger = d.pop("trigger")
    return PublicationAct(**d, anchors=anchors, trigger=trigger, proposer=proposer)
