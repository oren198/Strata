"""v1.20 item 4: the telling-act cue test (strata.telling)."""

from __future__ import annotations

import pytest

from strata.telling import has_telling_act

WITH_A_TELLING_ACT = [
    "Priya from security told me the rotation moved to Fridays.",
    "As discussed with the clinics team at the monthly review, we start intake at 07:30.",
    "The evening note says the west brooder thermometer reads warm.",
    "The group one desk over mentioned their retry logic in passing.",
    "The county library consortium published a notice that loans pause for August.",
    'Fraud\'s on-call engineer sent me their notes: "Chargeback events are not logged".',
    "Sam, our compliance liaison, told me directly that releases need a sign-off.",
]
WITHOUT_ONE = [
    "A keeper replaced the brooder thermometer after the noon round.",
    "Fresh dry sand was barrowed into the pit after the inspection.",
    "The harbor bell rang after the buoy lamp was already lit.",
    "The skiff crew logged the buoy lamp in the evening book.",
    "Our harbour-bakery oven runs hot on the left; let's rotate trays halfway through every bake.",
    "Heads up -- facilities' maintenance crew had the HVAC switched off when we arrived.",
]


@pytest.mark.parametrize("text", WITH_A_TELLING_ACT)
def test_a_note_that_records_a_telling_act_is_flagged_as_one(text: str) -> None:
    assert has_telling_act(text)


@pytest.mark.parametrize("text", WITHOUT_ONE)
def test_a_note_with_no_telling_act_is_first_hand(text: str) -> None:
    assert not has_telling_act(text)


def test_an_empty_note_has_no_telling_act() -> None:
    assert not has_telling_act("") and not has_telling_act(None)  # type: ignore[arg-type]
