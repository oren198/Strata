"""The text report says the hearsay-on-first-hand count is an upper bound."""

from __future__ import annotations

from strata.judgecheck import load_set, score
from strata.judgecheck.report import build_report, render_text, run_header
from tests.judgecheck_support import golden_result

CAVEAT = (
    "counted with a cue list known to miss some forms of reported speech; "
    "read it as an upper bound, not a measured error rate."
)


def test_the_text_report_carries_the_upper_bound_caveat() -> None:
    items = {i.id: i for i in load_set()}
    scored = score(
        [golden_result(i, 0) for i in items.values()], items, reps=1, strata_version="9.9.9"
    )
    header = run_header(
        {"model": "m", "provider": "p", "base_url_host": "h"},
        engine_version="9.9.9",
        set_sha256="0" * 64,
        reps=1,
        spent_usd=0.0,
        date="2026-10-28",
    )
    text = render_text(build_report(scored.to_dict(), header))
    assert CAVEAT in text
    assert "hearsay_labels_on_first_hand" not in text
