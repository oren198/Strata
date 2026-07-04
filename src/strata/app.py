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
    Static file server for the Strata Console UI (ui/ directory).

POST /contribute
    Accept a contribution from an agent, invoke the scope-manager, persist
    the judgment, and (if accepted) update the scope summary.

    Contribute-time validation (ADR 0002 invariants 9 and 10):
    - Scope not in FleetConfig → 404 ``scope_not_found``.
    - Scope ``status == "archived"`` → 409 ``scope_not_active``.

GET /scopes
    Return active scopes and strata from FleetConfig.

GET /scopes/{scope_id}/summary
    Return the scope summary.  200 with an empty summary if the scope exists
    but has no summary yet; 404 if the scope is unknown.

GET /scopes/{scope_id}/record
    Return the contribution record + judgments for a scope (forensic view).

Vocabulary follows CONTEXT.md verbatim.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

import anthropic
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from strata.fleet_config import FleetConfig
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

# Resolve the ui/ directory relative to this file so that the static mount
# works regardless of the current working directory when the server starts.
_UI_DIR = pathlib.Path(__file__).parent.parent.parent / "ui"

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

        # Step 3: append contribution
        contributor_ref = ContributorRef(
            scope_id=body.contributor.scope_id,
            skill=body.contributor.skill,
            session_id=body.contributor.session_id,
            ts=body.contributor.ts,
        )
        try:
            contribution: Contribution = record_store.append_contribution(
                scope_id=body.scope_id,
                content=body.content,
                proposed_classification=body.proposed_classification,
                subject=body.subject,
                supersedes=body.supersedes,
                contributor=contributor_ref,
            )
        except sqlite3.IntegrityError as exc:
            # The only FK on contributions is supersedes → contributions(id):
            # a bad supersedes reference is client input error, not a 500.
            raise HTTPException(
                status_code=422,
                detail={"error": "supersedes_not_found", "supersedes": body.supersedes},
            ) from exc

        # Step 4: load summary + recent contributions
        current_summary = summary_store.read(body.scope_id)
        recent_contributions = record_store.list_contributions(scope_id=body.scope_id, limit=20)

        # Resolve the inter-stratum parent's summary for manager context (ADR 0004
        # Decision 2). The caller (here) does the graph traversal; the manager is a
        # pure judgment primitive that receives the resolved summary.
        parent_scope = fleet.inter_stratum_parent(body.scope_id)
        parent_summary = summary_store.read(parent_scope.id) if parent_scope is not None else None

        # Step 5: call scope-manager
        try:
            judgment: ScopeManagerJudgment = scope_manager.judge(
                scope=scope,
                stratum=stratum,
                parent_summary=parent_summary,
                current_summary=current_summary,
                recent_contributions=recent_contributions,
                new_contribution=contribution,
                summary_max_words=request_settings.summary_max_words,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={"error": "scope_manager_failure", "detail": str(exc)},
            ) from exc

        # Step 6: persist the judgment
        record_store.record_judgment(
            contribution_id=contribution.id,
            decision=judgment.decision,
            judged_by="scope-manager",
            notes=judgment.reasoning,
        )

        # Step 7: persist updated summary if accepted
        summary_updated = False
        if judgment.decision != "decline" and judgment.new_summary is not None:
            # Stamp the parent-summary version the judgment was built from, so
            # staleness stays detectable without re-running the LLM (ADR 0004 D4).
            to_write = judgment.new_summary.model_copy(
                update={"parent_version": parent_summary.version if parent_summary else None}
            )
            summary_store.write(body.scope_id, to_write)
            summary_updated = True

        return ContributeResponse(
            contribution_id=contribution.id,
            judgment=JudgmentResult(
                decision=judgment.decision,
                reasoning=judgment.reasoning,
                summary_updated=summary_updated,
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

        # Scope exists but has no summary yet — return an empty summary.
        empty = ScopeSummary(
            scope_id=scope_id,
            directives=[],
            context="",
            updated_at=datetime.now(tz=UTC).isoformat(),
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

        from dataclasses import asdict

        return {
            "contributions": [asdict(c) for c in contributions],
            "judgments": [asdict(j) for j in judgments],
        }

    return application


# ---------------------------------------------------------------------------
# Module-level app — used by uvicorn strata.app:app
# ---------------------------------------------------------------------------

app = create_app()
