"""Cost estimate before a run and a hard cap during it.

The cap is enforced in-process, before EVERY judge call: the call is refused when the spend so
far plus a worst-case cost for that one call would pass `max_usd`. Worst case = the call's
`max_tokens` at the output price plus its prompt at the input price (prompt tokens estimated
pessimistically from the request's size). So a run never goes over the cap, and never by more
than one call's estimation error.

A run with a cap but no known price is refused: a cap that cannot be enforced is not a cap.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

#: Judge HTTP calls per judgment, measured on the 1.18.3 gate.
CALLS_PER_JUDGMENT = 1.5
#: Output tokens allowed per call in the estimate (a judgment's tool call is short).
EST_OUTPUT_TOKENS = 250
#: Characters per token used for the PESSIMISTIC (cap) estimate; real text is ~4.
_PESSIMISTIC_CHARS_PER_TOKEN = 3.0


class PriceUnknown(RuntimeError):
    """No price for the configured model and none given with --price."""


class CapReached(Exception):
    """The next call would take the run past --max-usd. Carries the spend so far."""

    def __init__(self, spent_usd: float, max_usd: float) -> None:
        super().__init__(f"cap reached: ${spent_usd:.4f} spent of ${max_usd:.4f}")
        self.spent_usd = spent_usd
        self.max_usd = max_usd


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens."""

    input_per_m: float
    output_per_m: float
    source: str

    def cost(self, input_tokens: float, output_tokens: float) -> float:
        return (input_tokens * self.input_per_m + output_tokens * self.output_per_m) / 1_000_000


def parse_price(text: str) -> Price:
    """`--price IN,OUT` (dollars per million tokens), e.g. ``0.22,0.88``."""
    try:
        low, high = (float(part) for part in text.split(","))
    except ValueError as exc:
        raise ValueError(
            f"--price must look like 0.22,0.88 (input,output per million), got {text!r}"
        ) from exc
    if low < 0 or high < 0:
        raise ValueError("--price must not be negative.")
    return Price(low, high, "--price")


def fetch_price(
    base_url: str | None, model: str, *, http_get: Callable[[str], Any] | None = None
) -> Price | None:
    """The model's price from the provider's models endpoint (OpenRouter shape), or ``None``.

    Reads ``{base_url}/models`` and the entry whose id is *model*: ``pricing.prompt`` and
    ``pricing.completion`` are dollars PER TOKEN there. Any failure, or a provider without the
    endpoint, is ``None``: the caller then needs ``--price``."""
    if not base_url:
        return None
    url = base_url.rstrip("/") + "/models"
    try:
        if http_get is None:
            import httpx  # noqa: PLC0415

            body = httpx.get(url, timeout=10.0).json()
        else:
            body = http_get(url)
        for entry in body.get("data", []):
            if entry.get("id") == model:
                pricing = entry["pricing"]
                return Price(
                    float(pricing["prompt"]) * 1_000_000,
                    float(pricing["completion"]) * 1_000_000,
                    "provider models endpoint",
                )
    except Exception:  # noqa: BLE001 — a price lookup must never break a run
        return None
    return None


def resolve_price(
    cli_price: str | None,
    base_url: str | None,
    model: str,
    *,
    http_get: Callable[[str], Any] | None = None,
) -> Price:
    """`--price` wins; else the provider's endpoint; else refuse, naming --price."""
    if cli_price:
        return parse_price(cli_price)
    found = fetch_price(base_url, model, http_get=http_get)
    if found is None:
        raise PriceUnknown(
            f"No price is known for model {model!r}. Pass --price IN,OUT (dollars per million "
            "tokens); judge-check never runs unpriced."
        )
    return found


def request_tokens(
    kwargs: dict[str, Any], *, chars_per_token: float = _PESSIMISTIC_CHARS_PER_TOKEN
) -> float:
    """A pessimistic prompt-size estimate for one request."""
    blob = json.dumps(
        [kwargs.get("system"), kwargs.get("tools"), kwargs.get("messages")], default=str
    )
    return len(blob) / chars_per_token


def worst_case_cost(kwargs: dict[str, Any], price: Price) -> float:
    """The most one call can cost: all of ``max_tokens`` as output plus the whole prompt."""
    return price.cost(request_tokens(kwargs), float(kwargs.get("max_tokens", EST_OUTPUT_TOKENS)))


@dataclass(frozen=True)
class Estimate:
    judgments: int
    calls: float
    input_tokens_per_call: float
    output_tokens_per_call: float
    est_usd: float
    price_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "judgments": self.judgments,
            "calls": self.calls,
            "input_tokens_per_call": self.input_tokens_per_call,
            "output_tokens_per_call": self.output_tokens_per_call,
            "est_usd": self.est_usd,
            "price_source": self.price_source,
        }


def estimate(item_sizes: Sequence[int], reps: int, price: Price) -> Estimate:
    """Estimate before the run. *item_sizes* are the prompt sizes in characters of the set's
    items (system prompt included); tokens use the real-text ratio (4 chars), not the cap's."""
    judgments = len(item_sizes) * reps
    calls = judgments * CALLS_PER_JUDGMENT
    avg_in = (sum(item_sizes) / len(item_sizes) / 4.0) if item_sizes else 0.0
    cost = calls * price.cost(avg_in, EST_OUTPUT_TOKENS)
    return Estimate(judgments, calls, avg_in, float(EST_OUTPUT_TOKENS), cost, price.source)


@dataclass
class Budget:
    """The spend of a run, shared by every judge in it (so an A/B run has ONE cap)."""

    price: Price | None
    max_usd: float | None
    spent_usd: float = 0.0
    calls: int = 0

    def __post_init__(self) -> None:
        if self.max_usd is not None and self.price is None:
            raise PriceUnknown("a cap needs a price: pass --price IN,OUT.")

    def check_before(self, kwargs: dict[str, Any]) -> None:
        if self.max_usd is None or self.price is None:
            return
        if self.spent_usd + worst_case_cost(kwargs, self.price) > self.max_usd:
            raise CapReached(self.spent_usd, self.max_usd)

    def add_after(self, kwargs: dict[str, Any], response: Any) -> None:
        self.calls += 1
        if self.price is None:
            return
        usage = getattr(response, "usage", None)
        tin = getattr(usage, "input_tokens", None)
        tout = getattr(usage, "output_tokens", None)
        if tin is None or tout is None:
            self.spent_usd += worst_case_cost(
                kwargs, self.price
            )  # unknown usage: charge the worst case
            return
        extra = (getattr(usage, "cache_creation_input_tokens", 0) or 0) + (
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )
        self.spent_usd += self.price.cost(float(tin) + float(extra), float(tout))


def item_prompt_chars(item: Any) -> int:
    """A rough size, in characters, of the prompt an item produces: the engine's judge system
    prompt plus the item's own scenario and contribution/proposal. Used only by the estimate."""
    from strata.scope_manager import _SYSTEM_PROMPT  # noqa: PLC0415

    body = json.dumps(
        [item.data.get("scenario"), item.data.get("contribution"), item.data.get("proposal")],
        default=str,
    )
    return len(_SYSTEM_PROMPT) + len(body)
