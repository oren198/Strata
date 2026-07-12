"""Tests for src/strata/operator.py — the operator stratum (ADR 0008, issue #91).

Covers both of the operator's capacities (ADR 0008 Context — "two
capacities, two records"):

1. Writing the operator stratum (not judged): operator_publish,
   operator_supersede_item, operator_retire_item, read_operator_layer,
   operator_memory_binding, operator_health.
2. Correcting a scope's native memory in person (a judgment, made by the
   operator): operator_supersede, operator_retire.

Vocabulary follows CONTEXT.md verbatim: operator, directive, context, scope,
scope summary, record, contribution, retirement, supersession.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.operator import (
    OperatorItem,
    operator_health,
    operator_memory_binding,
    operator_publish,
    operator_retire,
    operator_retire_item,
    operator_supersede,
    operator_supersede_item,
    read_operator_layer,
)
from strata.record_store import ContributorRef, RecordStore
from strata.summary_store import Directive, ScopeSummary, SummaryStore

# ---------------------------------------------------------------------------
# Fixture fleet — g_exec (L0) <- g_func (L1) <- g_team (L2)
# ---------------------------------------------------------------------------


def _make_fleet(tmp_path: Path) -> FleetConfig:
    import yaml

    fleet = {
        "strata": [
            {"id": "L0", "name": "executive", "ordinal": 0},
            {"id": "L1", "name": "function", "ordinal": 1},
            {"id": "L2", "name": "team", "ordinal": 2},
        ],
        "scopes": [
            {"id": "g_exec", "name": "Executive", "stratum_id": "L0"},
            {"id": "g_func", "name": "Function", "stratum_id": "L1"},
            {"id": "g_team", "name": "Team", "stratum_id": "L2"},
        ],
        "edges": [
            {"from": "g_func", "to": "g_exec"},
            {"from": "g_team", "to": "g_func"},
        ],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return FleetConfig.load(fleet_path)


@pytest.fixture()
def record_store(tmp_path: Path):
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    store = RecordStore(db_path)
    yield store
    store.close()


@pytest.fixture()
def summaries_dir(tmp_path: Path) -> str:
    return str(tmp_path / "summaries")


@pytest.fixture()
def summary_store(summaries_dir: str) -> SummaryStore:
    return SummaryStore(summaries_dir)


@pytest.fixture()
def fleet(tmp_path: Path) -> FleetConfig:
    return _make_fleet(tmp_path)


# ---------------------------------------------------------------------------
# Capacity 1 — the operator's own record (operator_acts). Append-only,
# op_-prefixed ids, never mixed into a scope's own record.
# ---------------------------------------------------------------------------


def test_publish_appends_exactly_one_operator_act_row(record_store, summaries_dir) -> None:
    item = operator_publish(
        "g_team",
        "All services must use TLS 1.3.",
        "directive",
        "tls",
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    acts = record_store.list_operator_acts()
    assert len(acts) == 1
    act = acts[0]
    assert act.act == "publish"
    assert act.target_scope_id == "g_team"
    assert act.kind == "directive"
    assert act.content == "All services must use TLS 1.3."
    assert act.subject == "tls"
    assert act.supersedes is None
    assert act.retires is None
    assert act.id == item.id
    assert act.id.startswith("op_")


def test_supersede_item_appends_supersede_act_with_reference(record_store, summaries_dir) -> None:
    first = operator_publish(
        "g_team", "v1", "directive", record_store=record_store, summaries_dir=summaries_dir
    )
    second = operator_supersede_item(
        "g_team", first.id, "v2", record_store=record_store, summaries_dir=summaries_dir
    )
    acts = record_store.list_operator_acts(target_scope_id="g_team")
    assert [a.act for a in acts] == ["publish", "supersede"]
    supersede_act = acts[1]
    assert supersede_act.supersedes == first.id
    assert supersede_act.id == second.id
    assert supersede_act.content == "v2"
    # kind is retained from the superseded item.
    assert supersede_act.kind == "directive"


def test_retire_item_appends_retire_act_with_null_kind_and_content(
    record_store, summaries_dir
) -> None:
    item = operator_publish(
        "g_team", "context item", "context", record_store=record_store, summaries_dir=summaries_dir
    )
    act = operator_retire_item(
        "g_team", item.id, record_store=record_store, summaries_dir=summaries_dir
    )
    assert act.act == "retire"
    assert act.retires == item.id
    assert act.kind is None
    assert act.content is None


def test_operator_acts_are_append_only_across_multiple_ops(record_store, summaries_dir) -> None:
    a = operator_publish(
        "g_team", "one", "directive", record_store=record_store, summaries_dir=summaries_dir
    )
    b = operator_supersede_item(
        "g_team", a.id, "two", record_store=record_store, summaries_dir=summaries_dir
    )
    operator_retire_item("g_team", b.id, record_store=record_store, summaries_dir=summaries_dir)
    acts = record_store.list_operator_acts(target_scope_id="g_team")
    assert len(acts) == 3
    assert [act.act for act in acts] == ["publish", "supersede", "retire"]
    ids = {act.id for act in acts}
    assert len(ids) == 3  # every act has a distinct id, nothing overwritten


def test_supersede_item_unknown_id_raises_keyerror(record_store, summaries_dir) -> None:
    with pytest.raises(KeyError):
        operator_supersede_item(
            "g_team", "op_doesnotexist", "x", record_store=record_store, summaries_dir=summaries_dir
        )


def test_retire_item_unknown_id_raises_keyerror(record_store, summaries_dir) -> None:
    with pytest.raises(KeyError):
        operator_retire_item(
            "g_team", "op_doesnotexist", record_store=record_store, summaries_dir=summaries_dir
        )


# ---------------------------------------------------------------------------
# Working layer files — verbatim, round-trip, atomic.
# ---------------------------------------------------------------------------


def test_publish_writes_layer_file_readable_back(record_store, summaries_dir) -> None:
    item = operator_publish(
        "g_team",
        "Content for the layer file.",
        "directive",
        "subj",
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    layer_path = Path(summaries_dir) / "operator" / "g_team.md"
    assert layer_path.exists()

    items = read_operator_layer("g_team", summaries_dir=summaries_dir)
    assert items == [item]


def test_read_operator_layer_empty_for_unattached_scope(summaries_dir) -> None:
    assert read_operator_layer("g_nonexistent", summaries_dir=summaries_dir) == []


def test_supersede_item_replaces_old_item_in_layer(record_store, summaries_dir) -> None:
    first = operator_publish(
        "g_team", "v1", "directive", "sub", record_store=record_store, summaries_dir=summaries_dir
    )
    second = operator_supersede_item(
        "g_team", first.id, "v2", record_store=record_store, summaries_dir=summaries_dir
    )
    items = read_operator_layer("g_team", summaries_dir=summaries_dir)
    assert len(items) == 1
    assert items[0].id == second.id
    assert items[0].content == "v2"
    # Subject carries over from the superseded item when not overridden.
    assert items[0].subject == "sub"


def test_retire_item_removes_from_layer(record_store, summaries_dir) -> None:
    item = operator_publish(
        "g_team", "gone soon", "context", record_store=record_store, summaries_dir=summaries_dir
    )
    operator_retire_item("g_team", item.id, record_store=record_store, summaries_dir=summaries_dir)
    items = read_operator_layer("g_team", summaries_dir=summaries_dir)
    assert items == []


def test_layer_file_content_survives_byte_identical_round_trip(record_store, summaries_dir) -> None:
    """Multi-line, markdown-ish content must round-trip byte-for-byte."""
    tricky_content = (
        "# Not a heading, just quoted text\n"
        "## [op_fake123] directive — this must not be parsed as a real heading\n"
        "- subject: nested dash line\n"
        "> already a blockquote in source\n"
        "Final line with **markdown** and `code`."
    )
    item = operator_publish(
        "g_team",
        tricky_content,
        "directive",
        "tricky",
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    items = read_operator_layer("g_team", summaries_dir=summaries_dir)
    assert len(items) == 1
    assert items[0].content == tricky_content
    assert items[0].content == item.content


def test_multiple_items_coexist_in_one_layer_file(record_store, summaries_dir) -> None:
    a = operator_publish(
        "g_team", "first", "directive", record_store=record_store, summaries_dir=summaries_dir
    )
    b = operator_publish(
        "g_team", "second", "context", record_store=record_store, summaries_dir=summaries_dir
    )
    items = read_operator_layer("g_team", summaries_dir=summaries_dir)
    assert {i.id for i in items} == {a.id, b.id}
    by_id = {i.id: i for i in items}
    assert by_id[a.id].kind == "directive"
    assert by_id[b.id].kind == "context"


def test_layers_for_different_scopes_are_independent_files(record_store, summaries_dir) -> None:
    operator_publish(
        "g_team", "team item", "directive", record_store=record_store, summaries_dir=summaries_dir
    )
    operator_publish(
        "g_func", "func item", "context", record_store=record_store, summaries_dir=summaries_dir
    )
    team_items = read_operator_layer("g_team", summaries_dir=summaries_dir)
    func_items = read_operator_layer("g_func", summaries_dir=summaries_dir)
    assert [i.content for i in team_items] == ["team item"]
    assert [i.content for i in func_items] == ["func item"]


# ---------------------------------------------------------------------------
# operator_memory_binding — what binds a scope (for judge rendering).
# ---------------------------------------------------------------------------


def test_memory_binding_includes_self_and_ancestors_root_first(
    fleet, record_store, summaries_dir
) -> None:
    operator_publish(
        "g_exec",
        "exec directive",
        "directive",
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    operator_publish(
        "g_team",
        "team directive",
        "directive",
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    binding = operator_memory_binding("g_team", fleet=fleet, summaries_dir=summaries_dir)
    assert [scope_id for scope_id, _ in binding] == ["g_exec", "g_team"]


def test_memory_binding_skips_unattached_ancestors(fleet, record_store, summaries_dir) -> None:
    # Only g_team has operator memory; g_exec and g_func do not.
    operator_publish(
        "g_team", "team only", "context", record_store=record_store, summaries_dir=summaries_dir
    )
    binding = operator_memory_binding("g_team", fleet=fleet, summaries_dir=summaries_dir)
    assert [scope_id for scope_id, _ in binding] == ["g_team"]


def test_memory_binding_empty_when_nothing_attached(fleet, summaries_dir) -> None:
    assert operator_memory_binding("g_team", fleet=fleet, summaries_dir=summaries_dir) == []


def test_memory_binding_unknown_scope_raises(fleet, summaries_dir) -> None:
    with pytest.raises(ValueError, match="g_nonexistent"):
        operator_memory_binding("g_nonexistent", fleet=fleet, summaries_dir=summaries_dir)


# ---------------------------------------------------------------------------
# operator_health — constitutional, not operational (ADR 0008 D6).
# ---------------------------------------------------------------------------


def test_health_reports_per_scope_and_total_counts(record_store, summaries_dir) -> None:
    operator_publish(
        "g_team",
        "one two three",
        "directive",
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    operator_publish(
        "g_func", "four five", "context", record_store=record_store, summaries_dir=summaries_dir
    )
    health = operator_health(record_store=record_store, summaries_dir=summaries_dir)
    assert health["per_scope"]["g_team"] == {"items": 1, "words": 3}
    assert health["per_scope"]["g_func"] == {"items": 1, "words": 2}
    assert health["total_items"] == 2
    assert health["total_words"] == 5


def test_health_reports_act_counts_and_churn(record_store, summaries_dir) -> None:
    item = operator_publish(
        "g_team", "one", "directive", record_store=record_store, summaries_dir=summaries_dir
    )
    operator_supersede_item(
        "g_team", item.id, "two", record_store=record_store, summaries_dir=summaries_dir
    )
    health = operator_health(record_store=record_store, summaries_dir=summaries_dir)
    assert health["total_acts"] == 2
    # Both acts happened "now" — well within the default 30-day window.
    assert health["acts_last_N_days"] == 2
    assert health["churn_window_days"] == 30


def test_health_empty_when_no_operator_memory(record_store, summaries_dir) -> None:
    health = operator_health(record_store=record_store, summaries_dir=summaries_dir)
    assert health == {
        "per_scope": {},
        "total_items": 0,
        "total_words": 0,
        "total_acts": 0,
        "acts_last_N_days": 0,
        "churn_window_days": 30,
    }


def test_health_retired_items_do_not_count_toward_size(record_store, summaries_dir) -> None:
    item = operator_publish(
        "g_team",
        "will be retired",
        "directive",
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    operator_retire_item("g_team", item.id, record_store=record_store, summaries_dir=summaries_dir)
    health = operator_health(record_store=record_store, summaries_dir=summaries_dir)
    assert health["total_items"] == 0
    assert "g_team" not in health["per_scope"]
    # But the acts (publish + retire) are still counted — the record never lies.
    assert health["total_acts"] == 2


# ---------------------------------------------------------------------------
# Capacity 2 — corrections. operator_supersede / operator_retire exercise the
# TARGET SCOPE's own authority in person: contribution + judgment rows
# (judged_by="operator") for supersede; a retirements row (no fabricated
# contribution) for retire. Both splice the summary mechanically.
# ---------------------------------------------------------------------------


def _seed_directive(
    record_store: RecordStore,
    summary_store: SummaryStore,
    *,
    scope_id: str,
    content: str,
    subject: str | None = None,
) -> str:
    """Seed a native directive as if a normal contribution had been accepted."""
    contribution = record_store.append_contribution(
        scope_id=scope_id,
        content=content,
        proposed_classification="directive",
        subject=subject,
        supersedes=None,
        contributor=ContributorRef(
            scope_id=scope_id, skill="architect", session_id="s1", ts="2026-07-01T00:00:00+00:00"
        ),
    )
    record_store.record_judgment(
        contribution_id=contribution.id, decision="accept_as_directive", judged_by="scope-manager"
    )
    summary_store.write(
        scope_id,
        ScopeSummary(
            scope_id=scope_id,
            directives=[
                Directive(
                    id=contribution.id,
                    content=content,
                    subject=subject,
                    source_scope_id=scope_id,
                    source_skill="architect",
                    created_at=contribution.created_at,
                )
            ],
            context="",
            updated_at=contribution.created_at,
        ),
    )
    return contribution.id


def test_operator_supersede_leaves_contribution_and_judgment_rows(
    fleet, record_store, summary_store
) -> None:
    directive_id = _seed_directive(
        record_store, summary_store, scope_id="g_team", content="Use snake_case."
    )

    new_directive = operator_supersede(
        "g_team",
        directive_id,
        "Use snake_case, PascalCase for classes.",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
    )

    contributions = record_store.list_contributions(scope_id="g_team")
    assert len(contributions) == 2  # the original + the operator's correction
    new_contribution = next(c for c in contributions if c.id == new_directive.id)
    assert new_contribution.supersedes == directive_id
    assert new_contribution.contributor.scope_id == "operator"
    assert new_contribution.contributor.skill == "operator"
    assert new_contribution.contributor.session_id == "operator"

    judgment = record_store.get_judgment(new_directive.id)
    assert judgment is not None
    assert judgment.decision == "accept_as_directive"
    assert judgment.judged_by == "operator"


def test_operator_supersede_splices_summary_old_out_new_in(
    fleet, record_store, summary_store
) -> None:
    directive_id = _seed_directive(
        record_store, summary_store, scope_id="g_team", content="Original text."
    )
    new_directive = operator_supersede(
        "g_team",
        directive_id,
        "Corrected text.",
        subject="corrected",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
    )
    summary = summary_store.read("g_team")
    directive_ids = [d.id for d in summary.directives]
    assert directive_id not in directive_ids
    assert new_directive.id in directive_ids
    spliced = next(d for d in summary.directives if d.id == new_directive.id)
    assert spliced.content == "Corrected text."
    assert spliced.source_scope_id == "operator"
    assert spliced.source_skill == "operator"


def test_operator_supersede_propagates_directive_removal_to_publication(
    fleet, record_store, summary_store
) -> None:
    """ADR 0007 D3: superseding mechanically withdraws a directive-only-anchored published item."""
    from strata.publication import PublishedItem, _write_publication, read_publication

    directive_id = _seed_directive(
        record_store, summary_store, scope_id="g_team", content="Original text."
    )
    act = record_store.append_publication_act(
        scope_id="g_team",
        act="publish",
        kind="directive",
        content="Published copy of the original text.",
        subject=None,
        anchors=[f"directive:{directive_id}"],
        withdraws=None,
        trigger=None,
        proposer=ContributorRef(
            scope_id="g_team", skill="strata-developer", session_id="s1", ts="2026-07-01T00:00:00Z"
        ),
    )
    record_store.record_publication_judgment(
        act_id=act.id, decision="accept", judged_by="scope-manager", reasoning="seeded"
    )
    _write_publication(
        "g_team",
        [
            PublishedItem(
                id=act.id,
                kind="directive",
                content="Published copy of the original text.",
                subject=None,
                anchors=[f"directive:{directive_id}"],
                published_at=act.created_at,
            )
        ],
        summaries_dir=str(summary_store.summaries_dir),
    )

    new_directive = operator_supersede(
        "g_team",
        directive_id,
        "Corrected text.",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
    )

    remaining = read_publication("g_team", summaries_dir=str(summary_store.summaries_dir))
    assert remaining == []

    acts = record_store.list_publication_acts(scope_id="g_team")
    withdraw_act = next(a for a in acts if a.act == "withdraw")
    assert withdraw_act.withdraws == act.id
    assert withdraw_act.trigger == new_directive.id
    assert record_store.get_publication_judgment(withdraw_act.id) is None


def test_operator_supersede_unknown_directive_raises_keyerror(
    fleet, record_store, summary_store
) -> None:
    _seed_directive(record_store, summary_store, scope_id="g_team", content="Something.")
    with pytest.raises(KeyError):
        operator_supersede(
            "g_team",
            "c_doesnotexist",
            "New text.",
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
        )


def test_operator_supersede_unknown_scope_raises_valueerror(
    fleet, record_store, summary_store
) -> None:
    with pytest.raises(ValueError, match="g_nonexistent"):
        operator_supersede(
            "g_nonexistent",
            "c_whatever",
            "text",
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
        )


def test_operator_retire_leaves_retirements_row_no_contribution(
    fleet, record_store, summary_store
) -> None:
    directive_id = _seed_directive(
        record_store, summary_store, scope_id="g_team", content="To be retired."
    )
    before_contributions = record_store.list_contributions(scope_id="g_team")

    retirement = operator_retire(
        "g_team",
        directive_id,
        "no longer applicable",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
    )

    after_contributions = record_store.list_contributions(scope_id="g_team")
    # No contribution row fabricated by the retire.
    assert len(after_contributions) == len(before_contributions)

    retirements = record_store.list_retirements(scope_id="g_team")
    assert len(retirements) == 1
    assert retirements[0].id == retirement.id
    assert retirements[0].directive_id == directive_id
    assert retirements[0].retired_by == "operator"
    assert retirements[0].reason == "no longer applicable"


def test_operator_retire_filters_directive_from_summary(fleet, record_store, summary_store) -> None:
    directive_id = _seed_directive(
        record_store, summary_store, scope_id="g_team", content="Filtered directive."
    )
    operator_retire(
        "g_team", directive_id, fleet=fleet, record_store=record_store, summary_store=summary_store
    )
    summary = summary_store.read("g_team")
    assert directive_id not in [d.id for d in summary.directives]
    assert summary.directives == []


def test_operator_retire_propagates_directive_removal_to_publication(
    fleet, record_store, summary_store
) -> None:
    """ADR 0007 D3: retiring mechanically withdraws a directive-only-anchored published item.

    A published item carrying an ADDITIONAL surviving subject anchor is
    spared — only a fully directive-anchored item is withdrawn.
    """
    from strata.publication import PublishedItem, _write_publication, read_publication

    directive_id = _seed_directive(
        record_store, summary_store, scope_id="g_team", content="To be retired."
    )
    proposer = ContributorRef(
        scope_id="g_team", skill="strata-developer", session_id="s1", ts="2026-07-01T00:00:00Z"
    )

    withdrawn_act = record_store.append_publication_act(
        scope_id="g_team",
        act="publish",
        kind="directive",
        content="Directive-only anchored — must be withdrawn.",
        subject=None,
        anchors=[f"directive:{directive_id}"],
        withdraws=None,
        trigger=None,
        proposer=proposer,
    )
    record_store.record_publication_judgment(
        act_id=withdrawn_act.id, decision="accept", judged_by="scope-manager", reasoning="seeded"
    )
    surviving_act = record_store.append_publication_act(
        scope_id="g_team",
        act="publish",
        kind="directive",
        content="Also anchored to a subject — must survive.",
        subject="policy",
        anchors=[f"directive:{directive_id}", "subject:policy"],
        withdraws=None,
        trigger=None,
        proposer=proposer,
    )
    record_store.record_publication_judgment(
        act_id=surviving_act.id, decision="accept", judged_by="scope-manager", reasoning="seeded"
    )
    _write_publication(
        "g_team",
        [
            PublishedItem(
                id=withdrawn_act.id,
                kind="directive",
                content="Directive-only anchored — must be withdrawn.",
                subject=None,
                anchors=[f"directive:{directive_id}"],
                published_at=withdrawn_act.created_at,
            ),
            PublishedItem(
                id=surviving_act.id,
                kind="directive",
                content="Also anchored to a subject — must survive.",
                subject="policy",
                anchors=[f"directive:{directive_id}", "subject:policy"],
                published_at=surviving_act.created_at,
            ),
        ],
        summaries_dir=str(summary_store.summaries_dir),
    )

    retirement = operator_retire(
        "g_team", directive_id, fleet=fleet, record_store=record_store, summary_store=summary_store
    )

    remaining = read_publication("g_team", summaries_dir=str(summary_store.summaries_dir))
    assert [i.id for i in remaining] == [surviving_act.id]

    acts = record_store.list_publication_acts(scope_id="g_team")
    withdraw_act = next(a for a in acts if a.act == "withdraw")
    assert withdraw_act.withdraws == withdrawn_act.id
    assert withdraw_act.trigger == retirement.id
    assert record_store.get_publication_judgment(withdraw_act.id) is None


def test_operator_retire_unknown_directive_raises_keyerror(
    fleet, record_store, summary_store
) -> None:
    _seed_directive(record_store, summary_store, scope_id="g_team", content="Something.")
    with pytest.raises(KeyError):
        operator_retire(
            "g_team",
            "c_doesnotexist",
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
        )


def test_operator_retire_unknown_scope_raises_valueerror(
    fleet, record_store, summary_store
) -> None:
    with pytest.raises(ValueError, match="g_nonexistent"):
        operator_retire(
            "g_nonexistent",
            "c_whatever",
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
        )


def test_corrections_are_explainable_by_the_record(fleet, record_store, summary_store) -> None:
    """ADR 0008 D4 / #38 invariant: the summary is always explainable by the record."""
    directive_id = _seed_directive(record_store, summary_store, scope_id="g_team", content="First.")
    new_directive = operator_supersede(
        "g_team",
        directive_id,
        "Second.",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
    )
    operator_retire(
        "g_team",
        new_directive.id,
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
    )

    summary = summary_store.read("g_team")
    assert summary.directives == []

    # Every step is reconstructable from the record: two contributions
    # (original seed + operator's supersede), one retirement event.
    contributions = record_store.list_contributions(scope_id="g_team")
    assert len(contributions) == 2
    retirements = record_store.list_retirements(scope_id="g_team")
    assert len(retirements) == 1
    assert retirements[0].directive_id == new_directive.id


# ---------------------------------------------------------------------------
# Operator-stratum acts never enter a scope's record, and scope corrections
# never enter the operator's own record — two capacities, two records.
# ---------------------------------------------------------------------------


def test_operator_stratum_acts_never_appear_in_scope_record(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    operator_publish(
        "g_team",
        "operator layer item",
        "directive",
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    contributions = record_store.list_contributions(scope_id="g_team")
    assert contributions == []


def test_scope_corrections_never_appear_in_operator_acts(
    fleet, record_store, summary_store
) -> None:
    directive_id = _seed_directive(
        record_store, summary_store, scope_id="g_team", content="Native directive."
    )
    operator_supersede(
        "g_team",
        directive_id,
        "Corrected.",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
    )
    assert record_store.list_operator_acts() == []


# ---------------------------------------------------------------------------
# OperatorItem is a plain, comparable dataclass.
# ---------------------------------------------------------------------------


def test_operator_item_equality() -> None:
    a = OperatorItem(id="op_1", kind="directive", content="x", subject=None, created_at="t")
    b = OperatorItem(id="op_1", kind="directive", content="x", subject=None, created_at="t")
    assert a == b
