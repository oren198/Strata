"""FastAPI HTTP layer for the Strata backend.

Wires the record store, summary store, and scope-manager together behind a
small REST API.  All endpoints return JSON.  Sync endpoints are used
throughout — FastAPI mixes sync and async without issue.

Fleet configuration is served from an in-memory :class:`FleetConfig` mirror,
lazily reloaded from ``fleet.yaml`` whenever the file's mtime/size changes
(ADR 0002, addendum: lazy reload-on-read) via a
:class:`~strata.fleet_reload.FleetReloader` held on ``app.state.fleet_reloader``.
The ``strata``, ``scopes``, and ``edges`` SQLite tables are gone;
scope-existence and active-status checks are enforced against the in-memory
mirror at contribute time.

Endpoints
---------
GET /
    Redirect to the Strata Console UI at /ui/index.html.

GET /ui/...
    Static file server for the Strata Console UI (strata/_ui/ package data).

POST /contribute
    Accept a contribution from an agent, invoke the scope-manager, persist
    the judgment, and (if accepted) update the scope summary.

    Contribute-time validation (ADR 0002 invariants 9 and 10):
    - Scope not in FleetConfig → 404 ``scope_not_found``.
    - Scope ``status == "archived"`` → 409 ``scope_not_active``.

GET /scopes
    Return active scopes and strata from FleetConfig.

GET /scopes/{scope_id}/summary
    Return the scope summary.  200 with a synthesized empty summary
    (``version=0``, ``exists=False``) if the scope exists but has no summary
    yet, distinguishable from a real first write (``version=1``,
    ``exists=True``); 404 if the scope is unknown.

GET /scopes/{scope_id}/record
    Return one page of the contribution record + judgments for a scope
    (forensic view), newest first.  Bounded by default (issue #130): walk
    older pages with ``before_id``, sized by ``limit``.

GET /scopes/{scope_id}/record/{contribution_id}
    Return one contribution with its state, verdict, and judgment attempts
    (issue #130) — the cheap "what happened to this contribution?" read.

Vocabulary follows CONTEXT.md verbatim.
"""

from __future__ import annotations

import importlib.resources
import json
import pathlib
import sqlite3
from collections.abc import AsyncGenerator, Generator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import anthropic
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from strata import __version__
from strata.fleet_config import FleetConfig, Scope, Stratum
from strata.fleet_reload import FleetReloader
from strata.locks import BATCH_CAP, QUEUE_WAIT_TIMEOUT_S, QueueTicket, configure_lock_dir
from strata.locks import scope_append_lock as _scope_append_lock
from strata.locks import scope_lock as _scope_lock
from strata.locks import scope_queue as _scope_queue
from strata.migrator import run_migrations
from strata.operator import operator_memory_binding, read_operator_layer
from strata.perspective import compose_perspective
from strata.project_config import StoragePaths, resolve_storage_paths
from strata.publication import (
    apply_judged_withdrawals,
    propagate_directive_removals,
    read_publication,
)
from strata.record_store import (
    JUDGE_FAILED,
    RECENCY_WINDOW_SIZE,
    Contribution,
    ContributorRef,
    RecentContribution,
    RecordStore,
)
from strata.scope_manager import (
    WINDOW_VERBATIM_TAIL,
    ScopeManager,
    ScopeManagerBatchJudgment,
    ScopeManagerJudgment,
)
from strata.session_state import (
    DEFAULT_STALENESS_WINDOW_DAYS,
    SessionStateStore,
    compute_fleet_staleness,
    sessions_dir_for,
)
from strata.settings import Settings, get_settings
from strata.summary_store import ScopeSummary, SummaryStore

# Console UI static files bundled as package data (same vendoring pattern as
# _skills/ / _migrations/ / _templates/), so the static mount works regardless
# of cwd and in wheel installs (pipx, ADR 0005 / issue #65).
_UI_DIR = pathlib.Path(str(importlib.resources.files("strata"))) / "_ui"

# GET /scopes/{scope_id}/summary's "retirements" key (P5) is bounded like the
# record page (issue #130's rationale, applied here): a long-lived scope's
# retirement history only ever grows, and every summary GET carries it, so an
# unbounded list would bloat a call that fires far more often than a record
# page walk. Newest-first, capped — older retirements stay in the record,
# reachable there, just not repeated on every summary fetch.
_SUMMARY_RETIREMENTS_LIMIT = 50

# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------


def get_storage_paths(
    settings: Settings = Depends(get_settings),
) -> StoragePaths:
    """Resolve storage paths — the single source of truth (issue #44).

    ``.strata/config.toml`` (when discoverable) wins over env-var settings,
    exactly as the MCP server resolves them, so the Console backend and the
    agents can never operate on different state.
    """
    return resolve_storage_paths(settings)


def get_record_store(
    paths: StoragePaths = Depends(get_storage_paths),
) -> Generator[RecordStore, None, None]:
    """Yield a fresh :class:`RecordStore` per request, closing it afterwards."""
    store = RecordStore(paths.db_path)
    try:
        yield store
    finally:
        store.close()


def get_summary_store(
    paths: StoragePaths = Depends(get_storage_paths),
) -> SummaryStore:
    """Return a :class:`SummaryStore` for the configured summaries directory."""
    return SummaryStore(paths.summaries_dir)


