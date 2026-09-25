"""Fixture corpus for the P1 no-judge-change golden test (#212/ADR 0017 v1.15 P1).

Captured against `_build_user_message` / `_build_batch_user_message` at the release/v1.15.0
head (4523634), BEFORE any P1 schema or code change — the same discipline #205 exists for:
render "before" only before the tree changes, never reconstruct it after. `capture_corpus()`
is the single source both the capture script and the pinning test import, so the two can
never independently drift on what the corpus contains.
"""

from __future__ import annotations

import dataclasses

from strata.fleet_config import EntitlementView, Scope, Stratum
from strata.operator import OperatorItem
from strata.record_store import Contribution, ContributorRef, RecentContribution
from strata.scope_manager import _build_batch_user_message, _build_user_message
from strata.summary_store import Directive, ScopeSummary

STRATUM = Stratum(id="L1", name="function", ordinal=1)
SCOPE = Scope(id="g_p1corpus", name="p1-corpus", stratum_id="L1")
PARENT_SCOPE = Scope(id="g_p1parent", name="p1-parent", stratum_id="L0")

CONTRIBUTOR = ContributorRef(
    scope_id="g_p1other", skill="engineer", session_id="sess_corpus", ts="2026-09-24T10:00:00+00:00"
)

EXISTING_DIRECTIVE = Directive(
    id="c_existing01",
    content="Use snake_case for identifiers.",
    subject="naming",
    source_scope_id=SCOPE.id,
    source_skill="architect",
    created_at="2026-08-01T09:00:00+00:00",
)
CURRENT_SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[EXISTING_DIRECTIVE],
    context="The team favours minimal abstractions.",
    updated_at="2026-08-01T09:00:00+00:00",
)

RECENT_CONTRIBUTION = Contribution(
    id="c_recent01",
    scope_id=SCOPE.id,
    content="An earlier observation about code style.",
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
    judgment_notes="Accepted as context: an observation, not a binding rule.",
)

PARENT_DIRECTIVE = Directive(
    id="c_parent01",
    content="All sub-teams must adhere to the company security policy.",
    subject="security-policy",
    source_scope_id=PARENT_SCOPE.id,
    source_skill="scope-manager",
    created_at="2026-01-01T00:00:00+00:00",
)
ANCESTOR_WALK = [(PARENT_SCOPE.id, [PARENT_DIRECTIVE])]

OPERATOR_DIRECTIVE = OperatorItem(
    id="op_tls123",
    kind="directive",
    content="All services must use TLS 1.3 or later.",
    subject="tls",
    created_at="2026-01-01T00:00:00+00:00",
)
OPERATOR_MEMORY = [("g_exec", [OPERATOR_DIRECTIVE])]

ENTITLEMENT = EntitlementView(chain=[SCOPE], descendants=[], referenced_peers=[], others=[])


def _plain_contribution(cid: str, content: str, *, supersedes: str | None = None) -> Contribution:
    return Contribution(
        id=cid,
        scope_id=SCOPE.id,
        content=content,
        proposed_classification="context",
        subject="corpus-item",
        supersedes=supersedes,
        contributor=CONTRIBUTOR,
        created_at="2026-09-24T09:00:00+00:00",
    )


#: Base contributions for each single-message case, keyed by corpus item name.
_BASE_CONTRIBUTIONS: dict[str, Contribution] = {
    "minimal": _plain_contribution("c_p1_minimal", "A plain observation with nothing special."),
    "supersedes": _plain_contribution(
        "c_p1_supersedes", "A correction to an earlier note.", supersedes=RECENT_CONTRIBUTION.id
    ),
    "operator_memory": _plain_contribution(
        "c_p1_opmem", "An observation judged alongside operator memory."
    ),
    "ancestor_directives": _plain_contribution(
        "c_p1_ancestor", "An observation judged alongside an inherited directive."
    ),
    "refresh_mode": _plain_contribution(
        "c_p1_refresh", "An observation judged during an input-change refresh."
    ),
}

#: (name, kwargs) for each single-message corpus item, applied over the base contribution.
_SINGLE_CASES: dict[str, dict] = {
    "minimal": {},
    "supersedes": {},
    "operator_memory": {"operator_memory": OPERATOR_MEMORY},
    "ancestor_directives": {"ancestor_directives": ANCESTOR_WALK},
    "refresh_mode": {"mode": "input_change_refresh"},
}


def capture_corpus(*, acted_on: str | None = None) -> dict[str, str]:
    """Render every corpus item and return ``{name: rendered_prompt}``.

    *acted_on* is set on every base contribution before rendering — ``None`` reproduces
    the release/v1.15.0 behaviour exactly (the field does not exist yet at capture time,
    so this is a no-op there); once P1 adds the field, passing a real id here is what
    proves the render is byte-identical whether or not a contribution carries it.
    """
    rendered: dict[str, str] = {}
    for name, kwargs in _SINGLE_CASES.items():
        contribution = _BASE_CONTRIBUTIONS[name]
        if acted_on is not None:
            contribution = dataclasses.replace(contribution, acted_on=acted_on)
        call_kwargs = {"ancestor_directives": None, **kwargs}
        rendered[name] = _build_user_message(
            scope=SCOPE,
            stratum=STRATUM,
            current_summary=CURRENT_SUMMARY,
            recent_contributions=[RECENT_ROW],
            new_contribution=contribution,
            entitlement=ENTITLEMENT,
            **call_kwargs,
        )

    batch_members = [
        _plain_contribution("c_p1_batch1", "First batch item."),
        _plain_contribution("c_p1_batch2", "Second batch item."),
        _plain_contribution(
            "c_p1_batch3", "Third batch item, superseding the first.", supersedes="c_p1_batch1"
        ),
    ]
    if acted_on is not None:
        batch_members = [dataclasses.replace(c, acted_on=acted_on) for c in batch_members]
    rendered["batch_of_3"] = _build_batch_user_message(
        scope=SCOPE,
        stratum=STRATUM,
        ancestor_directives=ANCESTOR_WALK,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[RECENT_ROW],
        new_contributions=batch_members,
        entitlement=ENTITLEMENT,
        operator_memory=OPERATOR_MEMORY,
    )
    return rendered
