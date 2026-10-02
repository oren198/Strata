"""#225 interior assertions — the input-identity test.

#225's trigger reads `entitlement.others` (already rendered by the
ENTITLEMENT block) and the contribution's own already-known content — no
new argument flows into `_build_user_message`, so the FIRST call's prompt
must be byte-identical before and after #225's engine code, for a
contribution whose content WOULD trigger it. Compared against the fixture
`tests/_judge_prompt_corpus_225.py` captured at release/v1.17.0 @ 01f72a5,
before any #225 code change.
"""

from __future__ import annotations

from pathlib import Path

from tests._judge_prompt_corpus_225 import capture_225_corpus

_GOLDEN_DIR = Path(__file__).parent / "fixtures" / "judge_prompts_225"


def _golden(name: str) -> str:
    return (_GOLDEN_DIR / f"{name}.txt").read_text(encoding="utf-8")


def test_the_first_call_prompt_for_a_triggering_contribution_is_unchanged() -> None:
    corpus = capture_225_corpus()
    for name, rendered in corpus.items():
        assert rendered == _golden(name), f"{name} drifted from the pinned #225 fixture"
