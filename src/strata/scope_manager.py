"""Scope-manager LLM judgment layer.

Given a scope's current state and a new contribution, this module makes a
single Anthropic API call (using forced tool use) and returns a structured
:class:`ScopeManagerJudgment`.

Responsibilities
----------------
- Build the system prompt (static; cached) and the per-call user message.
- Call ``client.messages.create`` with forced ``submit_judgment`` tool use.
- Parse and validate the tool-call response.
- Wrap the LLM-provided directives + context into a complete
  :class:`~strata.summary_store.ScopeSummary` with server-side ``scope_id``
  and ``updated_at``.

This module is a **pure judgment service** — it has no persistence logic.
The caller is responsible for wiring the returned judgment to
:func:`~strata.record_store.RecordStore.record_judgment` and
:meth:`~strata.summary_store.SummaryStore.write`.

Vocabulary follows ``CONTEXT.md`` verbatim:
*contribution*, *directive*, *context*, *ratification*, *supersession*.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import anthropic
from pydantic import BaseModel

from strata.fleet_config import Scope, Stratum
from strata.record_store import Contribution
from strata.summary_store import Directive, ScopeSummary, _render_summary

# ---------------------------------------------------------------------------
# Tool definition (static — eligible for prompt caching)
# ---------------------------------------------------------------------------

JUDGE_TOOL: dict = {
    "name": "submit_judgment",
    "description": (
        "Submit the scope-manager's verdict on the new contribution and, "
        "if accepting, the rewritten scope summary."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["accept_as_directive", "accept_as_context", "decline"],
            },
            "reasoning": {
                "type": "string",
                "description": "One or two sentences explaining the verdict.",
            },
            "new_summary": {
                "type": ["object", "null"],
                "description": "Required when accepting; must be null when declining.",
                "properties": {
                    "directives": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "content": {"type": "string"},
                                "subject": {"type": ["string", "null"]},
                                "source_scope_id": {"type": "string"},
                                "source_skill": {"type": "string"},
                                "created_at": {"type": "string"},
                            },
                            "required": [
                                "id",
                                "content",
                                "source_scope_id",
                                "source_skill",
                                "created_at",
                            ],
                        },
                    },
                    "context": {"type": "string"},
                },
                "required": ["directives", "context"],
            },
        },
        "required": ["decision", "reasoning", "new_summary"],
    },
}

# ---------------------------------------------------------------------------
# System prompt (static — eligible for prompt caching)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are the scope-manager for a Strata fleet — a shared memory system for
agent fleets. Your job is to judge a single new contribution to one scope.

Concepts you must know (from CONTEXT.md):
- A scope is a bounded region of the fleet.
- A scope's summary has two sections: directives (binding decisions, listed
  individually) and context (a condensed prose digest of non-binding
  knowledge).
- You may accept the contribution as a directive (binds this scope and all
  descendants), accept it as context (informs without binding), or decline.
- The contributor's proposed classification is a hint. You may re-classify
  in either direction, including upgrading peer-submitted context into a
  directive (ratification) when accumulated evidence warrants.
- If the contribution carries a "supersedes" reference, treat it as
  explicit replacement intent — but use your own judgment.
- When accepting, REWRITE the entire summary reflecting the new state.
  Preserve existing directives unless this contribution supersedes them.
  Update the context digest to incorporate the new contribution's
  observations (and to retire stale ones).

You must call the `submit_judgment` tool exactly once and provide a
one-or-two-sentence reasoning. When declining, set `new_summary` to null.\
"""

# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


class ScopeManagerJudgment(BaseModel):
    """The scope-manager's structured verdict on a contribution.

    Returned by :meth:`ScopeManager.judge`.  When ``decision`` is
    ``"decline"``, ``new_summary`` is always ``None``.  When accepting,
    ``new_summary`` contains the fully rewritten :class:`ScopeSummary`
    (with ``scope_id`` and ``updated_at`` filled in server-side).
    """

    decision: Literal["accept_as_directive", "accept_as_context", "decline"]
    reasoning: str
    """Brief explanation of the verdict — written to the judgment record."""

    new_summary: ScopeSummary | None
    """Rewritten scope summary when accepting; ``None`` when declining."""


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _render_recent_contributions(contributions: list[Contribution]) -> str:
    """Render the recent-contributions slice for the user message."""
    if not contributions:
        return "(none)"
    lines: list[str] = []
    for c in contributions:
        lines.append(
            f"[{c.id}] {c.proposed_classification}"
            f" by {c.contributor.skill}@{c.contributor.scope_id}"
            f" at {c.contributor.ts}: {c.content!r}"
        )
    return "\n".join(lines)


