"""v1.15 P1, ADR 0017, criterion 4 — the judge prompt is byte-identical, with or
without `acted_on` set, to what release/v1.15.0 rendered before P1.

P1 is the reference and the agent-facing text ONLY; the judge does not change until P3.
The corpus and its "before" render are captured once, at 4523634, in
tests/_judge_prompt_corpus.py and tests/fixtures/judge_prompts_v1150/ — this test
re-renders the SAME corpus now and diffs, so a change here can only mean the render
itself moved, never a re-baselined expectation (the #205 mistake in a new shape).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._judge_prompt_corpus import capture_corpus

_GOLDEN_DIR = Path(__file__).parent / "fixtures" / "judge_prompts_v1150"


def _golden(name: str) -> str:
    return (_GOLDEN_DIR / f"{name}.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(p.stem for p in _GOLDEN_DIR.glob("*.txt")))
def test_prompt_without_acted_on_matches_the_v1150_golden(name: str) -> None:
    rendered = capture_corpus(acted_on=None)
    assert rendered[name] == _golden(name)


@pytest.mark.parametrize("name", sorted(p.stem for p in _GOLDEN_DIR.glob("*.txt")))
def test_prompt_with_acted_on_set_is_still_byte_identical_to_the_v1150_golden(
    name: str,
) -> None:
    """The whole point of criterion 4: setting acted_on must not change one byte."""
    rendered = capture_corpus(acted_on="c_some_prior_outcome_target")
    assert rendered[name] == _golden(name)


def test_the_corpus_is_not_accidentally_empty() -> None:
    """A guard against the parametrize list silently shrinking to nothing."""
    assert len(list(_GOLDEN_DIR.glob("*.txt"))) == 6
