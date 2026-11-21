"""#126 review findings: an approval never overwrites newer context, and is idempotent.

1. A held judgment's context rewrite replaces the scope's whole context. If the scope's summary
   moved on since the hold, the approval keeps the current context and applies only the ops.
2. Approve writes the amendment and then the disposition, in two steps (the summary is a file, so
   they cannot be one transaction). A crash between them must not double-apply on the next approve.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from strata import review
from tests.v121_126_support import NEW_RULE, World, directive_answer


@pytest.fixture
def world(tmp_path: Path):  # noqa: ANN201
    w = World(tmp_path, {"g_org": 1})
    yield w
    w.close()


def _context_answer(text: str) -> dict:
    return {
        "decision": "accept_as_context",
        "reasoning": "A plain fact.",
        "directive_ops": [],
        "new_context": text,
    }


def _crash_once(store, name: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    real = getattr(store, name)
    state = {"crashed": False}

    def flaky(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if not state["crashed"]:
            state["crashed"] = True
            raise RuntimeError("the process died after the write")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, name, flaky)


# -- 1. stale context ---------------------------------------------------------------------------


def test_an_approval_after_the_context_moved_on_keeps_the_current_context(world: World) -> None:
    held = world.propose(
        {**directive_answer([{"op": "append"}]), "new_context": "The judge's old rewrite."}
    )
    world.propose(
        _context_answer("A later context item."), content="A later fact.", classification="context"
    )
    assert "A later context item." in world.summaries.read("g_org").context

    act = review.approve(held.contribution_id, operator="oren", **world.kw())

    summary = world.summaries.read("g_org")
    assert "A later context item." in summary.context  # nothing admitted meanwhile was wiped
    assert "The judge's old rewrite." not in summary.context
    assert NEW_RULE in [d.content for d in summary.directives]  # the ops still applied
    assert "kept the current context" in (act.reason or "")


def test_an_approval_with_an_unchanged_scope_applies_the_judges_context(world: World) -> None:
    held = world.propose(
        {**directive_answer([{"op": "append"}]), "new_context": "The judge's rewrite."}
    )
    act = review.approve(held.contribution_id, operator="oren", **world.kw())
    assert world.summaries.read("g_org").context == "The judge's rewrite."
    assert "applied the judge's context" in (act.reason or "")


# -- 2. idempotent approval ---------------------------------------------------------------------


def test_a_crash_after_the_write_does_not_double_apply_an_append(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = world.propose(directive_answer([{"op": "append"}]))
    _crash_once(world.store, "set_judgment_disposition", monkeypatch)
    with pytest.raises(RuntimeError):
        review.approve(held.contribution_id, operator="oren", **world.kw())
    assert world.store.get_judgment(held.contribution_id).disposition == "held"

    act = review.approve(held.contribution_id, operator="oren", **world.kw())

    minted = [d for d in world.summaries.read("g_org").directives if d.content == NEW_RULE]
    assert len(minted) == 1  # exactly one directive
    assert world.store.get_judgment(held.contribution_id).disposition == "approved"
    assert act.action == "approve"


def test_a_crash_after_the_write_does_not_lapse_an_enacted_judged_retire(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = world.propose(
        directive_answer([{"op": "retire", "id": "c_tokens", "changed_circumstance": "No more."}])
    )
    _crash_once(world.store, "set_judgment_disposition", monkeypatch)
    with pytest.raises(RuntimeError):
        review.approve(held.contribution_id, operator="oren", **world.kw())
    act = review.approve(held.contribution_id, operator="oren", **world.kw())
    assert world.store.get_judgment(held.contribution_id).disposition == "approved"
    assert act.action == "approve"
    assert len(world.store.list_retirements(scope_id="g_org")) == 1


def test_a_crash_after_the_retirement_does_not_lapse_an_enacted_request(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = review.request_retirement(
        "g_org", "c_tokens", "currency", "Not needed.", actor_session_id="s_a", **world.kw()
    )
    _crash_once(world.store, "set_request_disposition", monkeypatch)
    with pytest.raises(RuntimeError):
        review.approve(request.id, operator="oren", **world.kw())
    assert "c_tokens" not in world.org_ids()  # the retirement did happen
    act = review.approve(request.id, operator="oren", **world.kw())
    (stored,) = world.store.list_retirement_requests(scope_id="g_org")
    assert stored.disposition == "approved"  # not lapsed
    assert act.action == "approve"
    assert len(world.store.list_retirements(scope_id="g_org")) == 1