def _build_user_message(
    *,
    scope: Scope,
    stratum: Stratum,
    current_summary: ScopeSummary | None,
    recent_contributions: list[Contribution],
    new_contribution: Contribution,
) -> str:
    """Compose the (non-cached) per-call user message."""
    if current_summary is not None:
        rendered_summary = _render_summary(current_summary)
    else:
        rendered_summary = "(this scope has no summary yet)"

    recent_block = _render_recent_contributions(recent_contributions)
    contributor = new_contribution.contributor

    return (
        f"SCOPE: {scope.name} (id={scope.id})\n"
        f"STRATUM: {stratum.name} (ordinal={stratum.ordinal})\n"
        "\n"
        "CURRENT SUMMARY\n"
        "---\n"
        f"{rendered_summary}\n"
        "---\n"
        "\n"
        "RECENT CONTRIBUTIONS (oldest first, for context):\n"
        f"{recent_block}\n"
        "\n"
        "NEW CONTRIBUTION TO JUDGE:\n"
        f"- id: {new_contribution.id}\n"
        f"- proposed classification: {new_contribution.proposed_classification}\n"
        f"- subject: {new_contribution.subject or '(none)'}\n"
        f"- supersedes: {new_contribution.supersedes or '(none)'}\n"
        f"- contributor: skill={contributor.skill}"
        f" scope={contributor.scope_id}"
        f" at={contributor.ts}\n"
        "- content:\n"
        f"    {new_contribution.content}\n"
        "\n"
        "Judge it. Call `submit_judgment` exactly once."
    )


# ---------------------------------------------------------------------------
# ScopeManager
# ---------------------------------------------------------------------------


class ScopeManager:
    """Invokes the Anthropic API to judge a contribution against a scope.

    The scope-manager exercises the scope's full authority: it may accept the
    contribution as a directive (binding), accept it as context (informing),
    or decline.  If accepting, it rewrites the entire scope summary.

    Args:
        client: A configured :class:`anthropic.Anthropic` instance.
        model:  The model ID to use.  Defaults to ``"claude-haiku-4-5"`` to
                match the UI prototype.
    """

    def __init__(
        self,
        *,
        client: anthropic.Anthropic,
        model: str = "claude-haiku-4-5",
    ) -> None:
        self._client = client
        self._model = model

    def judge(
        self,
        *,
        scope: Scope,
        stratum: Stratum,
        current_summary: ScopeSummary | None,
        recent_contributions: list[Contribution],
        new_contribution: Contribution,
    ) -> ScopeManagerJudgment:
        """Judge a new contribution against the scope's current state.

        Makes exactly one Anthropic API call using forced ``submit_judgment``
        tool use.  Validates the response and constructs the final
        :class:`ScopeManagerJudgment`, filling in server-side fields
        (``scope_id``, ``updated_at``) on the returned summary.

        Args:
            scope:                The scope receiving the contribution.
            stratum:              The stratum *scope* belongs to.
            current_summary:      The scope's current summary, or ``None``
                                  for a fresh scope with no prior summary.
            recent_contributions: Ordered slice of recent contributions
                                  (oldest-first) providing trend/context to
                                  the model.
            new_contribution:     The contribution to be judged.

        Returns:
            A :class:`ScopeManagerJudgment` with the verdict, reasoning, and
            (when accepting) the rewritten :class:`ScopeSummary`.

        Raises:
            ValueError: If the model response is missing the ``tool_use``
                block, or if the verdict is internally inconsistent (e.g.
                ``decline`` with a non-null ``new_summary``, or an accept
                with ``new_summary=None``).
        """
        user_message = _build_user_message(
            scope=scope,
            stratum=stratum,
            current_summary=current_summary,
            recent_contributions=recent_contributions,
            new_contribution=new_contribution,
        )

        # Build the system prompt with cache_control on the last text block
        system: list[dict] = [
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        # Tool list with cache_control applied to the tool definition
        tools: list[dict] = [
            {
                **JUDGE_TOOL,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=system,
            tools=tools,
            tool_choice={"type": "tool", "name": "submit_judgment"},
            messages=[{"role": "user", "content": user_message}],
        )

        # Extract the tool_use block
        tool_use_block = None
        for block in response.content:
            if block.type == "tool_use":
                tool_use_block = block
                break

        if tool_use_block is None:
            raise ValueError(
                "Scope-manager response contained no tool_use block; "
                "expected exactly one `submit_judgment` call."
            )

        raw: dict = tool_use_block.input
        decision: str = raw["decision"]
        reasoning: str = raw["reasoning"]
        raw_summary = raw.get("new_summary")

        # Validate consistency between decision and new_summary presence
        if decision == "decline":
            if raw_summary is not None:
                raise ValueError(
                    "Scope-manager returned decision='decline' but new_summary is not null. "
                    "A declined contribution must not produce a summary rewrite."
                )
            return ScopeManagerJudgment(
                decision="decline",
                reasoning=reasoning,
                new_summary=None,
            )

        # decision is accept_as_directive or accept_as_context
        if raw_summary is None:
            raise ValueError(
                f"Scope-manager returned decision={decision!r} but new_summary is null. "
                f"An accepted contribution must include a rewritten summary."
            )

        # Build Directive objects from the LLM-provided data
        directives: list[Directive] = []
        for d in raw_summary.get("directives", []):
            directives.append(
                Directive(
                    id=d["id"],
                    content=d["content"],
                    subject=d.get("subject"),
                    source_scope_id=d["source_scope_id"],
                    source_skill=d["source_skill"],
                    created_at=d["created_at"],
                )
            )

        updated_at = datetime.now(tz=UTC).isoformat()
        new_summary = ScopeSummary(
            scope_id=scope.id,
            directives=directives,
            context=raw_summary.get("context", ""),
            updated_at=updated_at,
        )

        return ScopeManagerJudgment(
            decision=decision,  # type: ignore[arg-type]
            reasoning=reasoning,
            new_summary=new_summary,
        )
