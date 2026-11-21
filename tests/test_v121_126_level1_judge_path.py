"""#126 / ADR 0018 D2, D3, D5, D6, D7: level 1, the judge path.

A judgment that would change the scope's directives is recorded VERBATIM with disposition
``held`` and nothing is applied. Context and decline land as today. Level 0 is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.v121_126_support import NEW_RULE, TOKENS, World, directive_answer

EVIDENCE = "The note says the audit closed on 9 November."


@pytest.fixture
def world1(tmp_path: Path):  # noqa: ANN201
    w = World(tmp_path, {"g_org": 1})
    yield w
    w.close()


@pytest.fixture
def world0(tmp_path: Path):  # noqa: ANN201
    w = World(tmp_path)
    yield w
    w.close()


def _assert_held_verbatim(w: World, outcome, decision: str = "accept_as_directive") -> None:  # noqa: ANN001
    judgment = w.store.get_judgment(outcome.contribution_id)
    assert judgment.decision == decision  # the verdict is never overwritten
    assert judgment.disposition == "held"
    state = w.store.get_record_entry(outcome.contribution_id).state
    assert state.state == "held"
    assert state.disposition == "held"
    assert state.accepted is False  # a held item was never admitted
    assert outcome.summary_updated is False


# -- 2. level 0 is today's behaviour ----------------------------------------------------------


def test_a_level_zero_scope_admits_a_directive_exactly_as_today(world0: World) -> None:
    outcome = world0.propose(directive_answer([{"op": "append"}]))
    assert NEW_RULE in [d.content for d in world0.summaries.read("g_org").directives]
    judgment = world0.store.get_judgment(outcome.contribution_id)
    assert judgment.decision == "accept_as_directive" and judgment.disposition is None
    state = world0.store.get_record_entry(outcome.contribution_id).state
    assert state.state == "judged" and state.accepted is True
    assert outcome.summary_updated is True


# -- 3. level 1: every directive change by the judge is held ------------------------------------


def test_a_judged_append_is_held(world1: World) -> None:
    before = world1.summaries.read("g_org")
    outcome = world1.propose(directive_answer([{"op": "append"}]))
    _assert_held_verbatim(world1, outcome)
    assert world1.summaries.read("g_org") == before  # the summary is unchanged
    assert NEW_RULE not in world1.binding_contents("g_web")
    assert NEW_RULE not in world1.binding_contents("g_api")


def test_a_judged_supersede_is_held_and_the_old_directive_still_binds(world1: World) -> None:
    outcome = world1.propose(
        directive_answer([{"op": "supersede", "id": "c_tokens"}, {"op": "append"}])
    )
    _assert_held_verbatim(world1, outcome)
    assert world1.org_ids() == ["c_suite", "c_tokens"]
    assert TOKENS in world1.binding_contents("g_web")
    assert NEW_RULE not in world1.binding_contents("g_web")


def test_a_judged_retire_is_held_and_leaves_the_directive_binding(world1: World) -> None:
    outcome = world1.propose(
        directive_answer(
            [{"op": "retire", "id": "c_tokens", "changed_circumstance": "no longer so"}]
        )
    )
    _assert_held_verbatim(world1, outcome)
    assert "c_tokens" in world1.org_ids()
    assert world1.store.list_retirements(scope_id="g_org") == []
    assert TOKENS in world1.binding_contents("g_web")


def test_a_publish_op_is_held_too(world1: World) -> None:
    outcome = world1.propose(
        directive_answer([{"op": "publish", "content": "Two approvals per release."}])
    )
    _assert_held_verbatim(world1, outcome)
    assert "Two approvals per release." not in world1.binding_contents("g_web")


def test_a_judged_end_condition_confirmation_is_held(world1: World) -> None:
    world1.set_judged_end("c_suite")
    outcome = world1.propose(
        directive_answer([{"op": "retire", "id": "c_suite", "changed_circumstance": EVIDENCE}]),
        content="The audit closed on 9 November.",
        fulfils="c_suite",
    )
    _assert_held_verbatim(world1, outcome)
    assert "c_suite" in world1.org_ids()  # R2 would have retired it at level 0
    assert world1.store.list_retirements(scope_id="g_org") == []


def test_context_and_decline_land_as_today(world1: World) -> None:
    ctx = world1.propose(
        {
            "decision": "accept_as_context",
            "reasoning": "A plain fact.",
            "directive_ops": [],
            "new_context": "The audit closed on 9 November.",
        },
        content="The audit closed on 9 November.",
        classification="context",
    )
    state = world1.store.get_record_entry(ctx.contribution_id).state
    assert state.state == "judged" and state.accepted is True and state.disposition is None
    assert "audit closed" in world1.summaries.read("g_org").context

    declined = world1.propose(
        {
            "decision": "decline",
            "reasoning": "Not relevant.",
            "directive_ops": [],
            "new_context": None,
        },
        content="Lunch is at noon.",
    )
    state = world1.store.get_record_entry(declined.contribution_id).state
    assert state.state == "judged" and state.accepted is False and state.disposition is None


def test_reclassification_is_one_way_a_binding_reading_is_held_even_as_context(
    world1: World,
) -> None:
    """D7: proposed as context, but the judge reads it as binding: held, never admitted."""
    outcome = world1.propose(directive_answer([{"op": "append"}]), classification="context")
    _assert_held_verbatim(world1, outcome)
    assert NEW_RULE not in world1.binding_contents("g_web")


# -- 7. visibility: the contributing scope sees the held proposal ---------------------------------


def test_the_contributing_scope_sees_a_labelled_held_proposal_and_not_a_directive(
    world1: World,
) -> None:
    outcome = world1.propose(directive_answer([{"op": "append"}]))
    own = world1.perspective("g_org")
    text = str(own)
    assert "held for the operator" in text and "not binding" in text
    assert outcome.contribution_id in text
    assert NEW_RULE not in [
        d["content"]
        for layer in own["layers"]
        for d in layer.get("directives") or []
        if layer.get("scope_id") == "g_org" and layer.get("binding")
    ]
    # the condensed context prose never states it as settled
    assert NEW_RULE not in world1.summaries.read("g_org").context


def test_a_held_item_is_not_pending_judgment(world1: World) -> None:
    world1.propose(directive_answer([{"op": "append"}]))
    states = world1.store.list_contribution_states(scope_id="g_org")
    assert [s.state for s in states] == ["held"]


# -- the position gate fires first; the review hold never double-handles ---------------------------


def _non_owner_proposal(w: World):  # noqa: ANN202
    """g_api is not g_org's bound position, so the position gate (ScopeManager.judge) holds the
    directive change itself, before the review hold seam is reached."""
    return w.propose(
        directive_answer([{"op": "append"}]),
        scope_id="g_org",
        session="s_g_api",
        contributor_scope="g_api",
    )


def test_where_the_position_gate_fires_level_one_behaves_exactly_as_today(
    world0: World, world1: World
) -> None:
    today = _non_owner_proposal(world0)
    level1 = _non_owner_proposal(world1)
    j0 = world0.store.get_judgment(today.contribution_id)
    j1 = world1.store.get_judgment(level1.contribution_id)
    assert (j1.decision, j1.notes, j1.disposition) == (j0.decision, j0.notes, None)
    assert world1.summaries.read("g_org").directives == world0.summaries.read("g_org").directives
    assert NEW_RULE not in [d.content for d in world1.summaries.read("g_org").directives]
    # the position gate fired first, so there was nothing left for the review hold to hold
    from strata import review  # noqa: PLC0415

    assert review.list_held(record_store=world1.store).count == 0


# -- a no-op accept_as_directive is held, and approve is today's path ------------------------------


def _normalised(w: World, outcome) -> dict:  # noqa: ANN001
    summary = w.summaries.read("g_org")
    judgment = w.store.get_judgment(outcome.contribution_id)
    return {
        "directives": [(d.content, d.subject) for d in summary.directives],
        "context": summary.context,
        "decision": judgment.decision,
        "notes": judgment.notes,
    }


def test_approving_a_no_op_accept_as_directive_equals_the_level_zero_result(
    world0: World, world1: World
) -> None:
    from strata import review  # noqa: PLC0415

    answer = {
        "decision": "accept_as_directive",
        "reasoning": "Binding, but it changes nothing.",
        "directive_ops": [],
        "new_context": "The audit closed on 9 November.",
    }
    today = world0.propose(answer, content="The audit closed on 9 November.")
    held = world1.propose(answer, content="The audit closed on 9 November.")
    state = world1.store.get_record_entry(held.contribution_id).state
    assert state.state == "held" and state.accepted is False  # the decision alone is the trigger
    review.approve(held.contribution_id, operator="oren", **world1.kw())
    assert _normalised(world1, held) == _normalised(world0, today)
