"""The README's judge-side known limits say exactly what shipped (#210)."""

from __future__ import annotations

from pathlib import Path

_README = " ".join((Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8").split())


def test_the_office_note_limit_states_what_scope_descriptions_changed_and_what_they_did_not() -> (
    None
):
    assert (
        "Where a scope states a purpose (`description` in fleet.yaml) the judge declines "
        "material outside it — the office-note case above is declined 3/3 with a purpose set, "
        "and 0 of 11 items in that class are admitted. Where a scope states no purpose and "
        "has little or no memory yet, nothing changed: such a note is still admitted "
        "(10 of 11 in that class), and filtering it relies on the agent (#210)."
    ) in _README


def test_the_stale_claim_is_gone() -> None:
    assert "Filtering this kind of junk relies on the agent today" not in _README


# --- Choosing a judge (default-judge change) -------------------------------------------------


def _choosing_a_judge() -> str:
    start = _README.index("### Choosing a judge")
    end = _README.index("### Environment variables", start)
    return _README[start:end]


def test_choosing_a_judge_states_the_measurement_and_its_build() -> None:
    section = _choosing_a_judge()
    assert "2026-09-20" in section
    assert "`release/v1.13.0` @ `5f5bf49`" in section
    assert "docs/evidence/judge-baseline-2026-09-20.md" in section
    for figure in (
        "12",
        "94.4% (51/54)",
        "2.6% (2 of 76 scored; measured on the pre-#212 build)",
        "0 of 130",
        "$0.0157",
    ):
        assert figure in section  # qwen row
    for figure in ("7", "0.0% (0 of 84)", "$0.2139"):
        assert figure in section  # haiku row


def test_choosing_a_judge_states_the_haiku_over_decline_and_the_no_silent_switch_rule() -> None:
    section = _choosing_a_judge()
    assert "over-declined 2 of 6 legitimate operational notes (`wi-102`, `wi-103`)" in section
    # The upgrade promise and the key promise, each a sentence of its own.
    assert (
        "If your only key is an Anthropic one, the install stays on `claude-haiku-4-5` on "
        "Anthropic's own endpoint."
    ) in section
    assert "Anthropic's own endpoint. That key is never sent to the router. Either way" in section
    assert (
        "Strata's judging runs on a model you choose. I tried six of them against the same "
        "suites; three completed every one, and the table in this repo "
        "([docs/evidence/judge-baseline-2026-09-20.md]"
        "(docs/evidence/judge-baseline-2026-09-20.md)) "
        "shows what each did and where a cell is empty — one produced no judgments at all, "
        "one errored on most of the adversarial set, one ran partially."
    ) in section
    assert "`strata doctor` names the judge and endpoint you are actually running" in section
    assert "so read it and pick" in section
    assert "A model id ages" in section
    assert "JUDGE_MODEL=qwen/qwen3-235b-a22b-2507" in section


def test_choosing_a_judge_never_ranks_a_judge() -> None:
    """Truth rule: state what was measured, when, on which build — no 'best', no 'recommended'."""
    section = _choosing_a_judge().lower()
    for word in ("best", "recommend", "better", "safest", "superior", "strongest"):
        assert word not in section, word
    assert "it does not rank the judges" in section


# --- "What the demo shows": the three sentences 1.13 made false (Show HN fix) -------------


def test_the_demo_section_no_longer_claims_haiku_was_unmeasured() -> None:
    assert "it was not measured" not in _README
    assert "Nothing is claimed about the default Claude judge" not in _README
    assert "qwen is also the default judge as of 1.13.0" in _README
    assert "including `claude-haiku-4.5`" in _README


def test_the_demo_section_states_the_oblique_origin_class_is_closed_not_open() -> None:
    assert (
        "is no longer an open limit: ADR 0016 closed it as not a defect — that is "
        "admissible hearsay, not a leak — and #212 tracks the remaining attribution gap "
        "(D5, above)."
    ) in _README
    # The old framing — grouped with the retirement backstop as one pair of open limits —
    # is gone; only the retirement backstop remains tracked under #209.
    assert "and another scope's material relayed with an oblique origin" not in _README
    assert "A fix that declined both classes was tried, regressed elsewhere" not in _README


def test_the_demo_section_keeps_the_112_figure_and_adds_the_113_one() -> None:
    assert "the judge admitted 2 of 72 hard adversarial items (1.12 measurement)" in _README
    assert (
        "measured against the 84-item J4 adversarial set, the judge admitted 1 of 84 "
        "(`j4-822`) — a restricting directive held by the scope's own summary that the "
        "judge did not yet weigh"
    ) in _README


# --- Outcome loop row (v1.15.0) ---------------------------------------------------------------


def _outcome_loop_row() -> str:
    start = _README.index("| **Outcome loop** (1.15) |")
    end = _README.index("| **Where the evidence lives** |", start)
    return _README[start:end]


def test_outcome_loop_row_states_the_direct_readers_and_the_relay_gap() -> None:
    row = _outcome_loop_row()
    assert (
        "a correction is sent once to every scope that reads the publication directly "
        "(child scopes and scopes with a reference edge); copies re-published by a child "
        "are not covered (#221)"
    ) in row
    assert "every scope that received it" not in row


def test_outcome_loop_row_states_adoption_with_its_harness_and_sample() -> None:
    row = _outcome_loop_row()
    assert "0 of 6 sessions set `acted_on` before the perspective listed item ids" in row
    assert "and 6 of 6 after (n=6;" in row
    assert "Codex not measured." in row


def test_outcome_loop_row_states_the_limits_as_measured() -> None:
    row = _outcome_loop_row()
    assert "a published item that *paraphrases* a corrected claim stays up (#219)" in row
    assert (
        "an ambiguous failure was declined instead of treated as a correction in 1 of 3 runs (#220)"
    ) in row
    assert "sometimes declined" not in row
