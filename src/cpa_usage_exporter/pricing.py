"""Model price map for cost estimation.

Prices are expressed in currency units per million tokens. Lookup is exact
first, then longest-matching prefix, so ``claude-sonnet-4-5-20250929`` resolves
against a ``claude-sonnet-4-5`` entry without needing a row per snapshot date.
Costs are an estimate: subscription-based (OAuth) traffic is not billed per
token at all, and providers apply discounts this map cannot know about.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .models import TokenCounts

log = logging.getLogger(__name__)

BUNDLED_PRICES_PATH = Path(__file__).with_name("data") / "model_prices.yaml"


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Per-million-token rates for one model."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    reasoning: float | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> ModelPrice | None:
        if not isinstance(payload, dict):
            return None

        def rate(*names: str) -> float:
            for name in names:
                if payload.get(name) is not None:
                    try:
                        return float(payload[name])
                    except (TypeError, ValueError):
                        return 0.0
            return 0.0

        reasoning_raw = payload.get("reasoning")
        try:
            reasoning = float(reasoning_raw) if reasoning_raw is not None else None
        except (TypeError, ValueError):
            reasoning = None
        return cls(
            input=rate("input", "input_per_mtok", "prompt"),
            output=rate("output", "output_per_mtok", "completion"),
            cache_read=rate("cache_read", "cache_read_per_mtok"),
            cache_write=rate("cache_write", "cache_creation", "cache_write_per_mtok"),
            reasoning=reasoning,
        )


class PriceBook:
    """Model name to :class:`ModelPrice`, with prefix fallback."""

    def __init__(self, prices: dict[str, ModelPrice] | None = None) -> None:
        self._prices: dict[str, ModelPrice] = {}
        self._cache: dict[str, ModelPrice | None] = {}
        if prices:
            for name, price in prices.items():
                self._prices[_normalize(name)] = price

    def __len__(self) -> int:
        return len(self._prices)

    def __bool__(self) -> bool:
        return bool(self._prices)

    @classmethod
    def load(cls, path: str = "", *, fallback_to_bundled: bool = True) -> PriceBook:
        prices: dict[str, ModelPrice] = {}
        if fallback_to_bundled or not path:
            prices.update(_read_price_file(BUNDLED_PRICES_PATH))
        if path:
            resolved = Path(path).expanduser()
            if not resolved.is_file():
                raise FileNotFoundError(f"price map not found: {resolved}")
            prices.update(_read_price_file(resolved))
        return cls(prices)

    def lookup(self, model: str) -> ModelPrice | None:
        key = _normalize(model)
        if key in self._cache:
            return self._cache[key]
        price = self._prices.get(key)
        if price is None:
            best = ""
            for candidate in self._prices:
                if key.startswith(candidate) and len(candidate) > len(best):
                    best = candidate
            price = self._prices.get(best) if best else None
        self._cache[key] = price
        return price

    def estimate_cost(self, model: str, tokens: TokenCounts) -> float | None:
        """Estimated cost for one request, or ``None`` when the model is unpriced."""
        price = self.lookup(model)
        if price is None:
            return None
        # cached_tokens and cache_read_tokens describe the same read-hit pool;
        # providers populate one or the other, so take the larger of the two.
        cache_read = max(tokens.cache_read_tokens, tokens.cached_tokens)
        billable_input = max(0, tokens.input_tokens - tokens.cached_tokens)
        output_rate = price.output
        reasoning_cost = 0.0
        if price.reasoning is not None:
            reasoning_cost = tokens.reasoning_tokens * price.reasoning
        total = (
            billable_input * price.input
            + tokens.output_tokens * output_rate
            + cache_read * price.cache_read
            + tokens.cache_creation_tokens * price.cache_write
            + reasoning_cost
        )
        return total / 1_000_000


def _normalize(model: str) -> str:
    return str(model or "").strip().lower()


def _read_price_file(path: Path) -> dict[str, ModelPrice]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        log.warning("cannot read price map %s: %s", path, exc)
        return {}
    if not isinstance(document, dict):
        return {}
    models = document.get("models", document)
    if not isinstance(models, dict):
        return {}
    prices: dict[str, ModelPrice] = {}
    for name, payload in models.items():
        price = ModelPrice.from_payload(payload)
        if price is not None:
            prices[_normalize(name)] = price
    return prices
