"""Claims the README makes about human review that had no test of their own.

1. At level 1 the operator's own `supersede` and `retire-item` go through directly: applied at
   once, nothing held, no review act. (`publish` and `retire` are covered in
   test_v121_126_level1_passes_and_refresh.py.)
2. `ContributionState.accepted` is False after an item LAPSES, as well as while it is held and
   after it is rejected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from strata import review
from strata.operator import (
    operator_publish,
    operator_retire_item,
    operator_supersede,
    read_operator_layer,
)
from strata.record_store import ContributorRef
from strata.summary_store import Directive
from tests.v121_126_support import STAMP, World, directive_answer


@pytest.fixture
def world(tmp_path: Path):  # noqa: ANN201
    w = World(tmp_path, {"g_org": 1})
    yield w
    w.close()


def _nothing_held_and_no_review_act(w: World) -> None:
    assert review.list_held(record_store=w.store).count == 0
    assert w.store.list_review_acts() == []


def test_the_operators_own_supersede_is_applied_at_once_at_level_one(world: World) -> None:
    # a directive that came from a real contribution, so the supersession can name it
    origin = world.store.append_contribution(
        scope_id="g_org",
        content="Releases are tagged in git.",
        proposed_classification="directive",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_org", skill=None, session_id="s_org", ts="2026-11-21T09:00:00Z"
        ),
    )
    summary = world.summaries.read("g_org")
    world.summaries.write(
        "g_org",
        summary.model_copy(
            update={
                "directives": [
                    *summary.directives,
                    Directive(
                        id=origin.id,
                        content="Releases are tagged in git.",
                        source_scope_id="g_org",
                        created_at=STAMP,
                    ),
                ]
            }
        ),
    )

    operator_supersede("g_org", origin.id, "Releases are tagged and signed.", **world.kw())

    contents = [d.content for d in world.summaries.read("g_org").directives]
    assert "Releases are tagged and signed." in contents  # applied at once
    assert "Releases are tagged in git." not in contents
    assert origin.id not in world.org_ids()
    _nothing_held_and_no_review_act(world)


def test_the_operators_own_retire_item_is_applied_at_once_at_level_one(world: World) -> None:
    summaries_dir = str(world.summaries.summaries_dir)
    item = operator_publish(
        "g_org",
        "Shut the gate at dusk.",
        record_store=world.store,
        summaries_dir=summaries_dir,
        fleet=world.fleet,
    )
    assert item.id in [i.id for i in read_operator_layer("g_org", summaries_dir=summaries_dir)]

    operator_retire_item(
        "g_org",
        item.id,
        record_store=world.store,
        summaries_dir=summaries_dir,
        fleet=world.fleet,
    )

    assert item.id not in [i.id for i in read_operator_layer("g_org", summaries_dir=summaries_dir)]
    _nothing_held_and_no_review_act(world)


def test_a_lapsed_item_is_not_accepted(world: World) -> None:
    held = world.propose(directive_answer([{"op": "retire", "id": "c_tokens"}]))
    state = world.store.get_record_entry(held.contribution_id).state
    assert state.state == "held" and state.accepted is False

    world.replace_directive("c_tokens", "c_tokens_v2", "Tokens expire after 10 minutes.")
    act = review.approve(held.contribution_id, operator="oren", **world.kw())

    assert act.action == "lapse"
    assert world.store.get_judgment(held.contribution_id).disposition == "lapsed"
    state = world.store.get_record_entry(held.contribution_id).state
    assert state.disposition == "lapsed" and state.accepted is False
    assert state.state == "judged"  # the judge spoke; it is not pending and not held any more
