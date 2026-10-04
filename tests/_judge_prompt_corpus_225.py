"""#225 interior assertions — the input-identity fixture.

Captured against `_build_user_message` at release/v1.17.0 @ 01f72a5, BEFORE
any #225 code change. #225's trigger reads `entitlement.others` — a list the
ENTITLEMENT block ALREADY renders into the first call's user message — and
the contribution's own already-known content; nothing new flows into
`_build_user_message` for this item, so the FIRST call's prompt is
byte-identical by construction. This fixture is the safety net proving that
claim, for a contribution whose content WOULD trigger #225 (it names another
fleet scope that is in `entitlement.others`, not entitled).
"""

from __future__ import annotations

from strata.fleet_config import EntitlementView, Scope, Stratum
from strata.record_store import Contribution, ContributorRef, RecentContribution
from strata.scope_manager import _build_user_message
from strata.summary_store import ScopeSummary

STRATUM = Stratum(id="L1", name="function", ordinal=1)
SCOPE = Scope(id="g_225corpus", name="225-corpus", stratum_id="L1")
OTHER_SCOPE = Scope(id="g_other_225", name="other-225-scope", stratum_id="L1")

CONTRIBUTOR = ContributorRef(
    scope_id="g_225corpus",
    skill="engineer",
    session_id="sess_225corpus",
    ts="2026-10-02T10:00:00+00:00",
)

CURRENT_SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[],
    context="The team favours minimal abstractions.",
    updated_at="2026-08-01T09:00:00+00:00",
)

RECENT_CONTRIBUTION = Contribution(
    id="c_225_recent",
    scope_id=SCOPE.id,
    content="An earlier observation.",
    proposed_classification="context",
    subject=None,
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-08-15T08:00:00+00:00",
)
RECENT_ROW = RecentContribution(
    contribution=RECENT_CONTRIBUTION,
    state="judged",
    decision="accept_as_context",
    judgment_notes="Accepted as context.",
)

# `other-225-scope` is in `others` (not chain/descendants/referenced_peers) —
# the exact shape #225's trigger reads, already rendered by the ENTITLEMENT
# block below.
ENTITLEMENT = EntitlementView(
    chain=[SCOPE], descendants=[], referenced_peers=[], others=[OTHER_SCOPE]
)

TRIGGERING_CONTRIBUTION = Contribution(
    id="c_225_new",
    scope_id=SCOPE.id,
    content="other-225-scope only approves changes under 5,000 EUR without a second review.",
    proposed_classification="context",
    subject=None,
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-10-02T09:00:00+00:00",
)


def capture_225_corpus() -> dict[str, str]:
    """Render the first-call user message for a #225-triggering contribution."""
    return {
        "triggering_contribution": _build_user_message(
            scope=SCOPE,
            stratum=STRATUM,
            ancestor_directives=None,
            current_summary=CURRENT_SUMMARY,
            recent_contributions=[RECENT_ROW],
            new_contribution=TRIGGERING_CONTRIBUTION,
            entitlement=ENTITLEMENT,
        )
    }
