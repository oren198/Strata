"""FastAPI HTTP layer for the Strata backend.

Wires the record store, summary store, and scope-manager together behind a
small REST API.  All endpoints return JSON.  Sync endpoints are used
throughout — FastAPI mixes sync and async without issue.

Fleet configuration is served from the in-memory :class:`FleetConfig` mirror
loaded at startup from ``fleet.yaml`` (ADR 0002).  The ``strata``, ``scopes``,
and ``edges`` SQLite tables are gone; scope-existence and active-status checks
are enforced against the in-memory mirror at contribute time.

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
    Return the contribution record + judgments for a scope (forensic view).

Vocabulary follows CONTEXT.md verbatim.
"""

from __future__ import annotations

import importlib.resources
import pathlib
import sqlite3
import threading
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import anthropic
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from strata.fleet_config import FleetConfig, Scope, Stratum
from strata.migrator import run_migrations
from strata.project_config import StoragePaths, resolve_storage_paths
from strata.record_store import (
    Contribution,
    ContributorRef,
    RecordStore,
)
from strata.scope_manager import ScopeManager, ScopeManagerJudgment
from strata.settings import Settings, get_settings
from strata.summary_store import ScopeSummary, SummaryStore

# Console UI static files bundled as package data (same vendoring pattern as
# _skills/ / _migrations/ / _templates/), so the static mount works regardless
# of cwd and in wheel installs (pipx, ADR 0005 / issue #65).
_UI_DIR = pathlib.Path(str(importlib.resources.files("strata"))) / "_ui"

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


def get_anthropic_client(
    settings: Settings = Depends(get_settings),
) -> anthropic.Anthropic:
    """Return an :class:`anthropic.Anthropic` client using the configured API key."""
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def get_scope_manager(
    client: anthropic.Anthropic = Depends(get_anthropic_client),
    settings: Settings = Depends(get_settings),
) -> ScopeManager:
    """Return a :class:`ScopeManager` bound to the configured model."""
    return ScopeManager(client=client, model=settings.manager_model)


def get_fleet_config(request: Request) -> FleetConfig:
    """Return the in-memory :class:`FleetConfig` from app state."""
    return request.app.state.fleet_config


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ContributorRefBody(BaseModel):
    """Provenance metadata supplied by the contributing agent."""

    scope_id: str
    skill: str
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


# ---------------------------------------------------------------------------
# Contribute choke point (issues #38, #57)
#
# The single place where the read-summary -> judge -> record-judgment ->
# summary-write sequence for a scope runs. Both agent (MCP ``strata_contribute``
# / ``strata_rejudge``) and operator (HTTP ``POST /contribute``) surfaces route
# through ``run_contribution`` / ``rejudge_contribution`` so the serialization
# invariant lives in exactly one place.
# ---------------------------------------------------------------------------

# Per-scope lock registry: scope_id -> Lock, guarded by one registry lock.
# Module-level so every code path in the process shares it.
_scope_locks: dict[str, threading.Lock] = {}
_scope_locks_guard = threading.Lock()


def _scope_lock(scope_id: str) -> threading.Lock:
    """Return the process-wide lock serialising the contribute path for *scope_id*.

    Single-process scope only (issue #38). The lock serialises the
    read-summary -> judge -> record-judgment -> summary-write sequence within
    one process — threaded FastAPI endpoints and threadpool-dispatched MCP
    tools both run the sync path in worker threads — so the summary can never
    reflect a judgment the record does not, nor silently drop a judgment the
    record kept: the summary is always explainable by the record. Cross-process
    serialisation (N separate CC sessions, each its own MCP server) is issue
    #19 and out of scope; those still serialise the record itself on the
    SQLite writer mutex.
    """
    with _scope_locks_guard:
        lock = _scope_locks.get(scope_id)
        if lock is None:
            lock = threading.Lock()
            _scope_locks[scope_id] = lock
        return lock


@dataclass
class ContributionOutcome:
    """The result of running (or re-judging) a contribution through the choke point."""

    contribution_id: str
    decision: Literal["accept_as_directive", "accept_as_context", "decline"]
    reasoning: str
    summary_updated: bool