def get_session_store(
    paths: StoragePaths = Depends(get_storage_paths),
) -> SessionStateStore:
    """Return the per-session state store (UI-only reads: staleness + closeout counters).

    Session state is runtime measurement, never memory (see session_state.py's module
    docstring). Nothing in the contribute/judge path reads it through this provider.
    """
    return SessionStateStore(sessions_dir_for(paths.summaries_dir))


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, returning ``None`` on failure rather than raising.

    Used only by UI-only reads (the mechanical-declines counter): a malformed
    or missing timestamp on disk should degrade the count, never break the read.
    """
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


#: Characters per token in the Console's rough weight estimate. Deliberately a
#: constant, not a tokenizer: the Console must work offline with no model call and
#: no extra dependency, and the number's job is comparing layers to each other, not
#: predicting a bill. Every surface that shows it says "est.".
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    """Return a rough token estimate for *text* (UI-only, never used to judge)."""
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def get_anthropic_client(
    settings: Settings = Depends(get_settings),
) -> anthropic.Anthropic:
    """Return an :class:`anthropic.Anthropic` client using the configured judge key/base URL."""
    return settings.build_judge_client()


def get_scope_manager(
    client: anthropic.Anthropic = Depends(get_anthropic_client),
    settings: Settings = Depends(get_settings),
) -> ScopeManager:
    """Return a :class:`ScopeManager` bound to the configured model."""
    return ScopeManager(client=client, model=settings.manager_model)


def get_fleet_config(request: Request) -> FleetConfig:
    """Return the current :class:`FleetConfig`, reloading fleet.yaml if it changed.

    Delegates to the request's :class:`~strata.fleet_reload.FleetReloader`
    (``app.state.fleet_reloader``) — the lazy-reload-on-read path shared with
    the MCP server (ADR 0002 addendum). Every call here stats fleet.yaml
    first; an unchanged file returns the cached config with no re-parse.
    """
    return request.app.state.fleet_reloader.get()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ContributorRefBody(BaseModel):
    """Provenance metadata supplied by the contributing agent.

    ``skill`` is optional (issue #121): agent identity is scope + session, and
    a skill either carries a body or is omitted — a bare name adds nothing, so
    a skill-less binding sends no ``skill`` (or ``null``).
    """

    scope_id: str
    skill: str | None = None
    session_id: str
    ts: str


class ContributeRequest(BaseModel):
    """Request body for ``POST /contribute``."""

    scope_id: str
    content: str
    proposed_classification: Literal["directive", "context"]
    subject: str | None = None
    supersedes: str | None = None
    contributor: ContributorRefBody


class JudgmentResult(BaseModel):
    """Embedded judgment info in the ``POST /contribute`` response."""

    decision: Literal["accept_as_directive", "accept_as_context", "decline"]
    reasoning: str
    summary_updated: bool


class ContributeResponse(BaseModel):
    """Response body for ``POST /contribute``."""

    contribution_id: str
    judgment: JudgmentResult


class SupersedeDirectiveRequest(BaseModel):
    """Operator correction in person (ADR 0008 D4) — verbatim replacement text."""

    content: str = Field(min_length=1)
    subject: str | None = None

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content must not be blank")
        return v


class RetireDirectiveRequest(BaseModel):
    """Operator retirement in person (ADR 0008 D4) — no replacement memory enters."""

    reason: str | None = None


# ---------------------------------------------------------------------------
# Contribute choke point (issues #38, #57)
#
# The single place where the read-summary -> judge -> record-judgment ->
# summary-write sequence for a scope runs. Both agent (MCP ``strata_contribute``
# / ``strata_rejudge``) and operator (HTTP ``POST /contribute``) surfaces route
# through ``run_contribution`` / ``rejudge_contribution`` so the serialization
# invariant lives in exactly one place.
# ---------------------------------------------------------------------------

# The per-scope lock registry lives in strata.locks (extracted for ADR 0008 —
# strata.operator's correction primitives (operator_supersede/operator_retire)
# must serialize under this SAME lock, and importing strata.app from
# strata.operator would cycle back here, since this module also needs
# strata.operator.operator_memory_binding for judge inputs). `_scope_lock` is
# imported at module top under this name so every call site below is unchanged.


@dataclass
class ContributionOutcome:
    """The result of running (or re-judging) a contribution through the choke point."""

    contribution_id: str
    decision: Literal["accept_as_directive", "accept_as_context", "decline"]
    reasoning: str
    summary_updated: bool


class JudgeUnavailable(Exception):
    """Raised when the scope-manager's judgment fails during a contribution.

    The contribution is already in the record (issue #57 — the record never
    lies) and a judgment-attempt-failed *event* has been recorded against it,
    but no judgment exists: a verdict is an exercise of scope authority and no
    component outside the authority chain may forge one. Carries
    ``contribution_id`` so the caller routes a retry to re-judge
    (``strata_rejudge`` / :func:`rejudge_contribution`) instead of appending a
    duplicate contribution.

    Still exactly one contribution per error under ADR 0011 D3: a failed BATCH
    call raises one of these per member, each carrying its own contribution id
    and each with its own attempt row, so every waiting caller learns which
    contribution of its own to re-judge — never someone else's.
    """

    def __init__(self, contribution_id: str, error_class: str, message: str) -> None:
        self.contribution_id = contribution_id
        self.error_class = error_class
        super().__init__(message)


@dataclass
class _JudgeInputs:
    """The scope state one judgment call is judged against.

    Read once per call — per BATCH on the coalescing path (ADR 0011 D3), which
    is what makes N contributions cost one prompt instead of N.
    """

    current_summary: ScopeSummary | None
    parent_summary: ScopeSummary | None
    recent_contributions: list[RecentContribution]
    entitlement: object
    operator_memory: list
    current_publication: list
    peer_publications: list


def _read_judge_inputs(
    *,
    scope: Scope,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    recency_window_size: int = RECENCY_WINDOW_SIZE,
) -> _JudgeInputs:
    """Read everything the scope-manager judges against, under the caller's lock."""
    current_summary = summary_store.read(scope.id)
    # ADR 0011 D2: the recency window is a mechanical digest built from the
    # record — (contribution, state, judgment notes) triples, not verbatim
    # text. The contributions under judgment were appended before this read, so
    # they are in their own window as `pending` rows.
    recent_contributions = record_store.list_recent_contributions(
        scope_id=scope.id, limit=recency_window_size
    )

    # Resolve the inter-stratum parent's summary for manager context (ADR 0004
    # Decision 2). The caller does the graph traversal; the manager is a pure
    # judgment primitive that receives the resolved summary.
    parent_scope = fleet.inter_stratum_parent(scope.id)
    parent_summary = summary_store.read(parent_scope.id) if parent_scope is not None else None

    # Judge-aware rendering (ADR 0008 D3): the operator memory binding this
    # scope (attached here or at any inter-stratum ancestor) is rendered to
    # the scope-manager as a binding input, alongside the parent summary.
    operator_memory = operator_memory_binding(
        scope.id, fleet=fleet, summaries_dir=summary_store.summaries_dir
    )

    # ADR 0007 D3/D5: this scope's own current publication, and the
    # publications of every peer scope referenced by this scope's chain —
    # the rendered evidence the judge's withdraw_published verdict and the
    # #79 admission rule's "peer X published this" check are checked against.
    entitlement = fleet.entitlement_view(scope.id)
    current_publication = read_publication(scope.id, summaries_dir=str(summary_store.summaries_dir))
    peer_publications = [
        (peer.id, read_publication(peer.id, summaries_dir=str(summary_store.summaries_dir)))
        for peer in sorted(entitlement.referenced_peers, key=lambda s: s.id)
    ]
    return _JudgeInputs(
        current_summary=current_summary,
        parent_summary=parent_summary,
        recent_contributions=recent_contributions,
        entitlement=entitlement,
        operator_memory=operator_memory,
        current_publication=current_publication,
        peer_publications=peer_publications,
    )


def _judge_and_record(
    *,
    contribution: Contribution,
    scope: Scope,
    stratum: Stratum,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    scope_manager: ScopeManager,
    summary_max_words: int,
    window_verbatim_tail: int = WINDOW_VERBATIM_TAIL,
    recency_window_size: int = RECENCY_WINDOW_SIZE,
) -> ContributionOutcome:
    """Judge *contribution* against the scope's current state and persist the result.

    The caller MUST hold ``_scope_lock(scope.id)`` — this reads the current
    summary, judges, records the judgment, and writes the summary as one
    serialized unit. On judge failure it records a judgment-attempt-failed
    event and raises :class:`JudgeUnavailable`; no judgment row is written.
    """
    inputs = _read_judge_inputs(
        scope=scope,
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        recency_window_size=recency_window_size,
    )
    parent_summary = inputs.parent_summary

    try:
        judgment: ScopeManagerJudgment = scope_manager.judge(
            scope=scope,
            stratum=stratum,
            parent_summary=parent_summary,
            current_summary=inputs.current_summary,
            recent_contributions=inputs.recent_contributions,
            new_contribution=contribution,
            summary_max_words=summary_max_words,
            entitlement=inputs.entitlement,
            operator_memory=inputs.operator_memory,
            current_publication=inputs.current_publication,
            peer_publications=inputs.peer_publications,
            window_verbatim_tail=window_verbatim_tail,
        )
    except Exception as exc:
        # Record the failure as an event against the contribution — never as a
        # fabricated verdict (issue #57) — then surface it with the
        # contribution id so a retry routes to re-judge, not a duplicate.
        #
        # judge() exhausts its own corrective re-asks before it raises (the
        # #113 parse re-ask, the #63 overflow re-ask), so reaching here means
        # the judge run is over: mark the event JUDGE_FAILED (issue #118) so
        # read surfaces render "attempted, judge errored" instead of leaving
        # the contribution indistinguishable from one still in flight. The
        # marker is mechanical — no judge or LLM call is made to write it.
        record_store.record_judgment_attempt(
            contribution_id=contribution.id,
            error_class=type(exc).__name__,
            message=str(exc),
            outcome=JUDGE_FAILED,
        )
        raise JudgeUnavailable(contribution.id, type(exc).__name__, str(exc)) from exc

    record_store.record_judgment(
        contribution_id=contribution.id,
        decision=judgment.decision,
        judged_by="scope-manager",
        # The judge's reasoning, plus the mechanical note for any amendment op
        # the engine dropped (ADR 0011 D1) — the record shows what applied.
        notes=judgment.record_notes,
    )

    summary_updated = False
    if judgment.decision != "decline" and judgment.new_summary is not None:
        # Single path: the one judged contribution owns the whole amendment —
        # the implicit binding ADR 0011 D3 leaves exactly as it was.
        _write_amendment(
            judgment,
            scope=scope,
            record_store=record_store,
            summary_store=summary_store,
            parent_summary=parent_summary,
            retirements=[(d, judgment.reasoning) for d in judgment.retired_directive_ids],
            removals=[(d, contribution.id) for d in judgment.removed_directive_ids],
            withdraw_reasoning=judgment.reasoning,
        )
        summary_updated = True

    return ContributionOutcome(
        contribution_id=contribution.id,
        decision=judgment.decision,
        reasoning=judgment.reasoning,
        summary_updated=summary_updated,
    )


def _write_amendment(
    judgment: ScopeManagerJudgment | ScopeManagerBatchJudgment,
    *,
    scope: Scope,
    record_store: RecordStore,
    summary_store: SummaryStore,
    parent_summary: ScopeSummary | None,
    retirements: Sequence[tuple[str, str]],
    removals: Sequence[tuple[str, str]],
    withdraw_reasoning: str,
) -> None:
    """Write an accepted amendment's summary and everything that follows from it.

    One amendment, one summary write — so a batch of N accepts bumps
    ``version`` once (ADR 0011 D3), exactly as one accept does. The caller
    MUST hold ``_scope_lock(scope.id)``.

    The three record consequences carry attribution the CALLER resolved, since
    only it knows which contribution owns each op: *retirements* are
    ``(directive_id, reason)`` pairs, *removals* are ``(directive_id,
    trigger_contribution_id)`` pairs, and *withdraw_reasoning* explains the
    judged withdrawals. On the single path all three come from the one judged
    contribution; in a batch each op names its own member (ADR 0011 D3), and
    nothing is inferred — a ``Retirement`` row and a withdraw act are
    permanent, so a guessed owner would be a permanent misstatement of
    provenance.
    """
    assert judgment.new_summary is not None  # noqa: S101 — caller-checked invariant
    # Stamp the parent-summary version the judgment was built from, so
    # staleness stays detectable without re-running the LLM (ADR 0004 D4).
    to_write = judgment.new_summary.model_copy(
        update={"parent_version": parent_summary.version if parent_summary else None}
    )
    summary_store.write(scope.id, to_write)

    # ADR 0011 D1: a `retire` op removes a directive with no replacement,
    # so no contribution row carries the explanation — the retirement
    # event does (CONTEXT.md § Retirement), in the shape ADR 0008 D4
    # reserved for exactly this scope-manager explicit-retire. Superseded
    # directives get none: their explanation is the incoming directive's
    # own supersedes reference.
    for directive_id, reason in retirements:
        record_store.append_retirement(
            scope_id=scope.id,
            directive_id=directive_id,
            retired_by="scope-manager",
            reason=reason,
        )

    # ADR 0007 D3 — staleness propagation, two paths, both under the
    # lock this function's caller already holds:
    #
    # 1. Judged propagation (D3/D5): the judge itself named published
    #    items whose belief this rewrite drops or contradicts. Each
    #    withdrawal carries the SAME judged_by/reasoning as the judgment it
    #    came with — it was judged, just as part of that call rather than a
    #    fresh one.
    if judgment.withdraw_published:
        apply_judged_withdrawals(
            scope.id,
            judgment.withdraw_published,
            judged_by="scope-manager",
            reasoning=withdraw_reasoning,
            record_store=record_store,
            summaries_dir=str(summary_store.summaries_dir),
        )

    # 2. Mechanical propagation (D3): any published item anchored ONLY to
    #    directives that just left the summary is withdrawn, no LLM in the
    #    loop. The removed ids come straight off the amendment's
    #    supersede/retire ops (ADR 0011 D1) — the ops ARE the removal, so
    #    there is no longer anything to diff between two summary
    #    generations. Removals are grouped by the contribution each op names,
    #    so every withdraw act's trigger is the contribution that actually
    #    motivated it.
    surviving = {d.id for d in judgment.new_summary.directives}
    by_trigger: dict[str, set[str]] = {}
    for directive_id, trigger_contribution_id in removals:
        by_trigger.setdefault(trigger_contribution_id, set()).add(directive_id)
    for trigger_contribution_id, directive_ids in by_trigger.items():
        propagate_directive_removals(
            scope.id,
            directive_ids,
            trigger_contribution_id,
            surviving_directive_ids=surviving,
            record_store=record_store,
            summaries_dir=str(summary_store.summaries_dir),
        )


def _judge_batch_and_record(
    *,
    contributions: Sequence[Contribution],
    scope: Scope,
    stratum: Stratum,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    scope_manager: ScopeManager,
    summary_max_words: int,
    window_verbatim_tail: int = WINDOW_VERBATIM_TAIL,
    recency_window_size: int = RECENCY_WINDOW_SIZE,
) -> list[ContributionOutcome | JudgeUnavailable]:
    """Judge a batch of contributions in ONE call and persist the results (ADR 0011 D3).

    *contributions* are in arrival order — the order the record appended them.
    Returns one result per contribution, in that same order: its
    :class:`ContributionOutcome`, or its own :class:`JudgeUnavailable` when the
    call failed, so the drain can hand each waiting caller its own result. This
    function never raises for a judge failure; it returns it, per member.

    A batch of ONE takes the single-contribution path verbatim
    (:func:`_judge_and_record`): same tool schema, same prompt, same record
    rows. Batching is additive, never a replacement.

    Each verdict lands as its own judgment row against its own contribution id
    (the UNIQUE constraint is untouched), and a failed call writes one
    judgment-attempt row per member (issues #57/#118). The whole batch produces
    exactly ONE summary write.

    The caller MUST hold ``_scope_lock(scope.id)``.
    """
    if len(contributions) == 1:
        try:
            return [
                _judge_and_record(
                    contribution=contributions[0],
                    scope=scope,
                    stratum=stratum,
                    fleet=fleet,
                    record_store=record_store,
                    summary_store=summary_store,
                    scope_manager=scope_manager,
                    summary_max_words=summary_max_words,
                    window_verbatim_tail=window_verbatim_tail,
                    recency_window_size=recency_window_size,
                )
            ]
        except JudgeUnavailable as exc:
            return [exc]

    inputs = _read_judge_inputs(
        scope=scope,
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        recency_window_size=recency_window_size,
    )

    try:
        batch: ScopeManagerBatchJudgment = scope_manager.judge_batch(
            scope=scope,
            stratum=stratum,
            parent_summary=inputs.parent_summary,
            current_summary=inputs.current_summary,
            recent_contributions=inputs.recent_contributions,
            new_contributions=list(contributions),
            summary_max_words=summary_max_words,
            entitlement=inputs.entitlement,
            operator_memory=inputs.operator_memory,
            current_publication=inputs.current_publication,
            peer_publications=inputs.peer_publications,
            window_verbatim_tail=window_verbatim_tail,
        )
    except Exception as exc:  # noqa: BLE001 — every member needs its own error
        # One failed call strands the whole batch, so each member gets the
        # same treatment a single failed judgment gets: its own
        # judgment-attempt row marked JUDGE_FAILED (issues #57/#118), and its
        # own error carrying its own contribution id, so its caller re-judges
        # its own contribution rather than someone else's.
        return _fail_batch(contributions, exc, record_store=record_store, outcome=JUDGE_FAILED)

    # One judgment row per verdict, against its own contribution id, in
    # arrival order — the record's shape does not know this was a batch.
    for verdict in batch.verdicts:
        record_store.record_judgment(
            contribution_id=verdict.contribution_id,
            decision=verdict.decision,
            judged_by="scope-manager",
            notes=batch.record_notes_for(verdict.contribution_id),
        )

    summary_updated = False
    if batch.accepted_verdicts and batch.new_summary is not None:
        # Every op names the batch member that motivated it (ADR 0011 D3), so
        # each retirement's reason and each withdrawal's trigger come off the
        # op itself — the record never guesses which contribution meant it.
        retirements = [
            (directive_id, batch.verdict_reasoning(contribution_id or ""))
            for directive_id, contribution_id in batch.directive_retirements()
        ]
        removals = [
            (directive_id, contribution_id or "")
            for directive_id, contribution_id in batch.directive_removals()
        ]
        _write_amendment(
            batch,
            scope=scope,
            record_store=record_store,
            summary_store=summary_store,
            parent_summary=inputs.parent_summary,
            retirements=retirements,
            removals=removals,
            # A withdraw_published verdict is submitted against the amendment
            # as a whole, so it carries every accepted member's reasoning
            # rather than a guess at which one meant it.
            withdraw_reasoning=batch.batch_reasoning,
        )
        summary_updated = True

    return [
        ContributionOutcome(
            contribution_id=verdict.contribution_id,
            decision=verdict.decision,
            reasoning=verdict.reasoning,
            # A declined member did not update the summary, whatever its
            # batch-mates did — the same thing a single decline reports.
            summary_updated=summary_updated and verdict.decision != "decline",
        )
        for verdict in batch.verdicts
    ]


def _fail_batch(
    contributions: Sequence[Contribution],
    exc: Exception,
    *,
    record_store: RecordStore,
    outcome: str | None,
) -> list[ContributionOutcome | JudgeUnavailable]:
    """Record an attempt row per member and return each member's own error.

    *outcome* is :data:`JUDGE_FAILED` when the judge itself ran and failed
    (the judge run is over — issue #118), and ``None`` when the judgment was
    never attempted for these contributions, which leaves them reading as
    ``pending`` with a failed attempt rather than terminally failed.
    """
    error_class = type(exc).__name__
    message = str(exc)
    results: list[ContributionOutcome | JudgeUnavailable] = []
    for contribution in contributions:
        record_store.record_judgment_attempt(
            contribution_id=contribution.id,
            error_class=error_class,
            message=message,
            outcome=outcome,
        )
        results.append(JudgeUnavailable(contribution.id, error_class, message))
    return results


def run_contribution(
    *,
    scope: Scope,
    stratum: Stratum,
    content: str,
    proposed_classification: Literal["directive", "context"],
    subject: str | None,
    supersedes: str | None,
    contributor: ContributorRef,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    scope_manager: ScopeManager,
    summary_max_words: int,
    window_verbatim_tail: int = WINDOW_VERBATIM_TAIL,
    recency_window_size: int = RECENCY_WINDOW_SIZE,
    batch_cap: int = BATCH_CAP,
    queue_timeout_s: float = QUEUE_WAIT_TIMEOUT_S,
) -> ContributionOutcome:
    """Append a contribution to the record and get it judged (ADR 0011 D3).

    The append and the enqueue run under ``_scope_append_lock`` — a short hold
    that makes record order arrival order — and the judgment runs under
    ``_scope_lock``, the same lock the operator correction primitives take, so
    a scope's summary is still always explainable by its record (issue #38,
    ADR 0008 D4). Splitting the two is what lets a contribution arriving while
    a judgment is in flight QUEUE instead of blocking: this caller then either
    becomes the drain worker — judging everything queued, up to *batch_cap*, in
    one call (ADR 0011 D3) — or waits for its own verdict from the batch that
    includes it.

    What a caller sees is unchanged: its own :class:`ContributionOutcome`, or
    its own :class:`JudgeUnavailable` carrying its own contribution id.

    Callers validate the scope (exists / active / entitled) before calling.

    Raises:
        JudgeUnavailable: the scope-manager's judgment failed, or the wait for
            it expired. The contribution and a judgment-attempt event are
            already in the record; retry via :func:`rejudge_contribution`,
            never a fresh contribute (which would duplicate the contribution).
        sqlite3.IntegrityError: *supersedes* references a missing contribution
            (a client-input error the caller maps to its surface's error shape).
    """
    queue = _scope_queue(scope.id)
    with _scope_append_lock(scope.id):
        contribution = record_store.append_contribution(
            scope_id=scope.id,
            content=content,
            proposed_classification=proposed_classification,
            subject=subject,
            supersedes=supersedes,
            contributor=contributor,
        )
        ticket = queue.enqueue(contribution.id, contribution)

    while True:
        try:
            turn = queue.await_turn(ticket, timeout=queue_timeout_s)
        except TimeoutError as exc:
            # A wedged drain fails loudly rather than hanging this caller
            # forever. The contribution stays in the record, unjudged and
            # re-judgeable: the attempt row is written WITHOUT the
            # JUDGE_FAILED marker, since no judge run ended here (issue #118).
            queue.abandon(ticket)
            (error,) = _fail_batch([contribution], exc, record_store=record_store, outcome=None)
            raise error from exc

        if turn == "settled":
            return _unwrap_ticket(ticket)

        # This caller is now the drain worker. It judges whatever is queued —
        # its own contribution among them, unless another drain already took
        # it, in which case the batch it takes may be empty and it goes back
        # to waiting.
        try:
            batch = queue.take_batch(batch_cap)
            if batch:
                _drain_batch(
                    batch,
                    queue=queue,
                    scope=scope,
                    stratum=stratum,
                    fleet=fleet,
                    record_store=record_store,
                    summary_store=summary_store,
                    scope_manager=scope_manager,
                    summary_max_words=summary_max_words,
                    window_verbatim_tail=window_verbatim_tail,
                    recency_window_size=recency_window_size,
                )
        finally:
            # Always hand the role back, even if the drain broke: the next
            # caller takes it and judges what is still queued.
            queue.release_drain()

        if ticket.settled:
            return _unwrap_ticket(ticket)


def _drain_batch(
    batch: Sequence[QueueTicket],
    *,
    queue,  # noqa: ANN001 — strata.locks.ScopeWorkQueue, typed loosely to avoid a cycle
    scope: Scope,
    stratum: Stratum,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    scope_manager: ScopeManager,
    summary_max_words: int,
    window_verbatim_tail: int,
    recency_window_size: int = RECENCY_WINDOW_SIZE,
) -> None:
    """Judge one taken batch and publish each member's result (ADR 0011 D3).

    The judgment runs under ``_scope_lock``; the results are published AFTER
    that lock is released, so the summary lock and the queue's own condition
    are never held at the same time and cannot deadlock.

    Every taken ticket is settled before this returns — with its outcome, or,
    if the drain itself broke, with its own error. A taken ticket must never be
    left waiting on a batch nobody will judge again.
    """
    contributions = [ticket.payload for ticket in batch]
    try:
        with _scope_lock(scope.id):
            results = _judge_batch_and_record(
                contributions=contributions,
                scope=scope,
                stratum=stratum,
                fleet=fleet,
                record_store=record_store,
                summary_store=summary_store,
                scope_manager=scope_manager,
                summary_max_words=summary_max_words,
                window_verbatim_tail=window_verbatim_tail,
                recency_window_size=recency_window_size,
            )
    except Exception as exc:  # noqa: BLE001 — the drain broke, not the judge
        # Not a judge failure (those come back as results): the drain itself
        # failed — a store error, a bug. The waiters still get their own
        # errors and their own attempt rows, best-effort, and the drain's own
        # caller sees the original exception.
        try:
            results = _fail_batch(contributions, exc, record_store=record_store, outcome=None)
        except Exception:  # noqa: BLE001 — the record is unreachable too
            results = [
                JudgeUnavailable(ticket.key, type(exc).__name__, str(exc)) for ticket in batch
            ]
        queue.settle_batch(
            batch, {ticket.key: result for ticket, result in zip(batch, results, strict=True)}
        )
        raise

    queue.settle_batch(
        batch, {ticket.key: result for ticket, result in zip(batch, results, strict=True)}
    )


def _unwrap_ticket(ticket: QueueTicket) -> ContributionOutcome:
    """Return the ticket's outcome, or raise the error published for it.

    Each waiting caller gets its OWN result: its outcome, or its own
    :class:`JudgeUnavailable` carrying its own contribution id (ADR 0011 D3).
    """
    result = ticket.result
    if isinstance(result, JudgeUnavailable):
        raise result
    if not isinstance(result, ContributionOutcome):  # pragma: no cover — defensive
        raise JudgeUnavailable(
            ticket.key,
            "DrainPublishedNothing",
            f"The judgment queue settled contribution {ticket.key} with no result.",
        )
    return result


def rejudge_contribution(
    contribution_id: str,
    *,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    scope_manager: ScopeManager,
    summary_max_words: int,
    window_verbatim_tail: int = WINDOW_VERBATIM_TAIL,
    recency_window_size: int = RECENCY_WINDOW_SIZE,
) -> ContributionOutcome:
    """Idempotently (re-)judge a contribution that has no verdict yet (issue #57).

    No-op returning the existing judgment if one exists. Otherwise re-reads the
    *current* summary, judges, records the judgment, and updates the summary —
    all under the same per-scope lock as :func:`run_contribution`, so a re-judge
    never races a concurrent contribution or another re-judge (issue #38). A
    verdict is an exercise of scope authority: re-judge invokes the
    scope-manager, it never fabricates one.

    Raises:
        KeyError: *contribution_id* is not in the record.
        RuntimeError: the contribution's scope or stratum no longer resolves in
            the fleet config.
        JudgeUnavailable: the scope-manager's judge() call failed again. A fresh
            judgment-attempt-failed event is recorded; the contribution stays
            pending and can be re-judged again later.
    """
    contribution = record_store.get_contribution(contribution_id)
    if contribution is None:
        raise KeyError(f"Contribution not found: {contribution_id!r}")

    scope = fleet.get_scope(contribution.scope_id)
    if scope is None:
        raise RuntimeError(
            f"Scope {contribution.scope_id!r} for contribution {contribution_id!r} "
            "no longer exists in the fleet config."
        )
    stratum = next((s for s in fleet.strata if s.id == scope.stratum_id), None)
    if stratum is None:
        raise RuntimeError(
            f"Stratum {scope.stratum_id!r} for scope {scope.id!r} not found in fleet config."
        )

    with _scope_lock(scope.id):
        existing = record_store.get_judgment(contribution_id)
        if existing is not None:
            # Idempotent: a verdict already exists — return it, touch nothing.
            return ContributionOutcome(
                contribution_id=contribution_id,
                decision=existing.decision,
                reasoning=existing.notes or "",
                summary_updated=False,
            )
        return _judge_and_record(
            contribution=contribution,
            scope=scope,
            stratum=stratum,
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=scope_manager,
            summary_max_words=summary_max_words,
            window_verbatim_tail=window_verbatim_tail,
            recency_window_size=recency_window_size,
        )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(*, settings: Settings | None = None) -> FastAPI:
    """Construct and return the FastAPI application.

    Args:
        settings: Optional :class:`Settings` instance.  When provided, the
            app's dependency overrides are pre-wired so that
            ``get_settings`` resolves to this instance.  Useful in tests.

    Returns:
        A fully configured :class:`FastAPI` application.
    """
    resolved_settings = settings  # capture for the lifespan closure

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        effective = resolved_settings if resolved_settings is not None else get_settings()
        paths = resolve_storage_paths(effective)
        run_migrations(paths.db_path)
        # Cross-process scope lock directory (issue #19, ADR 0012): the
        # Console backend is its own OS process, separate from every
        # ``strata-mcp`` session process, so it must agree on the same
        # ``<db_dir>/.locks`` the MCP server derives in ``_init_stores``.
        configure_lock_dir(pathlib.Path(paths.db_path).parent / ".locks")
        # SummaryStore.__init__ creates summaries_dir on construct; ensure it
        # exists by instantiating one here.
        SummaryStore(paths.summaries_dir)
        # Hold a FleetReloader on app.state rather than a frozen FleetConfig
        # snapshot (lazy reload-on-read, ADR 0002 addendum): every
        # fleet-reading request stats fleet.yaml before serving, so a scope
        # added to fleet.yaml after this process started becomes visible on
        # the next request without a restart. An invalid file at reload time
        # keeps serving the last good fleet — see get_fleet()/list_scopes_endpoint.
        fleet_path = pathlib.Path(paths.fleet_yaml_path)
        app.state.fleet_reloader = FleetReloader(fleet_path)
        yield

    application = FastAPI(
        title="Strata",
        description="Shared memory for agent fleets.",
        version=__version__,
        lifespan=lifespan,
    )

    if settings is not None:
        application.dependency_overrides[get_settings] = lambda: settings

    # -----------------------------------------------------------------------
    # GET / — redirect to the Console UI
    # -----------------------------------------------------------------------

    @application.get("/", include_in_schema=False)
    def root_redirect() -> RedirectResponse:
        """Redirect the root URL to the Strata Console UI."""
        return RedirectResponse(url="/ui/index.html", status_code=307)

    # -----------------------------------------------------------------------
    # Static file mount — Strata Console UI
    # Served at /ui; resolved relative to the package root so that
    # `make run` works from any working directory.
    # -----------------------------------------------------------------------
    if _UI_DIR.is_dir():
        application.mount("/ui", StaticFiles(directory=str(_UI_DIR)), name="ui")

    # -----------------------------------------------------------------------
    # POST /contribute
    # -----------------------------------------------------------------------

    @application.post("/contribute", response_model=ContributeResponse)
    def contribute(
        body: ContributeRequest,
        request: Request,
        record_store: RecordStore = Depends(get_record_store),
        summary_store: SummaryStore = Depends(get_summary_store),
        scope_manager: ScopeManager = Depends(get_scope_manager),
        request_settings: Settings = Depends(get_settings),
    ) -> ContributeResponse:
        """Accept a contribution and invoke the scope-manager for judgment.

        Flow:
        1. Validate the target scope exists in FleetConfig (invariant 9).
        2. Validate the target scope is active (invariant 10).
        3. Append the contribution to the immutable record.
        4. Load the current summary + recent contributions for the scope-manager.
        5. Call the scope-manager.
        6. Persist the judgment.
        7. Persist the updated summary (if accepted).
        """
        fleet: FleetConfig = request.app.state.fleet_reloader.get()

        # Step 1: scope must exist in FleetConfig (invariant 9).
        scope = fleet.get_scope(body.scope_id)
        if scope is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "scope_not_found", "scope_id": body.scope_id},
            )

        # Step 2: scope must be active (invariant 10).
        if scope.status == "archived":
            raise HTTPException(
                status_code=409,
                detail={"error": "scope_not_active", "scope_id": body.scope_id},
            )

        # Resolve stratum from FleetConfig for scope-manager context.
        stratum = next(
            (s for s in fleet.strata if s.id == scope.stratum_id),
            None,
        )
        if stratum is None:
            # Invariant 4 (every scope's stratum_id resolves to a defined
            # stratum) is enforced at load and re-checked on every mutation,
            # so reaching here means the in-memory FleetConfig is internally
            # inconsistent rather than the request being at fault.
            raise HTTPException(
                status_code=500,
                detail={"error": "internal_inconsistency", "scope_id": body.scope_id},
            )

        # Steps 3–7 run through the shared contribute choke point under the
        # per-scope serialization lock (issue #38), so a concurrent operator
        # write to the same scope cannot leave the summary unexplainable by the
        # record.
        contributor_ref = ContributorRef(
            scope_id=body.contributor.scope_id,
            skill=body.contributor.skill,
            session_id=body.contributor.session_id,
            ts=body.contributor.ts,
        )
        try:
            outcome = run_contribution(
                scope=scope,
                stratum=stratum,
                content=body.content,
                proposed_classification=body.proposed_classification,
                subject=body.subject,
                supersedes=body.supersedes,
                contributor=contributor_ref,
                fleet=fleet,
                record_store=record_store,
                summary_store=summary_store,
                scope_manager=scope_manager,
                summary_max_words=request_settings.summary_max_words,
                window_verbatim_tail=request_settings.window_verbatim_tail,
                recency_window_size=request_settings.recency_window_size,
                batch_cap=request_settings.judgment_batch_cap,
            )
        except sqlite3.IntegrityError as exc:
            # The only FK on contributions is supersedes → contributions(id):
            # a bad supersedes reference is client input error, not a 500.
            raise HTTPException(
                status_code=422,
                detail={"error": "supersedes_not_found", "supersedes": body.supersedes},
            ) from exc
        except JudgeUnavailable as exc:
            # The contribution and a judgment-attempt-failed event are already
            # in the record (issue #57); carry the contribution id so a retry
            # routes to re-judge (strata_rejudge) instead of duplicating it.
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "scope_manager_failure",
                    "detail": str(exc),
                    "error_class": exc.error_class,
                    "contribution_id": exc.contribution_id,
                    "retry": "strata_rejudge",
                },
            ) from exc

        return ContributeResponse(
            contribution_id=outcome.contribution_id,
            judgment=JudgmentResult(
                decision=outcome.decision,
                reasoning=outcome.reasoning,
                summary_updated=outcome.summary_updated,
            ),
        )

    # -----------------------------------------------------------------------
    # GET /scopes
    # -----------------------------------------------------------------------

    @application.get("/scopes")
    def list_scopes_endpoint(request: Request) -> dict:
        """Return active scopes and strata from the in-memory FleetConfig.

        ``fleet_file_warning`` (Feature A, lazy reload — ADR 0002 addendum)
        is present and non-null only when the most recent fleet.yaml reload
        attempt failed: the response still carries the last KNOWN-GOOD fleet,
        but the field names the reload failure in plain language, so an
        operator watching the Console after an edit sees "your edit didn't
        take" instead of silently stale data.
        """
        reloader: FleetReloader = request.app.state.fleet_reloader
        fleet: FleetConfig = reloader.get()

        active = fleet.active_scopes()
        # Edges involving only active scopes.
        active_ids = {s.id for s in active}
        active_edges = [e for e in fleet.edges if e.from_ in active_ids and e.to in active_ids]

        return {
            "strata": [s.model_dump() for s in fleet.strata],
            "scopes": [s.model_dump() for s in active],
            # ``kind`` (ADR 0010) tells a client which edges bind: a chain
            # edge always runs child→parent after load canonicalization, a
            # reference edge always runs referencer→referenced.
            "edges": [
                {"from_scope_id": e.from_, "to_scope_id": e.to, "kind": e.kind}
                for e in active_edges
            ],
            "fleet_file_warning": reloader.warning,
        }

    # -----------------------------------------------------------------------
    # GET /staleness
    # -----------------------------------------------------------------------

    @application.get("/staleness")
    def get_staleness(
        request: Request,
        window_days: int = Query(default=30, ge=1),
        scope_id: str | None = None,
        record_store: RecordStore = Depends(get_record_store),
        summary_store: SummaryStore = Depends(get_summary_store),
        session_store: SessionStateStore = Depends(get_session_store),
    ) -> dict:
        """Return the fleet-wide staleness view (P2 proof surface).

        UI-only. No engine flow reads this; the metric itself lives in
        ``session_state.compute_fleet_staleness`` and is unchanged by this
        route.

        Returns 404 if ``scope_id`` is given but is not an active scope.
        """
        fleet: FleetConfig = request.app.state.fleet_reloader.get()
        active = fleet.active_scopes()

        if scope_id is not None:
            active = [s for s in active if s.id == scope_id]
            if not active:
                raise HTTPException(status_code=404, detail=f"Scope not found: {scope_id!r}")

        scope_ids = [s.id for s in active]
        staleness = compute_fleet_staleness(
            scope_ids,
            record_store=record_store,
            session_store=session_store,
            window_days=window_days,
        )
        by_id = {s.id: s for s in active}

        rows = []
        for metric in staleness:
            scope = by_id[metric.scope_id]
            summary = summary_store.read(metric.scope_id)
            summary_version = summary.version if summary is not None else 0
            summary_updated_at = summary.updated_at if summary is not None else None

            if summary_version == 0:
                state = "no_memory"
            elif metric.reads_since_last_contribution > 0:
                state = "stale"
            else:
                state = "fresh"

            rows.append(
                {
                    "scope_id": metric.scope_id,
                    "name": scope.name,
                    "stratum_id": scope.stratum_id,
                    "reads_since_last_contribution": metric.reads_since_last_contribution,
                    "last_accepted_contribution_at": metric.last_accepted_contribution_at,
                    "summary_version": summary_version,
                    "summary_updated_at": summary_updated_at,
                    "state": state,
                }
            )

        rows.sort(key=lambda r: (-r["reads_since_last_contribution"], r["scope_id"]))

        window_start = datetime.now(tz=UTC) - timedelta(days=window_days)
        contributions = 0
        closeouts = 0
        silent_readers = 0
        for state_row in session_store.all_states():
            updated_at = _parse_iso(state_row.updated_at)
            if updated_at is None or updated_at < window_start:
                continue
            if state_row.contributions > 0:
                contributions += 1
            elif state_row.declines > 0:
                closeouts += 1
            elif state_row.reads > 0:
                silent_readers += 1

        return {
            "window_days": window_days,
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "scopes": rows,
            "session_outcomes": {
                "contributions": contributions,
                "closeouts": closeouts,
                "silent_readers": silent_readers,
            },
        }

    # -----------------------------------------------------------------------
    # GET /scopes/{scope_id}/summary
    # -----------------------------------------------------------------------

    @application.get("/scopes/{scope_id}/summary")
    def get_scope_summary(
        scope_id: str,
        request: Request,
        summary_store: SummaryStore = Depends(get_summary_store),
        record_store: RecordStore = Depends(get_record_store),
    ) -> dict:
        """Return the scope summary.

        Returns 200 with an empty summary if the scope exists but has no summary
        yet.  Returns 404 if the scope is not in the FleetConfig.

        Carries a ``retirements`` key — the scope's own retirement events
        (ADR 0008 D4), newest first and capped at
        ``_SUMMARY_RETIREMENTS_LIMIT``, so the Console can show "retired
        here" without a second round trip. Retirements are events, not
        contributions, so they never appear in ``GET .../record``.
        """
        from dataclasses import asdict

        fleet: FleetConfig = request.app.state.fleet_reloader.get()
        scope = fleet.get_scope(scope_id)
        if scope is None:
            raise HTTPException(status_code=404, detail=f"Scope not found: {scope_id!r}")

        all_retirements = list(reversed(record_store.list_retirements(scope_id=scope_id)))
        retirements = [asdict(r) for r in all_retirements[:_SUMMARY_RETIREMENTS_LIMIT]]

        existing = summary_store.read(scope_id)
        if existing is not None:
            return {**existing.model_dump(), "retirements": retirements}

        # Scope exists but has no summary yet — return a synthesized empty
        # summary. version=0 + exists=False mark it as synthesized so it's
        # never mistaken for a real first write (version=1, exists=True) —
        # see ScopeSummary's docstring (issue #59).
        empty = ScopeSummary(
            scope_id=scope_id,
            directives=[],
            context="",
            updated_at=datetime.now(tz=UTC).isoformat(),
            version=0,
            exists=False,
        )
        return {**empty.model_dump(), "retirements": retirements}

    # -----------------------------------------------------------------------
    # GET /scopes/{scope_id}/perspective
    # -----------------------------------------------------------------------

    @application.get("/scopes/{scope_id}/perspective")
    def get_scope_perspective(
        scope_id: str,
        request: Request,
        summary_store: SummaryStore = Depends(get_summary_store),
    ) -> dict:
        """Return the composed perspective *scope_id* would receive, with token weights.

        Wires ``compose_perspective`` with the operator and publication readers
        exactly as the MCP server does (``strata/mcp/server.py``) — an operator
        inspecting "what does this agent actually see" must see operator
        layers and peer publications, never the legacy whole-face shape. The
        session nudge (``_attach_nudge``) is deliberately not applied: the
        nudge is a session-facing artefact and the Console is not a session.

        Each layer gets a ``token_estimate`` (see ``_estimate_tokens``) computed
        over its serialised payload — the structured text that actually reaches
        the agent, not just its free-text fields — and the response carries a
        ``token_estimate_total`` plus a ``token_estimate_method`` note that this
        is an estimate, not a tokenizer count.

        Returns 404 if the scope is not in the FleetConfig.
        """
        fleet: FleetConfig = request.app.state.fleet_reloader.get()
        scope = fleet.get_scope(scope_id)
        if scope is None:
            raise HTTPException(status_code=404, detail=f"Scope not found: {scope_id!r}")

        def _operator_reader(attachment_scope_id: str) -> list:
            return read_operator_layer(
                attachment_scope_id, summaries_dir=str(summary_store.summaries_dir)
            )

        def _publication_reader(peer_scope_id: str) -> list:
            return read_publication(peer_scope_id, summaries_dir=str(summary_store.summaries_dir))

        try:
            composed = compose_perspective(
                scope_id,
                fleet=fleet,
                summary_store=summary_store,
                operator_reader=_operator_reader,
                publication_reader=_publication_reader,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        total = 0
        for layer in composed["layers"]:
            payload = (
                layer.get("summary") or layer.get("publication") or layer.get("operator_memory")
            )
            estimate = _estimate_tokens(json.dumps(payload, sort_keys=True))
            layer["token_estimate"] = estimate
            total += estimate

        composed["token_estimate_total"] = total
        composed["token_estimate_method"] = "characters divided by 4 — an estimate, not a tokenizer"
        return composed

    # -----------------------------------------------------------------------
    # GET /scopes/{scope_id}/record
    # -----------------------------------------------------------------------

    @application.get("/scopes/{scope_id}/record")
    def get_scope_record(
        scope_id: str,
        request: Request,
        limit: int | None = None,
        before_id: str | None = None,
        record_store: RecordStore = Depends(get_record_store),
        settings: Settings = Depends(get_settings),
    ) -> dict:
        """Return one page of a scope's contribution record (forensic view).

        Bounded by default (issue #130): an unadorned call returns the newest
        page, not the whole record, which only ever grows. Walk back with
        ``before_id`` — the previous response's ``page.next_before_id`` —
        until that is null. ``limit`` defaults to
        ``settings.record_page_size``. Judgments, attempts, and states are
        restricted to the page's own contributions.

        Returns 404 if the scope is not in the FleetConfig, and 422 if the
        paging arguments are out of range (including a ``before_id`` that is
        not a contribution in this scope).
        """
        fleet: FleetConfig = request.app.state.fleet_reloader.get()
        scope = fleet.get_scope(scope_id)
        if scope is None:
            raise HTTPException(status_code=404, detail=f"Scope not found: {scope_id!r}")

        try:
            page = record_store.page_record(
                scope_id=scope_id,
                limit=limit if limit is not None else settings.record_page_size,
                before_id=before_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_page", "detail": str(exc)},
            ) from exc

        from dataclasses import asdict

        return {
            "contributions": [asdict(c) for c in page.contributions],
            "judgments": [asdict(j) for j in page.judgments],
            # Failed-judgment events (issue #57): let the forensic view mark a
            # pending contribution as "(pending — N failed attempts)".
            "judgment_attempts": [asdict(a) for a in page.judgment_attempts],
            # The derived per-contribution state (issue #118) so a client renders
            # "attempted, judge errored" without re-deriving the three-way join.
            "contribution_states": [asdict(s) for s in page.contribution_states],
            # next_before_id is None once the record is exhausted — the signal a
            # client pages until, rather than guessing from a short page.
            "page": {
                "limit": page.limit,
                "total": page.total,
                "next_before_id": page.next_before_id,
            },
        }

    # -----------------------------------------------------------------------
    # GET /scopes/{scope_id}/declines
    # -----------------------------------------------------------------------

    @application.get("/scopes/{scope_id}/declines")
    def get_scope_declines(
        scope_id: str,
        request: Request,
        limit: int | None = None,
        before_id: str | None = None,
        record_store: RecordStore = Depends(get_record_store),
        session_store: SessionStateStore = Depends(get_session_store),
        settings: Settings = Depends(get_settings),
    ) -> dict:
        """Return one page of a scope's declined contributions, with the judge's reasons.

        UI-only proof surface (constraint G1): nothing in the contribute/judge
        engine flow reads this. Bounded and cursor-paged exactly like
        ``GET /scopes/{scope_id}/record`` — see :meth:`RecordStore.page_declines`.

        Also reports ``mechanical_declines``: the count of sessions that read
        this scope inside the staleness window and ended having recorded
        nothing — an honest closeout signal, not a count of declines *of this
        scope*, and never rendered as record entries (see D2).

        Returns 404 if the scope is not in the FleetConfig, and 422 if the
        paging arguments are out of range (including a ``before_id`` that is
        not a declined contribution in this scope).
        """
        fleet: FleetConfig = request.app.state.fleet_reloader.get()
        scope = fleet.get_scope(scope_id)
        if scope is None:
            raise HTTPException(status_code=404, detail=f"Scope not found: {scope_id!r}")

        try:
            page = record_store.page_declines(
                scope_id=scope_id,
                limit=limit if limit is not None else settings.record_page_size,
                before_id=before_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_page", "detail": str(exc)},
            ) from exc

        window_start = datetime.now(tz=UTC) - timedelta(days=DEFAULT_STALENESS_WINDOW_DAYS)
        mechanical = 0
        for state in session_store.all_states():
            if state.declines <= 0:
                continue
            if state.contributions > 0:
                continue
            receipt = state.reads_by_scope.get(scope_id)
            if receipt is None:
                continue
            read_at = _parse_iso(receipt.last_read_at)
            if read_at is not None and read_at >= window_start:
                mechanical += 1

        return {
            "scope_id": scope_id,
            "declines": [
                {
                    "contribution_id": entry.contribution.id,
                    "content": entry.contribution.content,
                    "subject": entry.contribution.subject,
                    "proposed_classification": entry.contribution.proposed_classification,
                    "contributor": {
                        "scope_id": entry.contribution.contributor.scope_id,
                        "skill": entry.contribution.contributor.skill,
                        "session_id": entry.contribution.contributor.session_id,
                        "ts": entry.contribution.contributor.ts,
                    },
                    "created_at": entry.contribution.created_at,
                    "reason": entry.judgment.notes,
                    "judged_by": entry.judgment.judged_by,
                    "judged_at": entry.judgment.created_at,
                }
                for entry in page.declines
            ],
            "mechanical_declines": {
                "sessions_that_read_and_recorded_nothing": mechanical,
                "window_days": DEFAULT_STALENESS_WINDOW_DAYS,
            },
            "page": {
                "limit": page.limit,
                "total": page.total,
                "next_before_id": page.next_before_id,
            },
        }

    # -----------------------------------------------------------------------
    # GET /scopes/{scope_id}/record/{contribution_id}
    # -----------------------------------------------------------------------

    @application.get("/scopes/{scope_id}/record/{contribution_id}")
    def get_record_entry(
        scope_id: str,
        contribution_id: str,
        request: Request,
        record_store: RecordStore = Depends(get_record_store),
    ) -> dict:
        """Return one contribution with its state, verdict, and judgment attempts.

        The by-id read of the record (issue #130) — "did this contribution get
        judged, and what did the scope-manager say?" answered without pulling
        the scope's record. ``judgment`` is null unless the state is
        ``judged``: only a verdict carries the judge's notes.

        Returns 404 if the scope is not in the FleetConfig, or if the
        contribution is unknown or belongs to another scope.
        """
        fleet: FleetConfig = request.app.state.fleet_reloader.get()
        scope = fleet.get_scope(scope_id)
        if scope is None:
            raise HTTPException(status_code=404, detail=f"Scope not found: {scope_id!r}")

        entry = record_store.get_record_entry(contribution_id)
        # A contribution in another scope's record is "not found" here, never
        # served: the record is owned per-scope, so reading it through the
        # wrong scope's path must not reach across.
        if entry is None or entry.contribution.scope_id != scope_id:
            raise HTTPException(
                status_code=404,
                detail=f"Contribution not found: {contribution_id!r}",
            )

        from dataclasses import asdict

        return {
            "contribution": asdict(entry.contribution),
            "state": asdict(entry.state),
            "judgment": asdict(entry.judgment) if entry.judgment is not None else None,
            "judgment_attempts": [asdict(a) for a in entry.judgment_attempts],
        }

    # -----------------------------------------------------------------------
    # POST /scopes/{scope_id}/directives/{directive_id}/supersede
    # POST /scopes/{scope_id}/directives/{directive_id}/retire
    #
    # One-click operator correction, in person (ADR 0008 D4) — the Console
    # surface for `strata operator supersede` / `strata operator retire`.
    # Both routes handle c_ native-directive ids only; op_ operator-stratum
    # ids stay a command-line-only capacity (ADR 0008 D1, this task's D5).
    # -----------------------------------------------------------------------

    def _reject_operator_stratum_id(directive_id: str) -> None:
        if not directive_id.startswith("c_"):
            raise HTTPException(
                status_code=422,
                detail=(
                    "This action corrects a scope's own directive. Operator-stratum "
                    "items (ids starting with 'op_') are managed from the command line."
                ),
            )

    @application.post("/scopes/{scope_id}/directives/{directive_id}/supersede")
    def supersede_directive(
        scope_id: str,
        directive_id: str,
        body: SupersedeDirectiveRequest,
        request: Request,
        record_store: RecordStore = Depends(get_record_store),
        summary_store: SummaryStore = Depends(get_summary_store),
    ) -> dict:
        """Operator correction, in person (ADR 0008 D4) — Console surface for
        ``strata operator supersede``.

        Delegates straight to :func:`strata.operator.operator_supersede`, which takes
        :func:`strata.locks.scope_lock` itself — the SAME cross-process per-scope lock
        (ADR 0012) the CLI and the contribute path take. This route deliberately takes
        NO lock of its own. UI-only surface (constraint G1): no engine flow calls it.
        """
        from strata.operator import operator_supersede

        _reject_operator_stratum_id(directive_id)
        fleet: FleetConfig = request.app.state.fleet_reloader.get()
        try:
            new_directive = operator_supersede(
                scope_id,
                directive_id,
                body.content,
                body.subject,
                fleet=fleet,
                record_store=record_store,
                summary_store=summary_store,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
        return {
            "scope_id": scope_id,
            "superseded_directive_id": directive_id,
            "directive": new_directive.model_dump(),
        }

    @application.post("/scopes/{scope_id}/directives/{directive_id}/retire")
    def retire_directive(
        scope_id: str,
        directive_id: str,
        body: RetireDirectiveRequest,
        request: Request,
        record_store: RecordStore = Depends(get_record_store),
        summary_store: SummaryStore = Depends(get_summary_store),
    ) -> dict:
        """Operator retirement, in person (ADR 0008 D4) — Console surface for
        ``strata operator retire``.

        Delegates straight to :func:`strata.operator.operator_retire`, which takes
        :func:`strata.locks.scope_lock` itself — the SAME cross-process per-scope lock
        (ADR 0012) the CLI and the contribute path take. This route deliberately takes
        NO lock of its own. UI-only surface (constraint G1): no engine flow calls it.
        """
        from dataclasses import asdict

        from strata.operator import operator_retire

        _reject_operator_stratum_id(directive_id)
        fleet: FleetConfig = request.app.state.fleet_reloader.get()
        try:
            retirement = operator_retire(
                scope_id,
                directive_id,
                body.reason,
                fleet=fleet,
                record_store=record_store,
                summary_store=summary_store,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
        return {"scope_id": scope_id, "retirement": asdict(retirement)}

    return application


# ---------------------------------------------------------------------------
# Module-level app — used by uvicorn strata.app:app
# ---------------------------------------------------------------------------

app = create_app()
