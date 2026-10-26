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


# --- Judge models (v1.18; the operator's order of 2026-10-11) -------------------------------
# The README names the judge we used and the one we recommend. The rule this replaces (1.13: "never
# rank a judge") is retired by that order; its substance stays: every recommendation sits beside
# its measured numbers and their source, no unmeasured superlatives, an unmeasured judge is not
# called worse, and the stated limits are printed.


def _judge_models() -> str:
    start = _README.index("### Judge models")
    end = _README.index("### Environment variables", start)
    return _README[start:end]


def test_judge_models_ties_the_recommendation_to_its_measurement_and_source() -> None:
    section = _judge_models()
    assert (
        "The v1.18 baseline ran `qwen/qwen3-235b-a22b-2507` on OpenRouter, pinned to the Nebius "
        "provider"
    ) in section
    # what we used, not what to use
    assert "I recommend" not in section
    # the v1.18 measurement: model, provider, build, date, reps, source
    assert (
        "`qwen/qwen3-235b-a22b-2507` on OpenRouter, pinned to Nebius, engine `ed31692`, "
        "2026-10-14, 3 runs per item ([evidence](docs/evidence/v1.18-judge-baseline.md))"
    ) in section
    for figure in (
        "6 of 279",
        "11 of 126",
        "139 of 144",
        "144 of 144",
        "108 of 108",
        "72 of 72",
        "0 of 10 published",
        "32 of 32",
        "141 of 162",
        "$3.40",
    ):
        assert figure in section, figure
    # the cross-model study keeps its build and source, and is not set against v1.18
    assert "2026-09-20, engine `release/v1.13.0` @ `5f5bf49`" in section
    assert "docs/evidence/judge-baseline-2026-09-20.md" in section
    assert "don't set them against the v1.18 table above" in section
    for figure in ("$0.0157", "$0.2139", "0 of 84", "2 of 76 scored"):
        assert figure in section, figure
    assert "This is a new baseline." in section


def test_judge_models_keeps_the_key_promise_and_the_no_silent_switch_rule() -> None:
    section = _judge_models()
    assert (
        "If your only key is an Anthropic one, the install uses `claude-haiku-4-5` on "
        "Anthropic's own endpoint. That key is never sent to the router. Either way"
    ) in section
    assert "JUDGE_PROVIDER=<provider name>" in section
    assert "A model id ages." in section
    assert "A new provider is a new measurement, not the same judge." in section


def test_judge_models_has_no_unmeasured_superlative_and_states_its_limits() -> None:
    section = _judge_models()
    lowered = section.lower()
    for word in ("best", "safest", "superior", "strongest", "most accurate", "most secure"):
        assert word not in lowered, word
    assert (
        "A model or provider not in these tables is unmeasured, not known to be worse." in section
    )
    assert "it does not rank the judges" not in lowered  # retired by the operator's order
    for limit in (
        "A reworded exception that shares no words with the inherited rule can be admitted when "
        "the judge calls it a refinement (seen in 2 of 3 runs on one test item).",
        "the engine still admits 8 of 30 and 15 of 30 exceptions as directives",
        "11 of 12 attempts",
        "The ordinary judge's input wraps the scope summary in an ambiguous fence",
        "the engine doesn't enforce it (#209)",
    ):
        assert limit in section, limit


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
    start = _README.index("| **Outcome loop** (1.15, 1.16) |")
    end = _README.index("| **Where the evidence lives** |", start)
    return _README[start:end]


def test_outcome_loop_row_states_the_direct_readers_and_the_relay_gap() -> None:
    row = _outcome_loop_row()
    assert (
        "a correction is sent once to every scope that reads the publication directly "
        "(child scopes and scopes with a reference edge)"
    ) in row
    assert "reaches the grandchild once when the relayed copy is verbatim (#221)" in row
    assert "paraphrases the claim is not covered (#219)" in row
    assert "(#221, #219)" not in row
    assert "every scope that received it" not in row


def test_outcome_loop_row_states_adoption_with_its_harness_and_sample() -> None:
    row = _outcome_loop_row()
    assert "0 of 6 sessions set `acted_on` before the perspective listed item ids" in row
    assert "and 6 of 6 after (n=6;" in row
    assert "not yet a rate" in row
    assert "Codex not measured." not in row


def test_outcome_loop_row_states_the_limits_as_measured() -> None:
    row = _outcome_loop_row()
    assert ("#219 C wrongly withdraws about 2–7% of a scope's neighbouring items (measured)") in row
    assert "restored there by the operator" in row
    assert "recovered 5 of 12 such items in our measurement" in row
    assert "the operator path is the reliable remedy" in row
    assert "did not recur on re-measure (20 of 20 correct) and is closed" in row
    assert "sometimes declined" not in row


def test_outcome_loop_row_states_the_v116_additions() -> None:
    row = _outcome_loop_row()
    for phrase in (
        "(1.15, 1.16)",
        "not yet a rate",
        "a correction kept 74% of its facts",
        "condensed away or reworded",
        "over-counts the same way (#227)",
    ):
        assert phrase in row


# --- #225: interior assertions admitted via an invented informant (v1.16) ---------------------


def test_the_invented_informant_limit_is_stated_with_its_numbers() -> None:
    assert "tracked in #225" in _README
    assert "hearsay from an informant the judge invented" in _README
    assert "22 admits" in _README
