"""v1.16 item 1c — the ADR 0016 ground block's FIRST-HAND item drops its concrete
worked example ("vendor-mgmt's agent refused our escalation twice") for a placeholder,
plus the philosopher's criterion as rule text: observed conduct and an inference from
it, offered on the contributor's own account, are first-hand; asserting what the other
scope holds/decides/records with nobody standing behind it is not.

FINAL REVISION (CEO): the live gate's leak (interior assertions via an invented
informant — the contributor's own role, or unnamed "others") added one line to the
INFORMANT item: neither the contributor itself nor an undescribed "others" is an
informant; an informant is a party the agent can identify.

The informant example (Priya) is untouched — it was measured harmless. Every other
byte of the static system prompt is identical to the release/v1.16.0 base captured in
tests/fixtures/judge_prompts_v1160/system_prompt.txt (own commit, before this change).
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
    "  (1) FIRST-HAND: the contributor's own observation of the world — what another scope\n"
    "      DID in its dealings with this one (\"<another scope>'s agent <did something> in\n"
    "      its dealings with us\"), and the contributor's own inference from it, offered on\n"
    "      its own account, are both first-hand and admitted. What crosses the line is\n"
    "      ASSERTING what the other scope holds, decides, or records, with no one from\n"
    "      that scope standing behind it.\n"
)

_OLD_INFORMANT_TAIL = (
    "      identifies the person; it does not make their scope stand behind the claim, and\n"
    "      a role is a way of identifying a person, not a way of naming their scope.\n"
    "      THE TEST: can you point at someone"
)
_NEW_INFORMANT_TAIL = (
    "      identifies the person; it does not make their scope stand behind the claim, and\n"
    "      a role is a way of identifying a person, not a way of naming their scope. The\n"
    '      contributor itself, or "others" with no identifying description at all, is not\n'
    "      an informant; an informant is a party the agent can identify — by name, role,\n"
    "      affiliation or location (ADR 0016 D1).\n"
    "      THE TEST: can you point at someone"
)


def test_only_the_first_hand_and_informant_items_changed() -> None:
    assert _OLD_ITEM in _GOLDEN
    assert _NEW_ITEM not in _GOLDEN
    assert _OLD_INFORMANT_TAIL in _GOLDEN
    assert _NEW_INFORMANT_TAIL not in _GOLDEN
    rebuilt = _GOLDEN.replace(_OLD_ITEM, _NEW_ITEM).replace(
        _OLD_INFORMANT_TAIL, _NEW_INFORMANT_TAIL
    )
    assert rebuilt == _SYSTEM_PROMPT


def test_the_informant_example_is_byte_identical() -> None:
    """Priya stays — measured harmless, untouched by this change."""
    assert (
        'identified by name or by role or affiliation ("Priya, one of the security-eng'
        in _SYSTEM_PROMPT
    )
    assert (
        'identified by name or by role or affiliation ("Priya, one of the security-eng' in _GOLDEN
    )


def test_the_new_item_names_no_real_domain() -> None:
    assert "vendor-mgmt" not in _SYSTEM_PROMPT
    assert "escalation" not in _SYSTEM_PROMPT
    assert "<another scope>'s agent <did something> in" in _SYSTEM_PROMPT
    assert "its dealings with us" in _SYSTEM_PROMPT


def test_the_criterion_names_the_manufactured_attribution_line() -> None:
    assert (
        "ASSERTING what the other scope holds, decides, or records, with no one from"
        in _SYSTEM_PROMPT
    )


def test_the_informant_item_excludes_the_contributor_and_undescribed_others() -> None:
    """CEO's final revision: the live gate's leak was an INVENTED informant — the
    contributor's own role, or unnamed 'others'. 'Unnamed' means no identifying
    description at all: a role, affiliation or location still identifies a party."""
    assert (
        'contributor itself, or "others" with no identifying description at all, is not\n'
        "      an informant; an informant is a party the agent can identify — by name, role,\n"
        "      affiliation or location (ADR 0016 D1)."
    ) in _SYSTEM_PROMPT
    # A role/affiliation/location informant is still explicitly admitted elsewhere.
    assert "one desk over" in _SYSTEM_PROMPT
    assert "the vendor relationship shared some numbers with me" in _SYSTEM_PROMPT
