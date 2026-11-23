"""#138 (v1.22) arm A, the comparison arm: re-ask on EVERY attribution-free accept in a scope that
has operator directives, worded as a confirmation (the engine matched nothing, so it does not claim
a subject match). Internal switch only; the default arm is C."""

from __future__ import annotations

from dataclasses import replace

import pytest

from strata import scope_manager as sm
from strata.scope_manager import ScopeManager
from tests.test_scope_manager import NEW_CONTRIBUTION, SECOND_CONTRIBUTION
from tests.test_v19_138_attribution import (
    TAG,
    UNRELATED,
    _accept,
    _batch_judge,
    _judge,
    _judge_calls,
    _two_context_verdicts,
)
from tests.test_v122_138_switch import _run, _script, _text


@pytest.fixture
def arm_a(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "_ATTRIBUTION_REASK_ARM", "A")


def test_a_an_unrelated_accept_gets_one_confirmation_reask(arm_a: None) -> None:
    judgment, client = _judge(_accept(UNRELATED), _accept(UNRELATED), content=UNRELATED)
    calls = _judge_calls(client)
    assert len(calls) == 2
    text = _text(calls[1])
    assert "The engine matched" not in text  # it matched nothing; it asks of every accept
    assert "operator directive(s) op_tag001" in text
    assert "if any does, resubmit with the attribution" in text.replace("\n", " ")
    assert "otherwise" in text and "unchanged" in text
    assert judgment.new_context == UNRELATED  # unchanged: kept, never filled in by the engine
    assert judgment.attribution_gap_kept == ["op_tag001"]


def test_a_the_reask_is_still_one_and_keep_first(arm_a: None) -> None:
    judgment, client = _judge(
        _accept(UNRELATED), _accept("A different rewrite."), content=UNRELATED
    )
    assert len(_judge_calls(client)) == 2  # exactly one re-ask, never two
    assert judgment.new_context == "A different rewrite."  # a usable retry with the same decision


def test_a_no_reask_without_operator_memory_on_a_decline_or_when_attributed(arm_a: None) -> None:
    _judgment, client = _judge(_accept(UNRELATED), content=UNRELATED, memory=[])
    assert len(_judge_calls(client)) == 1  # no operator directive in scope
    decline = {
        "decision": "decline",
        "reasoning": "Not relevant.",
        "directive_ops": [],
        "new_context": None,
    }
    _judgment, client = _judge(decline, content=UNRELATED)
    assert len(_judge_calls(client)) == 1  # not an accept
    attributed = _accept(f"{UNRELATED}, per operator directive {TAG.id}.")
    _judgment, client = _judge(attributed, content=UNRELATED)
    assert len(_judge_calls(client)) == 1  # already attributed


def test_a_the_batch_asks_once_for_the_whole_batch_naming_each_accepted_member(arm_a: None) -> None:
    echoing = replace(NEW_CONTRIBUTION, content=UNRELATED, proposed_classification="context")
    other = replace(SECOND_CONTRIBUTION, content=UNRELATED, proposed_classification="context")
    first = _two_context_verdicts(echoing, other, UNRELATED)
    judgment, client, echoing, other = _batch_judge(first, first)
    assert len(_judge_calls(client)) == 2  # one batch call plus ONE re-ask
    text = _text(_judge_calls(client)[1])
    assert f"{echoing.id}:{TAG.id}" in text and f"{other.id}:{TAG.id}" in text


def test_the_counter_under_a_counts_one_reask_per_checked_accept(arm_a: None) -> None:
    manager = ScopeManager(client=_script(_accept(UNRELATED)))
    _run(manager, UNRELATED)
    _run(manager, UNRELATED)
    assert manager.attribution_reask_counts == {"accepts_checked": 2, "reasks_fired": 2}


def test_a_the_cost_per_accept_is_visible_and_c_is_cheaper(monkeypatch: pytest.MonkeyPatch) -> None:
    def cost(arm: str) -> int:
        monkeypatch.setattr(sm, "_ATTRIBUTION_REASK_ARM", arm)
        manager = ScopeManager(client=_script(_accept(UNRELATED)))
        for _ in range(3):
            _run(manager, UNRELATED)
        return manager.attribution_reask_counts["reasks_fired"]

    assert cost("C") == 0 and cost("A") == 3
