"""Pricing a run from OpenRouter: the right URL, and the right provider's price.

The bodies are recorded in shape from OpenRouter's `/api/v1/models/<id>/endpoints`; nothing here
touches a network."""

from __future__ import annotations

import pytest

from strata.judgecheck.cost import PriceUnknown, fetch_price, models_url, resolve_price

MODEL = "qwen/qwen3-235b-a22b-2507"
ENDPOINTS_URL = f"https://openrouter.ai/api/v1/models/{MODEL}/endpoints"
ENDPOINTS = {
    "data": {
        "id": MODEL,
        "name": "Qwen: Qwen3 235B A22B Instruct 2507",
        "endpoints": [
            {
                "name": "Nebius | qwen/qwen3-235b-a22b-2507",
                "provider_name": "Nebius",
                "tag": "nebius/fp8",
                "pricing": {"prompt": "0.00000009", "completion": "0.00000055"},
            },
            {
                "name": "Parasail | qwen/qwen3-235b-a22b-2507",
                "provider_name": "Parasail",
                "tag": "parasail/fp8",
                "pricing": {"prompt": "0.00000014", "completion": "0.0000008"},
            },
            {
                "name": "DeepInfra | qwen/qwen3-235b-a22b-2507",
                "provider_name": "DeepInfra",
                "tag": "deepinfra/fp8",
                "pricing": {"prompt": "0.00000012", "completion": "0.0000006"},
            },
        ],
    }
}


def _get(seen: list[str], body: dict | None = None):  # noqa: ANN202
    def get(url: str) -> dict:
        seen.append(url)
        if url != ENDPOINTS_URL:
            raise AssertionError(f"unexpected URL {url}")
        return body or ENDPOINTS

    return get


@pytest.mark.parametrize(
    "base",
    [
        "https://openrouter.ai/api",
        "https://openrouter.ai/api/",
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1/",
        "https://OpenRouter.ai/api",
    ],
)
def test_every_form_of_the_openrouter_base_reads_the_v1_listing(base: str) -> None:
    seen: list[str] = []
    price = fetch_price(base, MODEL, provider="Parasail", http_get=_get(seen))
    assert seen == [ENDPOINTS_URL]
    assert price is not None


def test_the_documented_base_url_is_not_asked_for_api_models() -> None:
    assert models_url("https://openrouter.ai/api") == "https://openrouter.ai/api/v1/models"
    assert models_url("https://host.example/v1/") == "https://host.example/v1/models"


def test_a_pinned_run_is_priced_at_its_own_providers_endpoint_not_the_cheapest() -> None:
    price = fetch_price("https://openrouter.ai/api", MODEL, provider="Parasail", http_get=_get([]))
    assert price is not None
    assert price.input_per_m == pytest.approx(0.14) and price.output_per_m == pytest.approx(0.80)
    assert "Parasail" in price.source


def test_the_provider_pin_matches_case_insensitively_and_by_tag() -> None:
    for pin in ("parasail", "PARASAIL", "Parasail"):
        price = fetch_price("https://openrouter.ai/api", MODEL, provider=pin, http_get=_get([]))
        assert price is not None and price.output_per_m == pytest.approx(0.80)


def test_a_pinned_provider_that_is_not_listed_is_refused_with_its_name() -> None:
    with pytest.raises(PriceUnknown, match="Chutes"):
        fetch_price("https://openrouter.ai/api", MODEL, provider="Chutes", http_get=_get([]))
    with pytest.raises(PriceUnknown, match="Chutes"):
        resolve_price(
            None, "https://openrouter.ai/api", MODEL, provider="Chutes", http_get=_get([])
        )


def test_an_unpinned_run_is_priced_at_the_dearest_endpoint_of_each_kind() -> None:
    price = fetch_price("https://openrouter.ai/api", MODEL, http_get=_get([]))
    assert price is not None
    assert price.input_per_m == pytest.approx(0.14) and price.output_per_m == pytest.approx(0.80)
    assert "dearest" in price.source


def test_a_failed_read_is_none_and_never_falls_back_to_the_cheapest_listing() -> None:
    seen: list[str] = []

    def failing(url: str) -> dict:
        seen.append(url)
        raise OSError("down")

    assert (
        fetch_price("https://openrouter.ai/api", MODEL, provider="Parasail", http_get=failing)
        is None
    )
    assert seen == [ENDPOINTS_URL]  # it did not try the single-price /models listing
    with pytest.raises(PriceUnknown, match="--price"):
        resolve_price(
            None, "https://openrouter.ai/api", MODEL, provider="Parasail", http_get=failing
        )


def test_an_empty_listing_unpinned_is_none() -> None:
    body = {"data": {"id": MODEL, "endpoints": []}}
    assert fetch_price("https://openrouter.ai/api", MODEL, http_get=_get([], body)) is None


def test_the_price_flag_still_wins_over_any_listing() -> None:
    price = resolve_price("0.2,0.9", "https://openrouter.ai/api", MODEL, provider="Parasail")
    assert (price.input_per_m, price.output_per_m, price.source) == (0.2, 0.9, "--price")


def test_other_hosts_keep_the_plain_models_listing_and_ignore_the_pin() -> None:
    body = {"data": [{"id": MODEL, "pricing": {"prompt": "0.0000002", "completion": "0.0000008"}}]}
    seen: list[str] = []

    def get(url: str) -> dict:
        seen.append(url)
        return body

    price = fetch_price("https://gateway.example/v1", MODEL, provider="Parasail", http_get=get)
    assert seen == ["https://gateway.example/v1/models"]
    assert price is not None and price.input_per_m == pytest.approx(0.2)
