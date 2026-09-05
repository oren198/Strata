"""Change-event emission from the contribution choke point (ADR 0014 D1/D4).

A scope's own contribution is not a trigger for the scope itself — it
already has a path (ADR 0014 D1). It IS a trigger for its descendants, when
the judgment's directive ops change what those descendants compose: an
appended directive binds them, a retired one stops binding them, and neither
is something they can see for themselves.

Pin 7's damping is the other half: a context-only rewrite emits nothing, so
a rewording cannot restart a wave.

Vocabulary follows CONTEXT.md: scope, contribution, judgment, record,
directive, scope summary, change event.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# Make strata importable when running from the repo root.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.app import run_contribution  # noqa: E402
from strata.fleet_config import FleetConfig  # noqa: E402
from strata.migrator import run_migrations  # noqa: E402
from strata.record_store import ContributorRef, RecordStore  # noqa: E402
from strata.scope_manager import ScopeManagerJudgment  # noqa: E402
from strata.summary_store import Directive, ScopeSummary, SummaryStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures — g_parent (L0) <- g_child (L1). The scope queue is process-wide
# and keyed by scope id (ADR 0011 D3), so these ids are unique to this file.
# ---------------------------------------------------------------------------


@pytest.fixture
def fleet(tmp_path: Path) -> FleetConfig:
    raw = {
        "strata": [
            {"id": "L0", "name": "executive", "ordinal": 0},
            {"id": "L1", "name": "team", "ordinal": 1},
        ],
        "scopes": [
            {"id": "g_ce_parent", "name": "Parent", "stratum_id": "L0"},
            {"id": "g_ce_child", "name": "Child", "stratum_id": "L1"},
        ],
        "edges": [{"from": "g_ce_child", "to": "g_ce_parent"}],
    }
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.dump(raw, default_flow_style=False), encoding="utf-8")
    return FleetConfig.load(path)


@pytest.fixture
def record_store(tmp_path: Path):  # noqa: ANN201 — RecordStore context manager
    db_path = str(tmp_path / "record.db")
    run_migrations(db_path)
    with RecordStore(db_path) as store:
        yield store


@pytest.fixture
def summary_store(tmp_path: Path) -> SummaryStore:
    return SummaryStore(str(tmp_path / "summaries"))


def _contributor() -> ContributorRef:
    return ContributorRef(
        scope_id="g_ce_parent",
        skill="strata-developer",
        session_id="sess_test",
        ts="2026-09-05T00:00:00+00:00",
    )


def _directive(directive_id: str, content: str = "Use protobuf.") -> Directive:
    return Directive(
        id=directive_id,
        content=content,
        subject="rpc",
        source_scope_id="g_ce_parent",
        source_skill="strata-developer",
        created_at="2026-09-05T00:00:00+00:00",
    )


def _summary(*directives: Directive, context: str = "") -> ScopeSummary:
    return ScopeSummary(
        scope_id="g_ce_parent",
        directives=list(directives),
        context=context,
        updated_at="2026-09-05T00:00:00+00:00",
    )


class _ScriptedManager:
    """Returns a prepared judgment, whatever it is asked to judge."""

    def __init__(self, judgment: ScopeManagerJudgment) -> None:
        self.judgment = judgment

    def judge(self, *, new_contribution, **_kwargs):  # noqa: ANN001, ANN003, ANN201
        return self.judgment


def _run(fleet, record_store, summary_store, judgment, content="A new rule."):  # noqa: ANN001, ANN201
    return run_contribution(
        scope=fleet.get_scope("g_ce_parent"),
        stratum=next(s for s in fleet.strata if s.id == "L0"),
        content=content,
        proposed_classification="directive",
        subject="rpc",
        supersedes=None,
        contributor=_contributor(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=_ScriptedManager(judgment),
        summary_max_words=500,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_an_appended_directive_tells_the_descendants(fleet, record_store, summary_store) -> None:
    """The scope's own contribution is not a trigger for itself, but its
    descendants compose the directive it just gained (ADR 0014 D1/D3)."""
    judgment = ScopeManagerJudgment(
        decision="accept_as_directive",
        reasoning="Binding.",
        new_summary=_summary(_directive("c_new")),
    )

    outcome = _run(fleet, record_store, summary_store, judgment)

    # Precondition: the contribution really was judged and the summary written.
    assert outcome.decision == "accept_as_directive"
    assert outcome.summary_updated is True

    (event,) = record_store.list_change_events(scope_id="g_ce_child")
    assert event.kind == "directive_appended"
    assert event.item_id == "c_new"
    assert "c_new" in (event.after or "")
    # The scope's own record carries the contribution, never a notice to itself.
    assert record_store.list_change_events(scope_id="g_ce_parent") == []


def test_a_retired_directive_tells_the_descendants(fleet, record_store, summary_store) -> None:
    summary_store.write("g_ce_parent", _summary(_directive("c_old")))
    judgment = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="No longer binding.",
        new_summary=_summary(context="Noted."),
    )

    outcome = _run(fleet, record_store, summary_store, judgment)

    assert outcome.summary_updated is True
    (event,) = record_store.list_change_events(scope_id="g_ce_child")
    assert event.kind == "directive_retired"
    assert event.item_id == "c_old"
    assert "c_old" in (event.before or "")


def test_a_replaced_directive_reads_as_superseded(fleet, record_store, summary_store) -> None:
    summary_store.write("g_ce_parent", _summary(_directive("c_old")))
    judgment = ScopeManagerJudgment(
        decision="accept_as_directive",
        reasoning="Replaced.",
        new_summary=_summary(_directive("c_new", content="Use gRPC.")),
    )

    _run(fleet, record_store, summary_store, judgment)

    (event,) = record_store.list_change_events(scope_id="g_ce_child")
    assert event.kind == "directive_superseded"
    assert "c_old" in (event.before or "")
    assert "c_new" in (event.after or "")


def test_a_context_only_rewrite_emits_nothing(fleet, record_store, summary_store) -> None:
    """Fixpoint damping (implementation pin 7): rewording cannot restart a wave.

    Precondition asserted first — the amendment really was written."""
    summary_store.write("g_ce_parent", _summary(_directive("c_keep"), context="Old wording."))
    judgment = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="Reworded.",
        new_summary=_summary(_directive("c_keep"), context="New wording."),
    )

    outcome = _run(fleet, record_store, summary_store, judgment)

    assert outcome.summary_updated is True
    assert summary_store.read("g_ce_parent").context == "New wording."
    assert record_store.list_change_events(scope_id="g_ce_child") == []


def test_a_decline_emits_nothing(fleet, record_store, summary_store) -> None:
    """A decline changes no input. Precondition: the verdict IS in the record."""
    judgment = ScopeManagerJudgment(
        decision="decline", reasoning="Not for this scope.", new_summary=None
    )

    outcome = _run(fleet, record_store, summary_store, judgment)

    assert record_store.get_judgment(outcome.contribution_id) is not None
    assert record_store.list_change_events(scope_id="g_ce_child") == []


def test_a_judgment_carrying_a_change_id_makes_the_derived_event_inherit_it(
    fleet, record_store, summary_store
) -> None:
    """ADR 0014 D4 — a directive admitted on a refresh is a DERIVED change and
    inherits the id of the change that triggered the refresh."""
    judgment = ScopeManagerJudgment(
        decision="accept_as_directive",
        reasoning="Admitted on a refresh.",
        new_summary=_summary(_directive("c_new")),
        change_id="chg_upstream",
    )

    _run(fleet, record_store, summary_store, judgment)

    (event,) = record_store.list_change_events(scope_id="g_ce_child")
    assert event.change_id == "chg_upstream"


def test_a_scope_with_no_descendants_emits_nothing(fleet, record_store, summary_store) -> None:
    """Pin 7: emission needs somebody to emit to. Precondition first — the
    same amendment shape from the PARENT does reach a descendant."""
    judgment = ScopeManagerJudgment(
        decision="accept_as_directive",
        reasoning="Binding.",
        new_summary=_summary(_directive("c_new")),
    )
    _run(fleet, record_store, summary_store, judgment)
    assert record_store.list_change_events(scope_id="g_ce_child")

    child_judgment = ScopeManagerJudgment(
        decision="accept_as_directive",
        reasoning="Binding here too.",
        new_summary=ScopeSummary(
            scope_id="g_ce_child",
            directives=[_directive("c_child_new")],
            context="",
            updated_at="2026-09-05T00:00:00+00:00",
        ),
    )
    outcome = run_contribution(
        scope=fleet.get_scope("g_ce_child"),
        stratum=next(s for s in fleet.strata if s.id == "L1"),
        content="A leaf rule.",
        proposed_classification="directive",
        subject="rpc",
        supersedes=None,
        contributor=_contributor(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=_ScriptedManager(child_judgment),
        summary_max_words=500,
    )

    assert outcome.summary_updated is True
    # Nobody below the leaf: no new events anywhere.
    assert record_store.list_change_events(scope_id="g_ce_parent") == []
    assert len(record_store.list_change_events(scope_id="g_ce_child")) == 1


# ---------------------------------------------------------------------------
# The HTTP surface (implementation pin 3)
# ---------------------------------------------------------------------------


def test_the_contribute_route_emits_because_it_shares_the_choke_point(
    tmp_path: Path, fleet: FleetConfig
) -> None:
    """``POST /contribute`` routes through :func:`run_contribution` — the one
    choke point every surface shares — so it needs no hook of its own, and
    one route test proves the whole path.
    """
    from fastapi.testclient import TestClient

    from strata.app import create_app, get_scope_manager
    from strata.settings import Settings

    db_path = str(tmp_path / "route.db")
    run_migrations(db_path)
    settings = Settings(
        db_path=db_path,
        summaries_dir=str(tmp_path / "summaries"),
        fleet_yaml_path=str(tmp_path / "fleet.yaml"),
        manager_model="claude-haiku-4-5",
        anthropic_api_key="test-key",
    )
    application = create_app(settings=settings)
    application.dependency_overrides[get_scope_manager] = lambda: _ScriptedManager(
        ScopeManagerJudgment(
            decision="accept_as_directive",
            reasoning="Binding.",
            new_summary=_summary(_directive("c_route")),
        )
    )

    with TestClient(application) as tc:
        response = tc.post(
            "/contribute",
            json={
                "scope_id": "g_ce_parent",
                "content": "A new rule.",
                "proposed_classification": "directive",
                "contributor": {
                    "scope_id": "g_ce_parent",
                    "skill": "strata-developer",
                    "session_id": "sess_test",
                    "ts": "2026-09-05T00:00:00+00:00",
                },
            },
        )

    # Precondition: the route accepted and the amendment was written.
    assert response.status_code == 200, response.text
    assert response.json()["judgment"]["decision"] == "accept_as_directive"

    with RecordStore(db_path) as store:
        (event,) = store.list_change_events(scope_id="g_ce_child")
    assert event.kind == "directive_appended"


def test_one_amendment_is_one_input_change(fleet, record_store, summary_store) -> None:
    """An amendment that withdraws a published item AND moves the directive
    set is ONE input change (ADR 0014 D4), so every notice it produces —
    across both emitters — carries the same change id.
    """
    from strata.publication import PublishedItem, _write_publication, read_publication

    summaries_dir = str(summary_store.summaries_dir)
    act = record_store.append_publication_act(
        scope_id="g_ce_parent",
        act="publish",
        kind="context",
        content="Deploys are at 3pm.",
        subject="deploys",
        anchors=["subject:deploys"],
        withdraws=None,
        trigger=None,
        proposer=_contributor(),
    )
    _write_publication(
        "g_ce_parent",
        [
            PublishedItem(
                id=act.id,
                kind="context",
                content="Deploys are at 3pm.",
                subject="deploys",
                anchors=["subject:deploys"],
                published_at=act.created_at,
            )
        ],
        summaries_dir=summaries_dir,
    )

    judgment = ScopeManagerJudgment(
        decision="accept_as_directive",
        reasoning="No longer believed; new rule binds.",
        new_summary=_summary(_directive("c_new")),
        withdraw_published=[act.id],
    )

    outcome = _run(fleet, record_store, summary_store, judgment)

    # Preconditions: the amendment really did both things.
    assert outcome.summary_updated is True
    assert read_publication("g_ce_parent", summaries_dir=summaries_dir) == []

    events = record_store.list_change_events(scope_id="g_ce_child")
    assert {e.kind for e in events} == {"withdrawn", "directive_appended"}
    assert len({e.change_id for e in events}) == 1
