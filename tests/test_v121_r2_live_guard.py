"""R2 through the LIVE path: a judged ``retire`` of a directive with a judged end condition.

The cases run ``app.run_contribution`` with a real ``ScopeManager`` whose judge answer is
scripted, so the answer goes through ``ScopeManager.judge`` (which calls
``_guard_judged_end_condition_retires``), the amendment write and the retirement record. None
of them calls a standalone confirmation function (there is none).

The fleet is the #243 test world: g_org owns c_suite and c_tokens, g_web and g_api read them.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from strata.app import run_contribution
from strata.forgetting import compute_retirement_candidates
from strata.record_store import ContributorRef
from strata.scope_manager import ScopeManager
from strata.summary_store import EndCondition
from tests import test_v18_243_publication_exception as t243
from tests.test_scope_manager import _fake_response

world = t243.world

CONDITION = "the audit closes"
EVIDENCE = "The note says the audit closed on 9 November."


def _set_judged(world, directive_id: str) -> None:  # noqa: ANN001
    summaries = world[2]
    summary = summaries.read("g_org")
    summaries.write(
        "g_org",
        summary.model_copy(
            update={
                "directives": [
                    d.model_copy(
                        update={
                            "end_condition": EndCondition(kind="judged", condition=CONDITION),
                            "set_by": directive_id,
                        }
                    )
                    if d.id == directive_id
                    else d
                    for d in summary.directives
                ]
            }
        ),
    )


def _manager(answer: dict) -> ScopeManager:
    client = MagicMock()
    client.messages.create.return_value = _fake_response(answer)
    return ScopeManager(client=client)


def _retire_answer(target: str, evidence: str | None) -> dict:
    op: dict = {"op": "retire", "id": target}
    if evidence is not None:
        op["changed_circumstance"] = evidence
    return {
        "decision": "accept_as_directive",
        "reasoning": "The condition holds.",
        "directive_ops": [op],
        "new_context": None,
    }


def _run(world, answer: dict, *, fulfils: str | None, scope_id: str = "g_org"):  # noqa: ANN001, ANN202
    fleet, store, summaries = world
    scope = fleet.get_scope(scope_id)
    return run_contribution(
        scope=scope,
        stratum=next(s for s in fleet.strata if s.id == scope.stratum_id),
        content="The audit closed on 9 November.",
        proposed_classification="directive",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id=scope_id, skill=None, session_id=f"s_{scope_id}", ts="2026-11-10T12:00:00Z"
        ),
        fleet=fleet,
        record_store=store,
        summary_store=summaries,
        scope_manager=_manager(answer),
        summary_max_words=500,
        fulfils=fulfils,
    )


def _org_ids(world) -> list[str]:  # noqa: ANN001
    return [d.id for d in world[2].read("g_org").directives]


# -- 1. the owner's judge confirms ---------------------------------------------------------


def test_the_owners_judged_retire_with_fulfils_and_evidence_retires_with_the_honest_reason(
    world,  # noqa: ANN001
) -> None:
    _set_judged(world, "c_suite")
    outcome = _run(world, _retire_answer("c_suite", EVIDENCE), fulfils="c_suite")

    assert "c_suite" not in _org_ids(world)
    (retirement,) = world[1].list_retirements(scope_id="g_org")
    assert retirement.directive_id == "c_suite"
    assert retirement.cause == "condition_judged" and retirement.kind == "currency"
    assert retirement.notice == f'end condition judged met: "{CONDITION}" / {EVIDENCE}'
    assert retirement.changed_circumstance == retirement.notice
    assert retirement.evidence_contribution_id == outcome.contribution_id
    assert "verified" not in retirement.notice


# -- 2. nothing else retires it: the op is dropped and the directive stands -------------------


@pytest.mark.parametrize(
    ("case", "fulfils", "evidence"),
    [
        ("no_fulfils", None, EVIDENCE),
        ("fulfils_names_another_directive", "c_tokens", EVIDENCE),
        ("blank_evidence", "c_suite", "   "),
        ("no_evidence", "c_suite", None),
        ("verified", "c_suite", "I verified the audit closed."),
    ],
)
def test_the_op_is_dropped_and_the_directive_stands(world, case, fulfils, evidence) -> None:  # noqa: ANN001
    _set_judged(world, "c_suite")
    _set_judged(world, "c_tokens")  # so that fulfils=c_tokens is a valid claim of its own
    outcome = _run(world, _retire_answer("c_suite", evidence), fulfils=fulfils)

    assert "c_suite" in _org_ids(world), case
    assert world[1].list_retirements(scope_id="g_org") == [], case
    judgment = world[1].get_judgment(outcome.contribution_id)
    assert "retire of c_suite refused" in (judgment.notes or ""), case


# -- 3. a directive without a judged condition keeps the #209 path ------------------------------


def test_a_judged_retire_of_a_directive_without_a_condition_retires_as_before(world) -> None:  # noqa: ANN001
    _set_judged(world, "c_tokens")  # c_suite has no end condition
    _run(world, _retire_answer("c_suite", "no longer true"), fulfils=None)

    assert "c_suite" not in _org_ids(world)
    (retirement,) = world[1].list_retirements(scope_id="g_org")
    assert retirement.directive_id == "c_suite"
    assert retirement.cause != "condition_judged"
    assert retirement.changed_circumstance == "no longer true"


# -- 4. a fulfils from another scope prompts the owner, it never retires -------------------------


def test_a_fulfils_from_another_scope_opens_a_candidate_and_never_retires(world) -> None:  # noqa: ANN001
    _set_judged(world, "c_suite")
    answer = {
        "decision": "accept_as_context",
        "reasoning": "A report.",
        "new_context": "The audit closed on 9 November.",
    }
    outcome = _run(world, answer, fulfils="c_suite", scope_id="g_web")

    assert "c_suite" in _org_ids(world)
    assert world[1].list_retirements(scope_id="g_org") == []
    opened = compute_retirement_candidates(
        "g_org", record_store=world[1], summary_store=world[2], force=True
    )
    ground = {(c.directive_id, c.ground) for c in opened}
    assert ("c_suite", "condition_evidence") in ground
    (candidate,) = [c for c in opened if c.ground == "condition_evidence"]
    assert outcome.contribution_id in candidate.evidence_ids
    assert "c_suite" in _org_ids(world)
