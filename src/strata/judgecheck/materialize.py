"""Turn a set item into the engine's own call objects.

Ported from strata-evals' J-suite `materialize` (and the P5/A2 fixtures) so that a judge-check
judgment is the SAME call a gate run makes, without importing that repo. Entitlement is always
the engine's `FleetConfig.entitlement_view()` over a small fixture fleet built from the item,
never a second implementation of the rule. A cross-check test in strata-evals compares these
objects with the ones its solvers build, field by field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from strata.fleet_config import FleetConfig, Scope, Stratum, _validate
from strata.judgecheck.items import Item
from strata.operator import OperatorItem
from strata.publication import PublishedItem
from strata.record_store import Contribution, ContributorRef, RecentContribution
from strata.summary_store import Directive, ScopeSummary

#: Provenance the P5 chain seed gives the directives it writes (as strata-evals does).
CHAIN_SEED_SKILL = "seed-agent"
CHAIN_SEED_TS = "2026-07-01T00:00:00+00:00"


def synthetic_contribution_id(item_id: str) -> str:
    """The id of the contribution under judgment: derived from the item id, stable."""
    return f"c_{item_id}"


def _directive(d: dict[str, Any]) -> Directive:
    return Directive(
        id=d["id"],
        content=d["content"],
        subject=d.get("subject"),
        source_scope_id=d["source_scope_id"],
        source_skill=d["source_skill"],
        created_at=d["created_at"],
    )


def _summary(scope_id: str, raw: dict[str, Any] | None, *, updated_at: str) -> ScopeSummary | None:
    if raw is None:
        return None
    return ScopeSummary(
        scope_id=raw.get("scope_id", scope_id),
        directives=[_directive(d) for d in raw.get("directives", [])],
        context=raw.get("context", ""),
        updated_at=updated_at,
    )


def _contributor(raw: dict[str, Any]) -> ContributorRef:
    return ContributorRef(
        scope_id=raw["scope_id"], skill=raw["skill"], session_id=raw["session_id"], ts=raw["ts"]
    )


def _contribution(raw: dict[str, Any]) -> Contribution:
    return Contribution(
        id=raw["id"],
        scope_id=raw["scope_id"],
        content=raw["content"],
        proposed_classification=raw["proposed_classification"],
        subject=raw.get("subject"),
        supersedes=raw.get("supersedes"),
        contributor=_contributor(raw["contributor"]),
        created_at=raw["created_at"],
    )


def recent_window(raws: list[dict[str, Any]]) -> list[RecentContribution]:
    """The recency-window rows a judge is handed (state, decision and notes per row)."""
    rows = []
    for raw in raws:
        state = raw.get("state", "judged")
        judged = state == "judged"
        rows.append(
            RecentContribution(
                contribution=_contribution(raw),
                state=state,
                decision=raw.get("decision", "accept_as_context") if judged else None,
                judgment_notes=raw.get("judgment_notes", "") if judged else None,
            )
        )
    return rows


def _published(raw: dict[str, Any]) -> PublishedItem:
    return PublishedItem(
        id=raw["id"],
        kind=raw["kind"],
        content=raw["content"],
        subject=raw.get("subject"),
        anchors=list(raw.get("anchors", [])),
        published_at=raw.get("published_at", raw.get("id", "")),
    )


def _resolve_parent_scope_id(scenario: dict[str, Any], scope_id: str) -> str | None:
    parent = scenario.get("parent_summary")
    if not parent:
        return None
    return parent.get("scope_id") or f"{scope_id}_parent"


def fixture_fleet(item: Item) -> FleetConfig:
    """A minimal real `FleetConfig` for one item: the judged scope, its ancestor chain (one
    stratum per level, root first) or its single parent, and any entitled material origin."""
    scenario = item.data["scenario"]
    scope_id = scenario["scope_id"]
    strata = [
        {"id": "st_scenario_parent", "name": "scenario-parent-stratum", "ordinal": 0},
        {"id": "st_scenario", "name": "scenario-stratum", "ordinal": 1},
    ]
    scopes: list[dict[str, Any]] = [{"id": scope_id, "name": scope_id, "stratum_id": "st_scenario"}]
    known = {scope_id}
    edges: list[dict[str, Any]] = []
    ancestors = scenario.get("ancestors")
    parent_id: str | None
    if ancestors:
        n = len(ancestors)
        strata = [
            {
                "id": "st_scenario_parent" if i == n - 1 else f"st_scenario_ancestor_{i}",
                "name": "scenario-parent-stratum"
                if i == n - 1
                else f"scenario-ancestor-{i}-stratum",
                "ordinal": i,
            }
            for i in range(n)
        ]
        strata.append({"id": "st_scenario", "name": "scenario-stratum", "ordinal": n})
        child = scope_id
        for i in range(n - 1, -1, -1):
            aid = ancestors[i]["scope_id"]
            if aid not in known:
                scopes.append({"id": aid, "name": aid, "stratum_id": strata[i]["id"]})
                known.add(aid)
            edges.append({"from": child, "to": aid})
            child = aid
        parent_id = None
    else:
        parent_id = _resolve_parent_scope_id(scenario, scope_id)
    if parent_id is not None and parent_id not in known:
        scopes.append({"id": parent_id, "name": parent_id, "stratum_id": "st_scenario_parent"})
        known.add(parent_id)
        edges.append({"from": scope_id, "to": parent_id})
    origin = scenario.get("material_origin")
    if origin is not None:
        origin_id = origin["source_scope_id"]
        if origin_id not in known:
            scopes.append(
                {
                    "id": origin_id,
                    "name": origin.get("source_scope_name") or origin_id,
                    "stratum_id": "st_scenario",
                }
            )
            known.add(origin_id)
        if origin.get("entitled", False):
            edges.append({"from": scope_id, "to": origin_id})
    fleet = FleetConfig.model_validate({"strata": strata, "scopes": scopes, "edges": edges})
    _validate(fleet)
    return fleet


@dataclass(frozen=True)
class JudgeCall:
    """The arguments of one `ScopeManager.judge()` call for a j1-style, j3 or a2 item."""

    kwargs: dict[str, Any]
    contribution_id: str


def judge_call(item: Item) -> JudgeCall:
    """The `ScopeManager.judge()` call for a j1-style (h2, drift, refinement, cx), j3 or a2 item."""
    scenario = item.data["scenario"]
    contribution_raw = item.data["contribution"]
    scope_id = scenario["scope_id"]
    ts = contribution_raw["contributor"]["ts"]

    scope = Scope(id=scope_id, name=scope_id, stratum_id="st_scenario")
    stratum = Stratum(id="st_scenario", name="scenario-stratum", ordinal=0)
    parent_scope_id = _resolve_parent_scope_id(scenario, scope_id) or scope_id
    parent_summary = _summary(parent_scope_id, scenario.get("parent_summary"), updated_at=ts)
    current_summary = _summary(scope_id, scenario.get("current_summary"), updated_at=ts)

    ancestors_raw = scenario.get("ancestors")
    if ancestors_raw:
        ancestors = [
            (e["scope_id"], [_directive(d) for d in e.get("directives", [])]) for e in ancestors_raw
        ]
    elif parent_summary is not None:
        ancestors = [(parent_summary.scope_id, list(parent_summary.directives))]
    else:
        ancestors = None

    contribution_id = synthetic_contribution_id(item.id)
    new_contribution = _contribution(
        {
            "id": contribution_id,
            "scope_id": scope_id,
            "content": contribution_raw["content"],
            "proposed_classification": contribution_raw["proposed_classification"],
            "subject": contribution_raw.get("subject"),
            "supersedes": contribution_raw.get("supersedes"),
            "contributor": contribution_raw["contributor"],
            "created_at": ts,
        }
    )
    operator = scenario.get("operator_memory")
    peers = scenario.get("peer_publications")
    parent_pub = scenario.get("parent_publication")
    kwargs: dict[str, Any] = {
        "scope": scope,
        "stratum": stratum,
        "ancestor_directives": ancestors,
        "current_summary": current_summary,
        "recent_contributions": recent_window(scenario.get("recent_contributions", [])),
        "new_contribution": new_contribution,
        "summary_max_words": scenario.get("summary_max_words", 500),
        "entitlement": fixture_fleet(item).entitlement_view(scope_id),
        "operator_memory": (
            [(e["attachment_scope_id"], [OperatorItem(**i) for i in e["items"]]) for e in operator]
            if operator
            else None
        ),
        "peer_publications": (
            [(e["scope_id"], [_published(i) for i in e["items"]]) for e in peers] if peers else None
        ),
    }
    if parent_pub:
        kwargs["parent_publication"] = (
            parent_pub["scope_id"],
            [_published(i) for i in parent_pub["items"]],
        )
    return JudgeCall(kwargs=kwargs, contribution_id=contribution_id)


@dataclass(frozen=True)
class PublishWorld:
    """What a P5 item needs: a chain fleet, seeded summaries and the proposal."""

    scope_id: str
    ancestor_ids: list[str]
    fleet_dict: dict[str, Any]
    seeds: list[tuple[str, ScopeSummary]]
    proposal: dict[str, Any]
    proposer: ContributorRef


def _chain_directives(scope_id: str, raw: dict[str, Any] | None) -> list[Directive]:
    return [
        Directive(
            id=d["id"],
            content=d["content"],
            subject=d.get("subject"),
            source_scope_id=scope_id,
            source_skill=CHAIN_SEED_SKILL,
            created_at=CHAIN_SEED_TS,
        )
        for d in (raw or {}).get("directives", [])
    ]


def publish_world(item: Item, *, updated_at: str = "2026-07-01T00:00:00+00:00") -> PublishWorld:
    """The throwaway world for a P5 item: a linear chain, one stratum per level, root first,
    the publisher at the bottom; ancestors' directives and the publisher's summary seeded."""
    scenario = item.data["scenario"]
    proposal = item.data["proposal"]
    scope_id = scenario["scope_id"]
    ancestors = scenario.get("ancestors", [])
    ids = [*[a["scope_id"] for a in ancestors], scope_id]
    fleet_dict = {
        "strata": [{"id": f"L{i}", "name": f"Level {i}", "ordinal": i} for i in range(len(ids))],
        "scopes": [{"id": s, "name": s, "stratum_id": f"L{i}"} for i, s in enumerate(ids)],
        "edges": [{"from": ids[i], "to": ids[i - 1]} for i in range(1, len(ids))],
    }
    seeds: list[tuple[str, ScopeSummary]] = []
    for anc in ancestors:
        seeds.append(
            (
                anc["scope_id"],
                ScopeSummary(
                    scope_id=anc["scope_id"],
                    directives=_chain_directives(anc["scope_id"], anc),
                    context="",
                    updated_at=updated_at,
                ),
            )
        )
    current = scenario.get("current_summary")
    if current:
        seeds.append(
            (
                scope_id,
                ScopeSummary(
                    scope_id=scope_id,
                    directives=_chain_directives(scope_id, current),
                    context=current.get("context", ""),
                    updated_at=updated_at,
                ),
            )
        )
    proposer = ContributorRef(
        scope_id=scope_id,
        skill="publisher-agent",
        session_id=f"p5-{item.id}",
        ts=proposal.get("ts", "2026-07-12T00:00:00+00:00"),
    )
    return PublishWorld(scope_id, ids[:-1], fleet_dict, seeds, proposal, proposer)
