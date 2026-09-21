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
