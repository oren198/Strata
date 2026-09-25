"""v1.16 item 1b — the acted_on block's worked example no longer leaks a concrete
domain fact.

The judge quoted "used port 8443, the service refused; the right port is unknown"
(scope_manager.py's OUTCOME REPORT block, the failed_corrected line) as the
contributor's own words in 14 of 20 reasons. Fixed by dropping the concrete example
for an abstract placeholder in no real domain. This pins that ONLY that one line
changed — every other byte of both v1.16-specific renders, captured at the base in
tests/fixtures/judge_prompts_v1160/ (own commit, before this change), is identical.
"""

from __future__ import annotations

from pathlib import Path

from tests._judge_prompt_corpus_v1160 import capture_v1160_corpus

_GOLDEN_DIR = Path(__file__).parent / "fixtures" / "judge_prompts_v1160"

_OLD_LINE = (
    "  - failed_corrected: the claim was WRONG. The report's own observation is what "
    'now holds — a negative result counts as the replacement ("used port 8443, the '
    'service refused; the right port is unknown" contradicts and supersedes "listens '
    'on 8443"). There is no known-wrong state and no lowered standing: a failure '
    "either replaces the item or the report is declined."
)
_NEW_LINE = (
    "  - failed_corrected: the claim was WRONG. The report's own observation is what "
    'now holds — a negative result counts as the replacement ("<the action>; <the '
    'observed negative result>" contradicts and supersedes the original claim). '
    "There is no known-wrong state and no lowered standing: a failure either "
    "replaces the item or the report is declined."
)


def _golden(name: str) -> str:
    return (_GOLDEN_DIR / f"{name}.txt").read_text(encoding="utf-8")


def test_only_the_failed_corrected_line_changed_in_the_acted_on_render() -> None:
    before = _golden("acted_on_target")
    after = capture_v1160_corpus()["acted_on_target"]
    assert _OLD_LINE in before
    assert _NEW_LINE not in before
    assert before.replace(_OLD_LINE, _NEW_LINE) == after


def test_the_claim_corrected_refresh_render_is_untouched() -> None:
    """1b's fix lives entirely in the OUTCOME REPORT block — the INPUT CHANGES
    instruction line (a different render path) must not move at all."""
    before = _golden("claim_corrected_refresh")
    after = capture_v1160_corpus()["claim_corrected_refresh"]
    assert after == before


def test_the_new_line_names_no_real_domain() -> None:
    from strata.scope_manager import _render_outcome_block
    from tests._judge_prompt_corpus_v1160 import ACTED_ON_TARGET

    block = _render_outcome_block(ACTED_ON_TARGET)
    assert "8443" not in block
    assert "listens on" not in block
    assert "used port" not in block.lower()
    assert "<the action>; <the observed negative result>" in block
