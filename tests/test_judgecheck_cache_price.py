"""Cache reads and writes are billed at their own price, not the input price.

Bodies are recorded in shape from OpenRouter's `/api/v1/models/<id>/endpoints`; no network."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from strata.judgecheck.cost import Budget, Price, fetch_price, parse_price

MODEL = "qwen/qwen3-235b-a22b-2507"
BODY = {
    "data": {
        "id": MODEL,
        "endpoints": [
            {
                "provider_name": "Nebius",
                "tag": "nebius/fp8",
                "pricing": {"prompt": "0.00000009", "completion": "0.00000055"},
            },
            {
                "provider_name": "Parasail",
                "tag": "parasail/fp8",
                "pricing": {
                    "prompt": "0.00000014",
                    "completion": "0.0000008",
                    "input_cache_read": "0.00000005",
                    "input_cache_write": "0.00000018",
                },
            },
        ],
    }
}


def _usage(tin: int, tout: int, read: int = 0, written: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=tin,
            output_tokens=tout,
            cache_read_input_tokens=read,
            cache_creation_input_tokens=written,
        )
    )


def test_a_pinned_price_carries_that_endpoints_cache_prices() -> None:
    price = fetch_price(
        "https://openrouter.ai/api", MODEL, provider="Parasail", http_get=lambda _u: BODY
    )
    assert price is not None
    assert price.cache_read_per_m == pytest.approx(0.05)
    assert price.cache_write_per_m == pytest.approx(0.18)


def test_an_endpoint_without_cache_prices_leaves_them_unknown() -> None:
    price = fetch_price(
        "https://openrouter.ai/api", MODEL, provider="Nebius", http_get=lambda _u: BODY
    )
    assert price is not None
    assert price.cache_read_per_m is None and price.cache_write_per_m is None


def test_an_unpinned_cache_price_is_the_dearest_with_input_price_for_unlisted() -> None:
    price = fetch_price("https://openrouter.ai/api", MODEL, http_get=lambda _u: BODY)
    assert price is not None
    # Nebius lists none, so it counts at its own input price (0.09); Parasail's 0.05 is lower.
    assert price.cache_read_per_m == pytest.approx(0.09)
    assert price.cache_write_per_m == pytest.approx(0.18)


def test_the_budget_charges_cache_reads_at_the_cache_read_price() -> None:
    price = Price(0.14, 0.80, "t", cache_read_per_m=0.05)
    budget = Budget(price, 1.0)
    budget.add_after({}, _usage(1_000, 100, read=6_600))
    expected = (1_000 * 0.14 + 100 * 0.80 + 6_600 * 0.05) / 1e6
    assert budget.spent_usd == pytest.approx(expected)


def test_without_a_cache_price_reads_and_writes_cost_the_input_price() -> None:
    budget = Budget(Price(0.14, 0.80, "t"), 1.0)
    budget.add_after({}, _usage(1_000, 100, read=6_600, written=500))
    assert budget.spent_usd == pytest.approx((8_100 * 0.14 + 100 * 0.80) / 1e6)


def test_cache_writes_use_the_write_price_when_known() -> None:
    budget = Budget(Price(0.14, 0.80, "t", cache_write_per_m=0.18), 1.0)
    budget.add_after({}, _usage(0, 0, written=1_000))
    assert budget.spent_usd == pytest.approx(1_000 * 0.18 / 1e6)


def test_price_flag_takes_an_optional_third_field_for_cache_reads() -> None:
    price = parse_price("0.14,0.80,0.05")
    assert (price.input_per_m, price.output_per_m, price.cache_read_per_m) == (0.14, 0.80, 0.05)
    assert parse_price("0.14,0.80").cache_read_per_m is None
    for bad in ("1", "1,2,3,4", "1,2,-3", "a,b"):
        with pytest.raises(ValueError, match="--price"):
            parse_price(bad)
