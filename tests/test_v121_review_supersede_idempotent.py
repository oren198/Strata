"""A held judgment whose ops are supersede-only is recognised as applied after a crash.

A supersede-only judgment arises in a batch: the replacement is another member's op, so this
member's own ops carry a supersede and no mint. After the amendment is written the judgment row
is stamped with the summary version it wrote (``summary_version``); a crash before the
disposition leaves that stamp, and only that member's own write could have put it there. The
recognition therefore reads the RECORD, never the summary alone, so a supersede whose target
vanished for another reason is still refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from strata import review
from strata.record_store import ContributorRef
from strata.scope_manager import DirectiveOp, ScopeManagerJudgment
from tests.v121_126_support import World, directive_answer

REPLACEMENT = "Access tokens expire after at most 10 minutes."


@pytest.fixture
def world(tmp_path: Path):  # noqa: ANN201
    w = World(tmp_path, {"g_org": 1})
    yield w
    w.close()


def _hold_supersede_only(w: World, target: str = "c_tokens") -> str:
    """Record a held judgment whose own ops are a supersede and no mint."""
    contribution = w.store.append_contribution(
        scope_id="g_org",
        content="Tokens expire sooner now.",
        proposed_classification="directive",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_org", skill=None, session_id="s_org", ts="2026-11-21T09:00:00Z"
        ),
    )
    summary = w.summaries.read("g_org")
    judgment = ScopeManagerJudgment(
        decision="accept_as_directive",
        reasoning="The old limit is replaced.",
        directive_ops=[DirectiveOp(op="supersede", id=target, contribution_id=contribution.id)],
        new_summary=summary,
    )
    w.store.record_judgment(
        contribution_id=contribution.id,
        decision="accept_as_directive",
        judged_by="scope-manager",
        held_payload=review.encode_held(judgment.model_dump_json(), summary.version),
    )
    return contribution.id


def _crash_once(store, name: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    real = getattr(store, name)
    state = {"crashed": False}

    def flaky(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if not state["crashed"]:
            state["crashed"] = True
            raise RuntimeError("the process died after the write")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, name, flaky)


def test_a_crash_after_a_supersede_only_write_is_recognised_as_applied(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    replacement = world.propose(directive_answer([{"op": "append"}]), content=REPLACEMENT)
    review.approve(replacement.contribution_id, operator="oren", **world.kw())  # the mint
    held = _hold_supersede_only(world)

    _crash_once(world.store, "set_judgment_disposition", monkeypatch)
    with pytest.raises(RuntimeError):
        review.approve(held, operator="oren", **world.kw())
    assert "c_tokens" not in world.org_ids()  # the write did happen
    assert world.store.get_judgment(held).disposition == "held"

    act = review.approve(held, operator="oren", **world.kw())  # no ReviewRefusal

    assert world.store.get_judgment(held).disposition == "approved"
    assert act.action == "approve" and act.reason == "already applied; recorded the approval"
    assert "c_tokens" not in world.org_ids()
    assert REPLACEMENT in [d.content for d in world.summaries.read("g_org").directives]
    approvals = [a for a in world.store.list_review_acts(item_id=held) if a.action == "approve"]
    assert len(approvals) == 1  # no second write, one act


def test_a_supersede_whose_target_vanished_for_another_reason_is_still_refused(
    world: World,
) -> None:
    from strata.operator import operator_retire  # noqa: PLC0415

    held = _hold_supersede_only(world)
    operator_retire("g_org", "c_tokens", "in person", **world.kw())  # not applied by this item
    with pytest.raises(review.ReviewRefusal, match="c_tokens"):
        review.approve(held, operator="oren", **world.kw())
    assert world.store.get_judgment(held).disposition == "held"
    assert world.store.get_judgment(held).summary_version is None  # nothing of this item applied
