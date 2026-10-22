"""v1.19: a dated departure written with an irregular past verb ("wound", "blew") is admitted as
a departure report, on the scope's own directives and on inherited ones. The verbs come from the
1.18.3 irregular-past list, which the own-directive branch gets through the forward-merge.
Neutral fleets only; the re-ask answer is forged.
"""

from __future__ import annotations

import pytest

from strata.scope_manager import Directive, ScopeSummary, verify_inherited_relation
from tests.test_scope_manager import SCOPE
from tests.test_v19_own_directive_242 import EMPTY, OWN_ORIGIN, _answer, _judge

PADDLE_RULE = "Lock paddles must stay closed while the pound is draining."
WHISTLE_RULE = "The shift whistle must blow at 06:00 sharp."

ROWS = [
    (
        "wound",
        PADDLE_RULE,
        "On 2026-10-12 we wound the paddles open without checking the pound.",
    ),
    (
        "blew",
        WHISTLE_RULE,
        "On 2026-10-12 the whistle blew at 06:20 instead of 06:00.",
    ),
]


def _directive(rule: str, *, scope: str) -> Directive:
    return Directive(
        id="c_rule01",
        content=rule,
        subject="lock" if "paddle" in rule else "whistle",
        source_scope_id=scope,
        source_skill="architect",
        created_at="2026-04-01T09:00:00+00:00",
    )


@pytest.mark.parametrize(("verb", "rule", "note"), ROWS, ids=[r[0] for r in ROWS])
def test_an_own_directive_departure_with_an_irregular_verb_is_admitted(verb, rule, note):
    summary = ScopeSummary(
        scope_id=SCOPE.id,
        directives=[_directive(rule, scope=SCOPE.id)],
        context="Existing context.",
        updated_at="2026-04-01T09:00:00+00:00",
    )
    answer = {**_answer("departure_report", occurrence_span=note[:-1]), "inherited_id": "c_rule01"}
    judgment, _ = _judge(note, answer, summary=summary)
    assert judgment.decision == "accept_as_context"
    assert judgment.inherited_relation["verdict"] == "departure_report"
    assert judgment.inherited_relation["origin"] == OWN_ORIGIN
    assert judgment.new_summary.context == (
        f"Existing context.\nDeparture from c_rule01 (this scope): {note}"
    )


@pytest.mark.parametrize(("verb", "rule", "note"), ROWS, ids=[r[0] for r in ROWS])
def test_an_inherited_departure_with_an_irregular_verb_is_admitted(verb, rule, note):
    answer = {**_answer("departure_report", occurrence_span=note[:-1]), "inherited_id": "c_rule01"}
    judgment, _ = _judge(
        note,
        answer,
        summary=EMPTY,
        ancestors=[("g_arch", [_directive(rule, scope="g_arch")])],
    )
    assert judgment.decision == "accept_as_context"
    assert judgment.inherited_relation["verdict"] == "departure_report"


@pytest.mark.parametrize(("verb", "rule", "note"), ROWS, ids=[r[0] for r in ROWS])
def test_the_verifier_reads_the_irregular_verb_as_a_past_occurrence(verb, rule, note):
    answer = {"kind": "departure_report", "inherited_id": "c_rule01", "occurrence_span": note[:-1]}
    verdict, _ = verify_inherited_relation(answer, note, rule)
    assert verdict == "departure_report"