class JudgeUnavailable(Exception):
    """Raised when the scope-manager's ``judge()`` fails during a contribution.

    The contribution is already in the record (issue #57 — the record never
    lies) and a judgment-attempt-failed *event* has been recorded against it,
    but no judgment exists: a verdict is an exercise of scope authority and no
    component outside the authority chain may forge one. Carries
    ``contribution_id`` so the caller routes a retry to re-judge
    (``strata_rejudge`` / :func:`rejudge_contribution`) instead of appending a
    duplicate contribution.
    """

    def __init__(self, contribution_id: str, error_class: str, message: str) -> None:
        self.contribution_id = contribution_id
        self.error_class = error_class
        super().__init__(message)


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
) -> ContributionOutcome:
    """Judge *contribution* against the scope's current state and persist the result.

    The caller MUST hold ``_scope_lock(scope.id)`` — this reads the current
    summary, judges, records the judgment, and writes the summary as one
    serialized unit. On judge failure it records a judgment-attempt-failed
    event and raises :class:`JudgeUnavailable`; no judgment row is written.
    """
    current_summary = summary_store.read(scope.id)
    recent_contributions = record_store.list_contributions(scope_id=scope.id, limit=20)

    # Resolve the inter-stratum parent's summary for manager context (ADR 0004
    # Decision 2). The caller does the graph traversal; the manager is a pure
    # judgment primitive that receives the resolved summary.
    parent_scope = fleet.inter_stratum_parent(scope.id)
    parent_summary = summary_store.read(parent_scope.id) if parent_scope is not None else None

    try:
        judgment: ScopeManagerJudgment = scope_manager.judge(
            scope=scope,
            stratum=stratum,
            parent_summary=parent_summary,
            current_summary=current_summary,
            recent_contributions=recent_contributions,
            new_contribution=contribution,
            summary_max_words=summary_max_words,
            entitlement=fleet.entitlement_view(scope.id),
        )
    except Exception as exc:
        # Record the failure as an event against the contribution — never as a
        # fabricated verdict (issue #57) — then surface it with the
        # contribution id so a retry routes to re-judge, not a duplicate.
        record_store.record_judgment_attempt(
            contribution_id=contribution.id,
            error_class=type(exc).__name__,
            message=str(exc),
        )
        raise JudgeUnavailable(contribution.id, type(exc).__name__, str(exc)) from exc

    record_store.record_judgment(
        contribution_id=contribution.id,
        decision=judgment.decision,
        judged_by="scope-manager",
        notes=judgment.reasoning,
    )

    summary_updated = False
    if judgment.decision != "decline" and judgment.new_summary is not None:
        # Stamp the parent-summary version the judgment was built from, so
        # staleness stays detectable without re-running the LLM (ADR 0004 D4).
        to_write = judgment.new_summary.model_copy(
            update={"parent_version": parent_summary.version if parent_summary else None}
        )
        summary_store.write(scope.id, to_write)
        summary_updated = True

    return ContributionOutcome(
        contribution_id=contribution.id,
        decision=judgment.decision,
        reasoning=judgment.reasoning,
        summary_updated=summary_updated,
    )


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
) -> ContributionOutcome:
    """Append a contribution and judge it under the scope's serialization lock.

    Fixes issue #38: the whole record-append -> read-summary -> judge ->
    record-judgment -> summary-write sequence runs under ``_scope_lock``, so two
    concurrent contributions to the same scope are judged and written one after
    the other. Each accepted judgment's content reaches the summary; the record
    carries both. The append runs inside the lock too, so the manager always
    judges against a summary consistent with every already-recorded judgment.

    Callers validate the scope (exists / active / entitled) before calling.

    Raises:
        JudgeUnavailable: the scope-manager's judge() call failed. The
            contribution and a judgment-attempt-failed event are already in the
            record; retry via :func:`rejudge_contribution`, never a fresh
            contribute (which would duplicate the contribution).
        sqlite3.IntegrityError: *supersedes* references a missing contribution
            (a client-input error the caller maps to its surface's error shape).
    """
    with _scope_lock(scope.id):
        contribution = record_store.append_contribution(
            scope_id=scope.id,
            content=content,
            proposed_classification=proposed_classification,
            subject=subject,
            supersedes=supersedes,
            contributor=contributor,
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
        )


