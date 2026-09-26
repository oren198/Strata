"""v1.16 P5 — the directive-target acted_on render, captured BASE (before P5's
narrower {held, failed, decline} contract lands).

Captured against release/v1.16.0 @ dc7c527 (through #221, before any P5 code
change) — same discipline as `tests/_judge_prompt_corpus_v1160.py`: render
"before" only before the tree changes, never reconstruct it after.

The v1160 corpus already covers a CONTEXT acted_on_target (decision=
"accept_as_context"); it never exercises decision="accept_as_directive", which
is the ONE existing render P5 changes (from the four-way enum + directive
caveat, to the new three-way held/failed/decline block). This module pins
that base shape, and the base JUDGE_TOOL schema `_judge_tool_for` returns for
it, so the P5 change is provably deliberate rather than an accidental drift.

There is no base fixture for an OPERATOR-issued directive target: that code
path does not exist yet at this commit, so there is nothing to preserve.
"""

from __future__ import annotations

from strata.fleet_config import EntitlementView, Scope, Stratum
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import ActedOnTarget, _build_user_message, _judge_tool_for
from strata.summary_store import ScopeSummary

STRATUM = Stratum(id="L1", name="function", ordinal=1)
SCOPE = Scope(id="g_v1165p5corpus", name="v1165-p5-corpus", stratum_id="L1")

CONTRIBUTOR = ContributorRef(
    scope_id="g_other", skill="engineer", session_id="sess_p5corpus", ts="2026-09-26T10:00:00+00:00"
)

CURRENT_SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[],
    context="The team favours minimal abstractions.",
    updated_at="2026-08-01T09:00:00+00:00",
)

ENTITLEMENT = EntitlementView(chain=[SCOPE], descendants=[], referenced_peers=[], others=[])

DIRECTIVE_TARGET_CONTRIBUTION = Contribution(
    id="c_v1165p5_directive_target",
    scope_id=SCOPE.id,
    content="Ship behind a feature flag.",
    proposed_classification="directive",
    subject="rollout",
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-09-01T09:00:00+00:00",
)
DIRECTIVE_ACTED_ON_TARGET = ActedOnTarget(
    contribution=DIRECTIVE_TARGET_CONTRIBUTION, decision="accept_as_directive"
)

DIRECTIVE_OUTCOME_CONTRIBUTION = Contribution(
    id="c_v1165p5_directive_outcome",
    scope_id=SCOPE.id,
    content="Followed the flag rollout; it broke on staging.",
    proposed_classification="context",
    subject=None,
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-09-26T09:00:00+00:00",
    acted_on=DIRECTIVE_TARGET_CONTRIBUTION.id,
)


def capture_v1165_p5_corpus() -> dict[str, object]:
    """Render the one v1.16-P5-specific shape that already exists at base, plus
    its tool schema, and return ``{name: rendered_prompt_or_tool}``."""
    directive_target_render = _build_user_message(
        scope=SCOPE,
        stratum=STRATUM,
        ancestor_directives=None,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=DIRECTIVE_OUTCOME_CONTRIBUTION,
        entitlement=ENTITLEMENT,
        acted_on_target=DIRECTIVE_ACTED_ON_TARGET,
    )
    directive_target_tool = _judge_tool_for(DIRECTIVE_ACTED_ON_TARGET)
    return {
        "directive_target": directive_target_render,
        "directive_target_tool": directive_target_tool,
    }
