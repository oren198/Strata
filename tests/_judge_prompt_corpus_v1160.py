"""v1.16 item 1b/1c — the two rendered shapes the worked-example fix touches.

Captured against `_build_user_message` at release/v1.16.0 @ 83ad0a4 (the docs-only
commits above v1.15.0's tag; no code change yet) — same discipline as
`tests/_judge_prompt_corpus.py`: render "before" only before the tree changes, never
reconstruct it after.

The v1150 corpus (`tests/_judge_prompt_corpus.py`) already proves that setting the
bare `acted_on` FIELD changes nothing — the OUTCOME REPORT block only appears when the
caller ALSO passes `acted_on_target` (the resolved target item), which that corpus
never does. This module adds the two renders 1b's fix actually touches: an
acted_on_target-carrying render (the OUTCOME REPORT block itself) and a
claim_corrected refresh render (the INPUT CHANGES instruction line, ADR 0017 P4).
"""

from __future__ import annotations

from strata.fleet_config import EntitlementView, Scope, Stratum
from strata.record_store import ChangeEvent, Contribution, ContributorRef
from strata.scope_manager import ActedOnTarget, _build_user_message
from strata.summary_store import ScopeSummary

STRATUM = Stratum(id="L1", name="function", ordinal=1)
SCOPE = Scope(id="g_v1160corpus", name="v1160-corpus", stratum_id="L1")

CONTRIBUTOR = ContributorRef(
    scope_id="g_other", skill="engineer", session_id="sess_corpus", ts="2026-09-26T10:00:00+00:00"
)

CURRENT_SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[],
    context="The team favours minimal abstractions.",
    updated_at="2026-08-01T09:00:00+00:00",
)

ENTITLEMENT = EntitlementView(chain=[SCOPE], descendants=[], referenced_peers=[], others=[])

TARGET_CONTRIBUTION = Contribution(
    id="c_v1160_target",
    scope_id=SCOPE.id,
    content="An earlier claim, now reported on.",
    proposed_classification="context",
    subject="corpus-target",
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-09-01T09:00:00+00:00",
)
ACTED_ON_TARGET = ActedOnTarget(contribution=TARGET_CONTRIBUTION, decision="accept_as_context")

OUTCOME_CONTRIBUTION = Contribution(
    id="c_v1160_outcome",
    scope_id=SCOPE.id,
    content="A report of what happened acting on the item above.",
    proposed_classification="context",
    subject=None,
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-09-26T09:00:00+00:00",
    acted_on=TARGET_CONTRIBUTION.id,
)

REFRESH_NOTICE_CONTRIBUTION = Contribution(
    id="c_v1160_refresh_notice",
    scope_id=SCOPE.id,
    content="[Input change chg_v1160 — an input this scope's memory rests on has changed.]",
    proposed_classification="context",
    subject="manager-refresh",
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-09-26T09:00:00+00:00",
)

CLAIM_CORRECTED_EVENT = ChangeEvent(
    id="ce_v1160corpus",
    change_id="chg_v1160corpus",
    contribution_id=REFRESH_NOTICE_CONTRIBUTION.id,
    scope_id=SCOPE.id,
    source_scope_id="g_holder",
    item_id=TARGET_CONTRIBUTION.id,
    kind="claim_corrected",
    before="An earlier claim, now reported on.",
    after="The corrected observation.",
    hop=0,
    processed_at=None,
    created_at="2026-09-26T09:00:00+00:00",
)


def capture_v1160_corpus() -> dict[str, str]:
    """Render the two v1.16-specific shapes and return ``{name: rendered_prompt}``."""
    acted_on_render = _build_user_message(
        scope=SCOPE,
        stratum=STRATUM,
        ancestor_directives=None,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=OUTCOME_CONTRIBUTION,
        entitlement=ENTITLEMENT,
        acted_on_target=ACTED_ON_TARGET,
    )
    claim_corrected_refresh_render = _build_user_message(
        scope=SCOPE,
        stratum=STRATUM,
        ancestor_directives=None,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=REFRESH_NOTICE_CONTRIBUTION,
        entitlement=ENTITLEMENT,
        mode="input_change_refresh",
        input_changes=[CLAIM_CORRECTED_EVENT],
    )
    return {
        "acted_on_target": acted_on_render,
        "claim_corrected_refresh": claim_corrected_refresh_render,
    }
