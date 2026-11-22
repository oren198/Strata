"""A departure or consequence label is written into memory, so the occurrence must be about the
rule's SUBJECT (v1.22 P1).

The live record (g_marketing, c_0ce4ba89f7a73767, 2026-11-20): a correction saying the Hermes
work is closed was labelled "Departure from c_d95c...: ..." against the g_ceo rule "no work
happens in strata-web". The judge answered departure_report with the whole note as the span; the
verifier trusted it because one word ("work") overlapped the rule's action.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strata.scope_manager import Directive, inherited_context_words, verify_inherited_relation
from tests.test_v18_context_exception import _judge

RULE_ID = "c_d95c50ca5de971d6"
RULE = "What still stands is the fleet's existing rule that no work happens in strata-web."
NOTE = (
    "Correction to the 2026-10-26 Hermes entry: the demo is not waiting on an operator go. On "
    "2026-10-26 the operator, through the CEO, decided against Hermes work: no demo, no install, "
    "no further work. I deleted my Hermes clone. The report at .work/marketing/hermes-research.md "
    "stays as the record. The Hermes project is closed."
)
LEAVES = json.loads(
    (Path(__file__).parent / "fixtures" / "v122_departure_label_leaves.json").read_text()
)


@pytest.mark.parametrize("kind", ["departure_report", "consequence_report"])
def test_a_report_whose_span_shares_nothing_with_the_rules_subject_is_not_a_label(
    kind: str,
) -> None:
    verdict, reason = verify_inherited_relation(
        {"kind": kind, "inherited_id": RULE_ID, "occurrence_span": NOTE}, NOTE, RULE
    )
    assert verdict == "admit", (verdict, reason)  # unrelated: admitted, with no label


@pytest.mark.parametrize("kind", ["departure_report", "consequence_report"])
def test_the_summary_and_the_reasoning_carry_no_departure_label_for_that_note(kind: str) -> None:
    ancestors = [
        (
            "g_ceo",
            [
                Directive(
                    id=RULE_ID,
                    content=RULE,
                    subject="strata-mvp",
                    source_scope_id="g_ceo",
                    source_skill=None,
                    created_at="2026-10-20T19:34:32+00:00",
                )
            ],
        )
    ]
    answer = {
        "kind": kind,
        "inherited_id": RULE_ID,
        "reasoning": "r",
        "occurrence_span": NOTE,
    }
    judgment, _client = _judge(NOTE, answer, ancestors=ancestors)
    assert judgment.decision == "accept_as_context"
    text = f"{judgment.reasoning}\n{judgment.new_summary.context}"
    assert "Departure from" not in text and "departure from" not in text
    assert "A report of following" not in text
    assert judgment.inherited_relation["verdict"] == "admit"


# -- the door-table leaves (j1_golden, j1_drift, j1_attribution) -------------------------------


def _verdict(leaf: dict) -> str:
    texts = [leaf["rule"], *leaf["others"]]
    return verify_inherited_relation(
        {"kind": leaf["answer"], "inherited_id": "c_x", "occurrence_span": leaf["content"]},
        leaf["content"],
        leaf["rule"],
        leaf["others"],
        inherited_context_words(texts),
    )[0]


@pytest.mark.parametrize("leaf", LEAVES["keep"], ids=lambda x: f"{x['item']}-{x['answer']}")
def test_a_label_with_subject_overlap_keeps_its_verdict(leaf: dict) -> None:
    """Must-keep: this leaf carried a label on the base (8830b5e) and has subject overlap."""
    assert _verdict(leaf) == leaf["base"]


@pytest.mark.parametrize("leaf", LEAVES["flip"], ids=lambda x: f"{x['item']}-{x['answer']}")
def test_a_label_with_no_subject_overlap_becomes_an_unrelated_admit(leaf: dict) -> None:
    assert _verdict(leaf) == "admit"
