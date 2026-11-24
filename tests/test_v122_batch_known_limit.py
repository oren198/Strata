"""The batch-vs-serial truth fix: the README known limit, the ADR 0011 D3 measured note and the
CHANGELOG pointer say what was measured, and nothing claims batch judgment matches serial."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
ADR = " ".join((ROOT / "docs/adr/0011-judgment-economics.md").read_text(encoding="utf-8").split())
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_the_readme_states_the_batch_vs_serial_limit() -> None:
    assert (
        "Batched judgment is not verdict-equivalent to serial judgment: measured batch-vs-serial "
        "agreement 0.808 against a serial-repeat floor of 0.975 "
        "(qwen/qwen3-235b-a22b-2507 @ Parasail, n=120). Batches form only when several "
        "contributions to one scope queue in one process (mostly `strata start` serving many "
        "clients). Setting `judgment_batch_cap = 1` makes every judgment a single call."
    ) in README


def test_adr_0011_d3_carries_the_measured_note() -> None:
    assert (
        "Measured 2026-11-23 (strata-evals a11 noise floor, 1.22 tree 1c8a84b, "
        "qwen/qwen3-235b-a22b-2507 @ Parasail, n=120): serial-repeat agreement 0.975 "
        "(CI 0.929-0.991), batch-vs-serial verdict agreement 0.808 (CI 0.729-0.869), excess "
        "disagreement 0.167 (Newcombe CI 0.091-0.248). Batch judgment does NOT give the same "
        "verdicts as serial judgment on this judge; the sentence above states the design "
        "intent, not a measured property. Follow-up: 1.23."
    ) in ADR


def test_the_changelog_points_at_the_readme_limit_and_claims_no_hermes() -> None:
    assert "### Known limit" in CHANGELOG
    assert "Batched judgment is not verdict-equivalent to serial judgment" in CHANGELOG
    for text in (README, CHANGELOG):
        assert "hermes" not in text.lower()
