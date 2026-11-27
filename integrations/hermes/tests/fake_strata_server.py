"""A real ``strata-mcp`` stdio server with a SCRIPTED judge, for the strata-hermes tests.

Runs in its own process (the provider reaches it only over stdio). The judge declines a
contribution containing DECLINE and otherwise accepts it as context, so a test can tell what the
judge admitted. Storage paths come from the environment.
"""

from __future__ import annotations

from datetime import UTC, datetime

import strata.mcp.server as server
from strata.scope_manager import ScopeManagerJudgment
from strata.summary_store import ScopeSummary


class ScriptedJudge:
    def judge(self, *, scope, new_contribution, current_summary, **_kwargs):  # noqa: ANN003, ANN201
        text = new_contribution.content
        if "DECLINE" in text:
            return ScopeManagerJudgment(
                decision="decline", reasoning="Scripted decline.", new_summary=None
            )
        base = current_summary or ScopeSummary(
            scope_id=scope.id, directives=[], context="", updated_at=datetime.now(UTC).isoformat()
        )
        context = f"{base.context} {text}".strip()
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="Scripted accept.",
            new_context=context,
            new_summary=base.model_copy(update={"context": context}),
        )


server._build_scope_manager = lambda: ScriptedJudge()  # noqa: SLF001
server.main()
