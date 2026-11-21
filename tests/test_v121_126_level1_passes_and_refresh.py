"""#126 / ADR 0018 D5: what level 1 does NOT hold, and the refresh path it must hold.

Not held: the operator's own acts, a date lapse, and the mechanical propagation of an
ANCESTOR's retirement. Held: a refresh judgment (ADR 0014) that changes the scope's directives.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from strata.app import drain_scope
from strata.forgetting import sweep_date_lapses
from strata.operator import operator_publish, operator_retire
from strata.record_store import ContributorRef
from strata.scope_manager import BatchVerdict, DirectiveOp, ScopeManagerBatchJudgment
from strata.summary_store import EndCondition
from tests.v121_126_support import TOKENS, World


@pytest.fixture
def world(tmp_path: Path):  # noqa: ANN201
    w = World(tmp_path, {"g_org": 1, "g_web": 1})
    yield w
    w.close()


def _held(w: World) -> int:
    from strata import review  # noqa: PLC0415

    return review.list_held(record_store=w.store).count


def test_the_operators_own_retire_passes_directly(world: World) -> None:
    operator_retire("g_org", "c_tokens", "in person", **world.kw())
    assert "c_tokens" not in world.org_ids()
    assert len(world.store.list_retirements(scope_id="g_org")) == 1
    assert _held(world) == 0


def test_an_operator_publish_passes_directly(world: World) -> None:
    operator_publish(
        "g_org",
        "Shut the gate at dusk.",
        record_store=world.store,
        summaries_dir=str(world.summaries.summaries_dir),
        fleet=world.fleet,
    )
    assert "Shut the gate at dusk." in str(world.perspective("g_web"))
    assert _held(world) == 0


def test_a_date_lapse_passes_directly(world: World) -> None:
    summary = world.summaries.read("g_org")
    world.summaries.write(
        "g_org",
        summary.model_copy(
            update={
                "directives": [
                    d.model_copy(
                        update={
                            "end_condition": EndCondition(kind="date", until="2026-11-09"),
                            "set_by": "c_x",
                        }
                    )
                    if d.id == "c_suite"
                    else d
                    for d in summary.directives
                ]
            }
        ),
    )
    done = sweep_date_lapses("g_org", now=datetime(2026, 11, 10, 12, tzinfo=UTC), **world.kw())
    assert [r.directive_id for r in done] == ["c_suite"]
    assert "c_suite" not in world.org_ids() and _held(world) == 0


def test_an_ancestors_retirement_still_reaches_a_level_one_scopes_readers_at_once(
    world: World,
) -> None:
    """g_web is a review scope, but g_org's retirement is not g_web's delegated party's act."""
    assert TOKENS in world.binding_contents("g_web")
    operator_retire("g_org", "c_tokens", "in person", **world.kw())
    assert TOKENS not in world.binding_contents("g_web")
    assert any(c["item_id"] == "c_tokens" for c in world.perspective("g_web")["input_changes"])
    assert _held(world) == 0


def test_a_refresh_judgment_that_would_retire_a_directive_is_held_at_level_one(
    world: World,
) -> None:
    """ADR 0014 D2 drops append/publish in refresh mode, but supersede and retire still apply."""
    contributor = ContributorRef(
        scope_id="g_org",
        skill="scope-manager",
        session_id="change-event",
        ts="2026-11-21T00:00:00Z",
    )
    for change_id, item in (("chg_1", "pub_1"), ("chg_2", "pub_2")):
        world.store.append_change_notice(
            scope_id="g_org",
            content=f"[Input change {change_id}: {item} was withdrawn.]",
            contributor=contributor,
            change_id=change_id,
            source_scope_id="g_org",
            item_id=item,
            kind="withdrawn",
            hop=0,
        )

    class _Manager:
        def judge(self, **_kw):  # noqa: ANN003, ANN201
            raise AssertionError("a drain of several notices judges as a batch")

        def judge_batch(self, **kwargs):  # noqa: ANN003, ANN201
            members = kwargs["new_contributions"]
            summary = world.summaries.read("g_org")
            return ScopeManagerBatchJudgment(
                verdicts=[
                    BatchVerdict(
                        contribution_id=c.id, decision="accept_as_directive", reasoning="Refresh."
                    )
                    for c in members
                ],
                directive_ops=[
                    DirectiveOp(op="retire", id="c_tokens", contribution_id=members[0].id)
                ],
                new_summary=summary.model_copy(
                    update={"directives": [d for d in summary.directives if d.id != "c_tokens"]}
                ),
                change_ids=list(kwargs.get("change_ids") or []),
                hop=kwargs.get("hop", 0),
            )

    drain_scope(
        "g_org",
        scope_manager=_Manager(),
        summary_max_words=500,
        **world.kw(),
    )
    assert "c_tokens" in world.org_ids()  # nothing applied
    assert _held(world) >= 1