def rejudge_contribution(
    contribution_id: str,
    *,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    scope_manager: ScopeManager,
    summary_max_words: int,
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
        # SummaryStore.__init__ creates summaries_dir on construct; ensure it
        # exists by instantiating one here.
        SummaryStore(paths.summaries_dir)
        # Load the FleetConfig mirror from fleet.yaml and hold it on app.state.
        fleet_path = pathlib.Path(paths.fleet_yaml_path)
        if fleet_path.exists():
            app.state.fleet_config = FleetConfig.load(fleet_path)
        else:
            # Start with an empty config when no fleet.yaml is present (e.g.
            # test scenarios that don't need fleet config).
            app.state.fleet_config = FleetConfig(strata=[], scopes=[], edges=[])
        yield

    application = FastAPI(
        title="Strata",
        description="Shared memory for agent fleets.",
        version="0.0.1",
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
        fleet: FleetConfig = request.app.state.fleet_config

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
        """Return active scopes and strata from the in-memory FleetConfig."""
        fleet: FleetConfig = request.app.state.fleet_config

        active = fleet.active_scopes()
        # Edges involving only active scopes.
        active_ids = {s.id for s in active}
        active_edges = [e for e in fleet.edges if e.from_ in active_ids and e.to in active_ids]

        return {
            "strata": [s.model_dump() for s in fleet.strata],
            "scopes": [s.model_dump() for s in active],
            "edges": [{"from_scope_id": e.from_, "to_scope_id": e.to} for e in active_edges],
        }

    # -----------------------------------------------------------------------
    # GET /scopes/{scope_id}/summary
    # -----------------------------------------------------------------------

    @application.get("/scopes/{scope_id}/summary")
    def get_scope_summary(
        scope_id: str,
        request: Request,
        summary_store: SummaryStore = Depends(get_summary_store),
    ) -> dict:
        """Return the scope summary.

        Returns 200 with an empty summary if the scope exists but has no summary
        yet.  Returns 404 if the scope is not in the FleetConfig.
        """
        fleet: FleetConfig = request.app.state.fleet_config
        scope = fleet.get_scope(scope_id)
        if scope is None:
            raise HTTPException(status_code=404, detail=f"Scope not found: {scope_id!r}")

        existing = summary_store.read(scope_id)
        if existing is not None:
            return existing.model_dump()

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
        return empty.model_dump()

    # -----------------------------------------------------------------------
    # GET /scopes/{scope_id}/record
    # -----------------------------------------------------------------------

    @application.get("/scopes/{scope_id}/record")
    def get_scope_record(
        scope_id: str,
        request: Request,
        record_store: RecordStore = Depends(get_record_store),
    ) -> dict:
        """Return contributions and judgments for a scope (forensic view).

        Returns 404 if the scope is not in the FleetConfig.
        """
        fleet: FleetConfig = request.app.state.fleet_config
        scope = fleet.get_scope(scope_id)
        if scope is None:
            raise HTTPException(status_code=404, detail=f"Scope not found: {scope_id!r}")

        contributions = record_store.list_contributions(scope_id=scope_id)
        judgments = record_store.list_judgments(scope_id=scope_id)
        judgment_attempts = record_store.list_judgment_attempts(scope_id=scope_id)

        from dataclasses import asdict

        return {
            "contributions": [asdict(c) for c in contributions],
            "judgments": [asdict(j) for j in judgments],
            # Failed-judgment events (issue #57): let the forensic view mark a
            # pending contribution as "(pending — N failed attempts)".
            "judgment_attempts": [asdict(a) for a in judgment_attempts],
        }

    return application


# ---------------------------------------------------------------------------
# Module-level app — used by uvicorn strata.app:app
# ---------------------------------------------------------------------------

app = create_app()
