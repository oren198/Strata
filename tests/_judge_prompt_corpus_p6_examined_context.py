"""v1.16 P6 part 2 — the EXAMINED CONTEXT block (ADR 0017, issue #202/D6).

Captured against `_build_user_message` at release/v1.16.0 (through P6 part 1,
the wording fix, docs(evidence) commits — no code change since), BEFORE this
item's own code change: render "before" only before the tree changes, never
reconstruct it after (the same discipline every prior corpus module here
follows). No-examined-items renders are already pinned byte-identical by the
EXISTING corpus modules (`_judge_prompt_corpus.py`, `_v1160`, `_v1165_p5`) —
this module adds the one NEW shape those never exercised: a render WITH the
block, for one item of each examined kind.
"""

from __future__ import annotations

from strata.fleet_config import EntitlementView, Scope, Stratum
from strata.record_store import Contribution, ContributorRef, RecentContribution
from strata.scope_manager import ExaminedContextItem, _build_user_message
from strata.summary_store import ScopeSummary

STRATUM = Stratum(id="L1", name="function", ordinal=1)
SCOPE = Scope(id="g_p6corpus", name="p6-corpus", stratum_id="L1")

CONTRIBUTOR = ContributorRef(
    scope_id="g_other", skill="engineer", session_id="sess_p6corpus", ts="2026-09-26T10:00:00+00:00"
)

CURRENT_SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[],
    context="The service listens on port 8443. It corrects an earlier claim. It was raised.",
    updated_at="2026-08-01T09:00:00+00:00",
)

ENTITLEMENT = EntitlementView(chain=[SCOPE], descendants=[], referenced_peers=[], others=[])

RECENT_CONTRIBUTION = Contribution(
    id="c_p6_recent",
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

NEW_CONTRIBUTION = Contribution(
    id="c_p6_new",
    scope_id=SCOPE.id,
    content="A fresh observation.",
    proposed_classification="context",
    subject=None,
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-09-26T09:00:00+00:00",
)

EXAMINED_ITEMS = [
    ExaminedContextItem(
        contribution_id="c_p6_corroborated",
        content="The service listens on port 8443.",
        kind="corroborated",
        detail="3 held outcomes",
    ),
    ExaminedContextItem(
        contribution_id="c_p6_correcting",
        content="It corrects an earlier claim.",
        kind="correcting",
        detail="corrects c_p6_target",
    ),
    ExaminedContextItem(
        contribution_id="c_p6_raised",
        content="It was raised.",
        kind="raised",
        detail="evidence from g_child",
    ),
]


def capture_p6_examined_context_corpus() -> dict[str, str]:
    """Render the one new shape this item introduces and return
    ``{name: rendered_prompt}``."""
    with_block = _build_user_message(
        scope=SCOPE,
        stratum=STRATUM,
        ancestor_directives=None,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[RECENT_ROW],
        new_contribution=NEW_CONTRIBUTION,
        entitlement=ENTITLEMENT,
        examined_context=EXAMINED_ITEMS,
    )
    return {"examined_context_block": with_block}
