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
