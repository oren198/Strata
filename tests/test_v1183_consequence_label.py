"""1.18.3: the record line for a consequence report depends on which answer reached it.

An exception answer the engine kept as a specific past report gets the departure
wording. A follows answer (kind ``consequence_report``) keeps the following wording.
The wording is the judgment's record line, not a prefix on the stored note.
"""

from __future__ import annotations

from strata.scope_manager import _inherited_relation_note

_FOLLOWING = (
    "[A report of following c_pump01 (g_arch). To raise it to g_arch, resubmit with "
    "acted_on = c_pump01.]"
)
_DEPARTURE = (
    "[A departure from c_pump01 (g_arch), not a licence: only g_arch decides whether the rule is "
    "slack. To raise it to g_arch, resubmit with acted_on = c_pump01.]"
)


def _outcome(kind: str) -> dict:
    return {
        "kind": kind,
        "verdict": "consequence_report",
        "inherited_id": "c_pump01",
        "origin": "g_arch",
    }


def test_a_consequence_report_from_an_exception_answer_gets_the_departure_wording() -> None:
    assert _inherited_relation_note(_outcome("exception")) == _DEPARTURE


def test_a_consequence_report_from_a_follows_answer_keeps_the_following_wording() -> None:
    assert _inherited_relation_note(_outcome("consequence_report")) == _FOLLOWING
    assert _inherited_relation_note(_outcome("exception")) == _DEPARTURE
