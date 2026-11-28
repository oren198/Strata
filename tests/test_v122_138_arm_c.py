"""#138 (v1.22) arm C: an engine-detected operator echo needs TWO shared subject stems, not three
(the covered-subject plus head-noun branch is unchanged). a7-003 (strata-evals a7_operator_ops) is
the echo only the looser floor reaches."""

from __future__ import annotations

from strata import scope_manager as sm
from strata.scope_manager import OperatorItem
from tests.test_v19_138_attribution import TAG, UNRELATED, _accept, _judge, _judge_calls
from tests.test_v122_138_switch import _run, _script, _text

A7_DIRECTIVE = (
    "All services must retain audit logs for a minimum of 400 days; exceptions require operator "
    "sign-off."
)
A7_ECHO = (
    "Deploy-tooling audit logs are shipped to cold storage with a 400-day retention lifecycle rule."
)
A7_ITEM = OperatorItem(
    id="op_a7d003",
    kind="directive",
    content=A7_DIRECTIVE,
    subject=None,
    created_at="2026-01-01T00:00:00+00:00",
)
A7_MEMORY = [("g_exec", [A7_ITEM])]
#: one shared subject stem ("tag") and no covered subject: below the floor.
ONE_STEM = "A tag is pushed once the release candidate is approved."


def test_the_stem_floor_is_two() -> None:
    assert sm._ECHO_SHARED_STEMS == 2


def test_two_shared_stems_fire_the_echo_test() -> None:
    assert sm._note_echoes_directive(A7_DIRECTIVE, A7_ECHO) is True


def test_one_shared_stem_does_not_fire() -> None:
    assert sm._note_echoes_directive(TAG.content, ONE_STEM) is False


def test_the_a7_echo_gets_its_one_reask_through_the_judge() -> None:
    judgment, client = _judge(
        _accept(A7_ECHO),
        _accept(f"{A7_ECHO}, per operator directive {A7_ITEM.id}."),
        memory=A7_MEMORY,
        content=A7_ECHO,
    )
    assert len(_judge_calls(client)) == 2
    assert "The engine matched the admitted text to operator directive op_a7d003" in _text(
        _judge_calls(client)[1]
    )
    assert judgment.attribution_gap_kept == []


def test_a_one_stem_note_and_an_unrelated_note_get_no_reask() -> None:
    for note in (ONE_STEM, UNRELATED):
        _judgment, client = _judge(_accept(note), content=note)
        assert len(_judge_calls(client)) == 1, note


def test_the_counter_counts_the_a7_reask() -> None:
    from strata.scope_manager import ScopeManager

    manager = ScopeManager(client=_script(_accept(A7_ECHO), _accept(A7_ECHO)))
    _run(manager, A7_ECHO, memory=A7_MEMORY)
    assert manager.attribution_reask_counts == {"accepts_checked": 1, "reasks_fired": 1}
