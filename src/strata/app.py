"""FastAPI HTTP layer for the Strata backend.

Wires the record store, summary store, and scope-manager together behind a
small REST API.  All endpoints return JSON.  Sync endpoints are used
throughout — FastAPI mixes sync and async without issue.

Endpoints
---------
POST /contribute
    Accept a contribution from an agent, invoke the scope-manager, persist
    the judgment, and (if accepted) update the scope summary.

GET /scopes
    Return the full fleet config (strata, scopes, edges) for the UI.

GET /scopes/{scope_id}/summary
    Return the scope summary.  200 with an empty summary if the scope exists
    but has no summary yet; 404 if the scope is unknown.

GET /scopes/{scope_id}/record
    Return the contribution record + judgments for a scope (forensic view).

Vocabulary follows CONTEXT.md verbatim.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Literal

import anthropic
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from scripts.run_migrations import run_migrations
from strata.record_store import (
    Contribution,
    ContributorRef,
    RecordStore,
    Stratum,
)
from strata.scope_manager import ScopeManager, ScopeManagerJudgment
from strata.settings import Settings, get_settings
from strata.summary_store import ScopeSummary, SummaryStore

# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------


def get_record_store(
    settings: Settings = Depends(get_settings),
) -> Generator[RecordStore, None, None]:
    """Yield a fresh :class:`RecordStore` per request, closing it afterwards."""
    store = RecordStore(settings.db_path)
    try:
        yield store
    finally:
        store.close()


def get_summary_store(
    settings: Settings = Depends(get_settings),
) -> SummaryStore:
    """Return a :class:`SummaryStore` for the configured summaries directory."""
    return SummaryStore(settings.summaries_dir)


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
        run_migrations(effective.db_path)
        # SummaryStore.__init__ creates summaries_dir on construct; ensure it
        # exists by instantiating one here.
        SummaryStore(effective.summaries_dir)
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
    # POST /contribute
    # -----------------------------------------------------------------------

    @application.post("/contribute", response_model=ContributeResponse)
    def contribute(
        body: ContributeRequest,
        record_store: RecordStore = Depends(get_record_store),
        summary_store: SummaryStore = Depends(get_summary_store),
        scope_manager: ScopeManager = Depends(get_scope_manager),
    ) -> ContributeResponse:
        """Accept a contribution and invoke the scope-manager for judgment.

        Flow:
        1. Validate the target scope exists.
        2. Append the contribution to the immutable record.
        3. Load the current summary + recent contributions for the scope-manager.
        4. Call the scope-manager.
        5. Persist the judgment.
        6. Persist the updated summary (if accepted).
        """
        # Step 1: resolve scope and stratum
        scope = record_store.get_scope(body.scope_id)
        if scope is None:
            raise HTTPException(status_code=404, detail=f"Scope not found: {body.scope_id!r}")

        strata = record_store.list_strata()
        stratum: Stratum | None = next((s for s in strata if s.id == scope.stratum_id), None)
        if stratum is None:
            raise HTTPException(
                status_code=500,
                detail=f"Stratum {scope.stratum_id!r} for scope {body.scope_id!r} not found.",
            )

        # Step 2: append contribution
        contributor_ref = ContributorRef(
            scope_id=body.contributor.scope_id,
            skill=body.contributor.skill,
            session_id=body.contributor.session_id,
            ts=body.contributor.ts,
        )
        contribution: Contribution = record_store.append_contribution(
            scope_id=body.scope_id,
            content=body.content,
            proposed_classification=body.proposed_classification,
            subject=body.subject,
            supersedes=body.supersedes,
            contributor=contributor_ref,
        )

        # Step 3: load summary + recent contributions
        current_summary = summary_store.read(body.scope_id)
        recent_contributions = record_store.list_contributions(scope_id=body.scope_id, limit=20)

        # Step 4: call scope-manager
        try:
            judgment: ScopeManagerJudgment = scope_manager.judge(
                scope=scope,
                stratum=stratum,
                current_summary=current_summary,
                recent_contributions=recent_contributions,
                new_contribution=contribution,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={"error": "scope_manager_failure", "detail": str(exc)},
            ) from exc

        # Step 5: persist the judgment
        record_store.record_judgment(
            contribution_id=contribution.id,
            decision=judgment.decision,
            judged_by="scope-manager",
            notes=judgment.reasoning,
        )

        # Step 6: persist updated summary if accepted
        summary_updated = False
        if judgment.decision != "decline" and judgment.new_summary is not None:
            summary_store.write(body.scope_id, judgment.new_summary)
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
    def list_scopes_endpoint(
        record_store: RecordStore = Depends(get_record_store),
    ) -> dict:
        """Return the full fleet config: strata, scopes, and edges."""
        strata = record_store.list_strata()
        scopes = record_store.list_scopes()
        edges = record_store.list_edges()

        return {
            "strata": [asdict(s) for s in strata],
            "scopes": [asdict(s) for s in scopes],
            "edges": [asdict(e) for e in edges],
        }

    # -----------------------------------------------------------------------
    # GET /scopes/{scope_id}/summary
    # -----------------------------------------------------------------------

    @application.get("/scopes/{scope_id}/summary")
    def get_scope_summary(
        scope_id: str,
        record_store: RecordStore = Depends(get_record_store),
        summary_store: SummaryStore = Depends(get_summary_store),
    ) -> dict:
        """Return the scope summary.

        Returns 200 with an empty summary if the scope exists but has no summary
        yet.  Returns 404 if the scope does not exist.
        """
        scope = record_store.get_scope(scope_id)
        if scope is None:
            raise HTTPException(status_code=404, detail=f"Scope not found: {scope_id!r}")

        existing = summary_store.read(scope_id)
        if existing is not None:
            return existing.model_dump()

        # Scope exists but has no summary yet — return an empty summary
        empty = ScopeSummary(
            scope_id=scope_id,
            directives=[],
            context="",
            updated_at=scope.created_at,
        )
        return empty.model_dump()

    # -----------------------------------------------------------------------
    # GET /scopes/{scope_id}/record
    # -----------------------------------------------------------------------

    @application.get("/scopes/{scope_id}/record")
    def get_scope_record(
        scope_id: str,
        record_store: RecordStore = Depends(get_record_store),
    ) -> dict:
        """Return contributions and judgments for a scope (forensic view).

        Returns 404 if the scope does not exist.
        """
        scope = record_store.get_scope(scope_id)
        if scope is None:
            raise HTTPException(status_code=404, detail=f"Scope not found: {scope_id!r}")

        contributions = record_store.list_contributions(scope_id=scope_id)
        judgments = record_store.list_judgments(scope_id=scope_id)

        return {
            "contributions": [asdict(c) for c in contributions],
            "judgments": [asdict(j) for j in judgments],
        }

    return application


# ---------------------------------------------------------------------------
# Module-level app — used by uvicorn strata.app:app
# ---------------------------------------------------------------------------

app = create_app()
