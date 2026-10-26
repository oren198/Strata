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
from urllib.parse import urlparse

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


_OPENROUTER_V1 = "https://openrouter.ai/api/v1"


def _is_openrouter(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def models_url(base_url: str) -> str:
    """Where the provider lists its models. For OpenRouter, whatever form the Anthropic-
    compatible base takes (``https://openrouter.ai/api``, ``.../api/``, ``.../api/v1``), the
    listing is at ``https://openrouter.ai/api/v1/models``. Other hosts: ``<base>/models``."""
    if _is_openrouter(base_url):
        return _OPENROUTER_V1 + "/models"
    return base_url.rstrip("/") + "/models"


def _get_json(url: str, http_get: Callable[[str], Any] | None) -> Any:
    if http_get is not None:
        return http_get(url)
    import httpx  # noqa: PLC0415

    response = httpx.get(url, timeout=10.0)
    response.raise_for_status()
    return response.json()


def _per_million(pricing: dict[str, Any]) -> tuple[float, float]:
    return float(pricing["prompt"]) * 1_000_000, float(pricing["completion"]) * 1_000_000


def _openrouter_price(
    model: str, provider: str | None, http_get: Callable[[str], Any] | None
) -> Price | None:
    """OpenRouter lists a model once, at its CHEAPEST provider's price, so that listing would
    undercount a run pinned to a dearer provider. The per-endpoint listing has every provider's
    price: a pinned provider is priced at its own endpoint (refused when it is not listed), an
    unpinned run at the dearest endpoint, because OpenRouter may route to any of them."""
    body = _get_json(f"{_OPENROUTER_V1}/models/{model}/endpoints", http_get)
    endpoints = (body.get("data") or {}).get("endpoints") or []
    priced = [(e, _per_million(e["pricing"])) for e in endpoints if e.get("pricing")]
    if provider:
        wanted = provider.strip().lower()
        for endpoint, (low, high) in priced:
            names = {
                str(endpoint.get("provider_name", "")).lower(),
                str(endpoint.get("name", "")).split("|")[0].strip().lower(),
                str(endpoint.get("tag", "")).split("/")[0].lower(),
            }
            if wanted in names:
                return Price(low, high, f"provider endpoint listing ({provider})")
        raise PriceUnknown(
            f"The pinned provider {provider!r} is not listed for model {model!r} on OpenRouter; "
            "pass --price IN,OUT, or fix the provider pin."
        )
    if not priced:
        return None
    return Price(
        max(p[0] for _, p in priced),
        max(p[1] for _, p in priced),
        "provider endpoint listing (dearest endpoint; the run is not pinned)",
    )


def fetch_price(
    base_url: str | None,
    model: str,
    *,
    provider: str | None = None,
    http_get: Callable[[str], Any] | None = None,
) -> Price | None:
    """The model's price from the provider's listing, or ``None`` when it cannot be read.

    OpenRouter (any form of its base URL): the per-endpoint listing, priced at the pinned
    *provider* or, unpinned, at the dearest endpoint (see :func:`_openrouter_price`); a pinned
    provider that is not listed raises :class:`PriceUnknown`. Other hosts: ``<base>/models``,
    the entry whose id is *model* (``pricing.prompt`` / ``completion`` are dollars PER TOKEN).
    Any read failure is ``None``: the caller then needs ``--price``. It never falls back to
    OpenRouter's single-price listing, which is the cheapest provider's."""
    if not base_url:
        return None
    try:
        if _is_openrouter(base_url):
            return _openrouter_price(model, provider, http_get)
        body = _get_json(models_url(base_url), http_get)
        for entry in body.get("data", []):
            if entry.get("id") == model:
                low, high = _per_million(entry["pricing"])
                return Price(low, high, "provider models endpoint")
    except PriceUnknown:
        raise
    except Exception:  # noqa: BLE001 — a price lookup must never break a run
        return None
    return None


def resolve_price(
    cli_price: str | None,
    base_url: str | None,
    model: str,
    *,
    provider: str | None = None,
    http_get: Callable[[str], Any] | None = None,
) -> Price:
    """`--price` wins; else the provider's listing (pinned provider's price on OpenRouter);
    else refuse, naming --price."""
    if cli_price:
        return parse_price(cli_price)
    found = fetch_price(base_url, model, provider=provider, http_get=http_get)
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
