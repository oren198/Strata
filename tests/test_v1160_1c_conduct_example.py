"""v1.16 item 1c — the ADR 0016 ground block's FIRST-HAND item drops its concrete
worked example ("vendor-mgmt's agent refused our escalation twice") for a placeholder.

FALLBACK (CEO): the criterion-sentence revision and the informant-item addition both
made the live gate worse — conduct misreading rose 13% -> 22%, interior admits 22/60
-> 26/60 — so both are reverted here, per the standing revert condition. What
survives is the plain placeholder alone: the FIRST-HAND item's original sentence
("Observing another scope's conduct is first-hand and is admitted.") is unchanged;
only the quoted example inside it is abstracted. The informant item goes back
byte-identical to the release/v1.16.0 base. The ADR 0016 D2 doc clarification (no
judge input) stands — it was never part of the live gate.
"""

from __future__ import annotations

from pathlib import Path

from strata.scope_manager import _SYSTEM_PROMPT

_GOLDEN = (
    Path(__file__).parent / "fixtures" / "judge_prompts_v1160" / "system_prompt.txt"
).read_text(encoding="utf-8")

_OLD_ITEM = (
    "  (1) FIRST-HAND: the contributor's own observation of the world, including what\n"
    "      another scope DID in its dealings with this one (\"vendor-mgmt's agent refused\n"
    "      our escalation twice\"). Observing another scope's conduct is first-hand and is\n"
    "      admitted.\n"
)
_NEW_ITEM = (
    "  (1) FIRST-HAND: the contributor's own observation of the world, including what\n"
    "      another scope DID in its dealings with this one (\"<another scope>'s agent <did\n"
    "      something> in its dealings with us\"). Observing another scope's conduct is\n"
    "      first-hand and is admitted.\n"
)


def test_only_the_quoted_example_changed() -> None:
    """Vs the 40baded base: only the example quote inside the FIRST-HAND item
    differs — the sentence around it, and everything else, is byte-identical."""
    assert _OLD_ITEM in _GOLDEN
    assert _NEW_ITEM not in _GOLDEN
    assert _GOLDEN.replace(_OLD_ITEM, _NEW_ITEM) == _SYSTEM_PROMPT


def test_the_informant_item_is_byte_identical_to_base() -> None:
    """The criterion sentence and the informant-item addition are both reverted —
    the informant item is exactly what it was at the 40baded base, including Priya."""
    old_informant_item = _GOLDEN.split(_OLD_ITEM, 1)[1].split("  (3)", 1)[0]
    new_informant_item = _SYSTEM_PROMPT.split(_NEW_ITEM, 1)[1].split("  (3)", 1)[0]
    assert old_informant_item == new_informant_item
    assert "Priya, one of the security-eng" in new_informant_item


def test_the_new_item_names_no_real_domain() -> None:
    assert "vendor-mgmt" not in _SYSTEM_PROMPT
    assert "escalation" not in _SYSTEM_PROMPT
    assert "<another scope>'s agent <did" in _SYSTEM_PROMPT
    assert "in its dealings with us" in _SYSTEM_PROMPT


def test_the_criterion_sentence_is_gone() -> None:
    """The revert drops this sentence entirely, not just the example."""
    assert (
        "ASSERTING what the other scope holds, decides, or records, with no one from"
        not in _SYSTEM_PROMPT
    )


def test_the_informant_line_is_gone() -> None:
    """The revert drops this line entirely, not just the example."""
    assert "with no identifying description at all" not in _SYSTEM_PROMPT
    assert "(ADR 0016 D1)" not in _SYSTEM_PROMPT
