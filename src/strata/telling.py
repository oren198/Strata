"""Whether a contribution records a TELLING ACT: someone spoke to, wrote to, or agreed with the
agent, or a party put something out. A pure lexical test, used to decide when a "Hearsay:"
label cannot be right.

The judge prompt defines hearsay as "someone SPOKE TO THE AGENT (told, mentioned, shared,
said, 'passed along')". A contribution with no such act in its own text is the contributor's
own observation or action, however it names their role ("a keeper replaced the thermometer",
"the pit crew barrowed fresh sand"): first-hand. The test fails toward KEEPING a label: any cue
at all, anywhere in the text, counts as a telling act, so a note is called first-hand only when
nothing in it points at another party speaking, writing, agreeing or publishing.

Examples, cue present (the label may stay):
  "Priya from security told me the rotation moved to Fridays."
  "As discussed with the clinics team, we start at 07:30."
Examples, no cue (the label cannot stand):
  "A keeper replaced the brooder thermometer after the noon round."
  "Our oven runs hot on the left; let's rotate trays halfway through every bake."
"""

from __future__ import annotations

import re

_CUE_RE = re.compile(
    r"\b(?:"
    # spoke or wrote to the agent
    r"told|tell(?:s|ing)?|mention(?:ed|s|ing)?|said|says|saying|shar(?:ed|es|ing)|"
    r"passed\s+(?:along|on|over)|relay(?:ed|s|ing)?|conveyed|inform(?:ed|s|ing)?|"
    r"notif(?:ied|ies|ying)|wrote|writes|messag(?:ed|es|ing)|e-?mail(?:ed|s|ing)?|"
    r"(?:sent|handed|gave|forwarded|pasted)\s+(?:me|us|over|along|notes?|word)|"
    r"heard|asked|briefed|(?:called|rang|phoned)\s+(?:me|us)|"
    r"let\s+(?:me|us)\s+know|reached\s+out|"
    # agreed or discussed with someone
    r"agreed\s+with|discussed\s+with|as\s+discussed|as\s+agreed|"
    # reported, stated, confirmed
    r"report(?:ed|s)|stat(?:ed|es)|claim(?:ed|s)|insist(?:ed|s)|confirm(?:ed|s)|"
    r"announc(?:ed|es)|noted\s+that|flagged\s+(?:to|for)|"
    # a party put something out
    r"publish(?:ed|es)|issu(?:ed|es)|releas(?:ed|es)|advis(?:ed|ory|ories)|notice|guidance|"
    r"according\s+to|per\s+(?:the\s+)?[a-z][\w-]*['’]s|per\s+(?:their|its|his|her)"
    r")\b",
    re.IGNORECASE,
)
#: A quoted utterance ("..." or curly quotes) is somebody's words.
_QUOTE_RE = re.compile(r"[“\"‘][^”\"’]{6,}[”\"’]")


def has_telling_act(content: str) -> bool:
    """True when *content* points at another party speaking, writing, agreeing or publishing.

    ``False`` means nothing in the text is a telling act, so a "Hearsay:" reading of it is
    wrong. The cue list is deliberately broad: a miss keeps a label, never removes one."""
    text = content or ""
    return bool(_CUE_RE.search(text) or _QUOTE_RE.search(text))
